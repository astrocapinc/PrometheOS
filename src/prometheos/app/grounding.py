"""Grounding of evidence-matrix rows (A02, CP11 S11.4; decision 0064 §2).

``apply`` calls :func:`check_matrix` and :func:`check_extraction` for every create and revise of
a ``matrix`` or ``extraction`` record, whoever the caller (an agent, a rule run, the owner), so
no route can store an ungrounded cell (C11-GROUNDING):

- a proposed or reviewed cell has evidence; a cell without evidence is unsupported and claims no
  value;
- each evidence reference exists and is visible, belongs to the row's work (its full text, its
  abstract or one of its bibliography entries), passes ``check_evidence`` (S11.1 page and
  code-point offsets, S11.2 line ranges, the quote digest) and carries a quote digest; a PDF
  citation without offsets (a carried, pre-S11.1 one) never grounds a cell;
- the basis agrees with the evidence and with the work's ``access_state``;
- every quantity's decimal and unit stand verbatim, as a whole token, inside a quoted slice (no
  tolerance, numbers below ten included), and every number a cell states is either bound or
  quoted;
- a choice is one of its column's values.

Reviewing is the owner's act: the effects returned here are protected, so an agent's request
becomes ``approval_required``. A refusal names the cell.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from prometheos.app import quantities
from prometheos.domain.bodies import NOT_FOUND, ExtractionBody, MatrixBody, MatrixCell
from prometheos.domain.canonical import dumps_canonical
from prometheos.domain.models import SENSITIVITY_ORDER, EvidenceRef, PrometheosError
from prometheos.store.project import RecordState, Txn, live


def _invalid(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


def body_at(conn: sqlite3.Connection, record_id: str, rev: int) -> dict[str, Any] | None:
    """The body of one revision (no visibility check: callers check the revision first)."""
    row = conn.execute(
        "SELECT b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
        "WHERE v.record_id = ? AND v.rev = ?",
        (record_id, rev),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    body: dict[str, Any] = json.loads(row[0])
    return body


def _require(txn: Txn, record_id: str, rev: int, kind: str) -> RecordState:
    from prometheos.app.apply import require_ref

    require_ref(txn, record_id, rev, {kind})
    state = live(txn.get(record_id))
    if state is None or state.kind != kind:
        raise PrometheosError("not_found", f"{kind} does not exist", {"id": record_id})
    return state


def _canonical(value: Any) -> str:
    return dumps_canonical(value).decode("utf-8")


# ----- the matrix --------------------------------------------------------------------------------


def check_matrix(txn: Txn, model: MatrixBody, base: RecordState | None) -> int:
    """Its works and question exist; a column change raises the rubric version. Returns the
    highest sensitivity rank it names."""
    rank = 0
    if model.question_ref is not None:
        ref = model.question_ref
        question = _require(txn, ref.id, ref.rev, "entry")
        cited = body_at(txn.conn, ref.id, ref.rev) or {}
        if cited.get("category") != "question":
            raise _invalid("question_ref names an entry of category question", id=ref.id)
        rank = max(rank, SENSITIVITY_ORDER[question.sensitivity])
    for ref in model.work_refs:
        work = _require(txn, ref.id, ref.rev, "work")
        rank = max(rank, SENSITIVITY_ORDER[work.sensitivity])
    if base is not None:
        before = int(base.body["rubric_version"])
        if model.rubric_version < before:
            raise _invalid("rubric_version never decreases", rubric_version=before)
        columns = [c.model_dump(mode="json") for c in model.columns]
        if _canonical(columns) != _canonical(base.body["columns"]) and (
            model.rubric_version == before
        ):
            raise _invalid(
                "the columns changed: raise rubric_version (rows extracted under the older "
                "rubric become stale)",
                rubric_version=before,
            )
    return rank


# ----- the row -----------------------------------------------------------------------------------


def row_of(conn: sqlite3.Connection, matrix_id: str, work_id: str) -> list[str]:
    """Live extraction records of ``matrix_id`` for ``work_id`` (through the reference index)."""
    rows = conn.execute(
        "SELECT DISTINCT a.from_id FROM refs a JOIN refs b ON b.from_id = a.from_id "
        "JOIN records r ON r.id = a.from_id WHERE a.to_id = ? AND a.field = 'matrix_ref' "
        "AND b.to_id = ? AND b.field = 'work_ref' AND r.kind = 'extraction' "
        "AND r.lifecycle = 'active' ORDER BY a.from_id",
        (matrix_id, work_id),
    ).fetchall()
    return [str(r[0]) for r in rows]


class WorkSources:
    """What a work's cells may cite: its full text (extracted and PDF versions), its abstract
    span and the lines of its bibliography entries."""

    def __init__(self, txn: Txn, work: RecordState) -> None:
        body = work.body
        self.access_state = str(body["access_state"])
        self.full_text: set[str] = set()
        for ref in body.get("full_text_refs") or []:
            self.full_text.add(str(ref["id"]))
            text = live(txn.get(str(ref["id"])))
            derived = (text.body.get("derived_from") or {}) if text else {}
            if derived.get("version_id"):
                self.full_text.add(str(derived["version_id"]))
        self.abstract: dict[str, Any] | None = body.get("abstract_ref")
        self.bibliography: dict[str, list[dict[str, Any]]] = {}
        for record in body.get("field_provenance") or []:
            locator = record.get("locator") or {}
            if record.get("basis") == "bibliography" and locator.get("type") == "text":
                self.bibliography.setdefault(str(record["version_ref"]["id"]), []).append(locator)

    def owns(self, version_id: str) -> bool:
        abstract = self.abstract["version_id"] if self.abstract else None
        return (
            version_id in self.full_text
            or version_id == abstract
            or version_id in self.bibliography
        )

    def abstract_versions(self) -> set[str]:
        """The abstract's version, and its pair when the abstract is in the PDF's page text."""
        if self.abstract is None:
            return set()
        version = str(self.abstract["version_id"])
        if version in self.full_text and self.abstract["locator"].get("type") == "pdf":
            return {version, *self.full_text}
        return {version}


def _contains_lines(outer: dict[str, Any], inner: Any) -> bool:
    first, last = int(outer["start_line"]), int(outer["end_line"])
    return bool(first <= int(inner.start_line) and int(inner.end_line) <= last)


def _check_basis(cell: MatrixCell, ev: EvidenceRef, sources: WorkSources) -> None:
    loc = ev.locator
    if cell.basis == "full_text":
        if ev.version_id not in sources.full_text or loc.type != "pdf":
            raise _invalid(
                "basis full_text cites the work's extracted PDF text (a PDF locator with offsets)",
                version_id=ev.version_id,
            )
    elif cell.basis == "abstract":
        abstract = sources.abstract
        assert abstract is not None  # the access check refused a work without one
        span = abstract["locator"]
        if ev.version_id not in sources.abstract_versions() or loc.type != span.get("type"):
            raise _invalid("basis abstract cites the work's abstract", version_id=ev.version_id)
        if loc.type == "pdf":
            inside = (
                loc.page == span["page"]
                and loc.start is not None
                and loc.end is not None
                and int(span.get("start", 0)) <= loc.start
                and loc.end <= int(span.get("end", -1))
            )
        elif loc.type == "text":
            inside = _contains_lines(span, loc)
        else:
            inside = False
        if not inside:
            raise _invalid(
                "the evidence lies outside the work's abstract", version_id=ev.version_id
            )
    else:  # metadata: the lines of one of this work's bibliography entries
        spans = sources.bibliography.get(ev.version_id)
        if not spans or loc.type != "text" or not any(_contains_lines(s, loc) for s in spans):
            raise _invalid(
                "basis metadata cites the lines of this work's bibliography entry",
                version_id=ev.version_id,
            )


def _slice(txn: Txn, store_dir: Path, ev: EvidenceRef) -> tuple[str, Any]:
    """The quoted text of ``ev`` (already checked by ``check_evidence``) and what a quantity
    pointer is checked against: ``(page text, start, end)`` or ``(lines, start, end)``."""
    from prometheos.app.apply import read_version_bytes, text_lines
    from prometheos.library import index as library_index
    from prometheos.library.views import _text_version_of

    loc = ev.locator
    if loc.type == "pdf":
        text_id = _text_version_of(txn.conn, ev.version_id)
        text = live(txn.get(text_id)) if text_id else None
        payload = library_index.load_payload(txn.conn, text.body) if text else None
        if payload is None or loc.start is None or loc.end is None:
            raise _invalid("the cited PDF has no extracted text", version_id=ev.version_id)
        page = str(payload["pages"][loc.page - 1]["text"])
        return page[loc.start : loc.end], ("pdf", page, loc.start, loc.end)
    version = live(txn.get(ev.version_id))
    assert version is not None
    data = read_version_bytes(txn, store_dir, version.body)
    lines = text_lines(data) if data is not None else None
    if lines is None or loc.type != "text":
        raise _invalid("a cell cites PDF page text or text lines", version_id=ev.version_id)
    quoted = "\n".join(lines[loc.start_line - 1 : loc.end_line])
    return quoted, ("text", lines, loc.start_line, loc.end_line)


def _ground_cell(
    txn: Txn,
    store_dir: Path,
    cell: MatrixCell,
    column: dict[str, Any],
    sources: WorkSources,
) -> int:
    from prometheos.app.apply import check_evidence

    access = sources.access_state
    if cell.basis == "full_text" and access != "full_text":
        raise _invalid(
            f"basis full_text needs the work's full text; this work is {access}",
            access_state=access,
        )
    if cell.basis == "abstract" and (
        access not in ("abstract_only", "full_text") or sources.abstract is None
    ):
        raise _invalid(
            f"basis abstract needs the work's abstract; this work is {access}",
            access_state=access,
        )
    kind = column["value_kind"]
    rank = 0
    slices: list[str] = []
    targets: dict[str, Any] = {}
    for ev in cell.evidence:
        if ev.locator.type == "pdf" and ev.locator.start is None:
            raise _invalid(
                "a PDF citation without character offsets (an older, carried citation) never "
                "grounds a matrix cell; cite the span with start and end",
                version_id=ev.version_id,
            )
        rank = max(rank, check_evidence(txn, store_dir, ev))
        if ev.quote_sha256 is None:
            raise _invalid("each evidence reference of a cell carries its quote digest")
        if not sources.owns(ev.version_id):
            raise _invalid(
                "the cited version does not belong to the row's work", version_id=ev.version_id
            )
        _check_basis(cell, ev, sources)
        quoted, target = _slice(txn, store_dir, ev)
        slices.append(quoted)
        targets[ev.model_dump_json()] = target
    if cell.state != "unsupported":
        if kind == "choice" and cell.value_text not in (column.get("choices") or []):
            raise _invalid(
                "the value is not one of the column's choices", choices=column.get("choices")
            )
        if kind == "quantity" and not cell.quantities:
            raise _invalid("a quantity cell binds at least one quantity")
    for quantity in cell.quantities:
        kind_of_target, text, lo, hi = targets[quantity.source_ref.model_dump_json()]
        q = quantity.model_dump(mode="json")
        if kind_of_target == "pdf":
            quantities.verify_pdf(text, lo, hi, q)
        else:
            quantities.verify_text(text, lo, hi, q)
    if cell.state != "unsupported":
        # One tokenizer for both paths (review F1): in a quantity cell every number is a bound
        # quantity in its written form; in every cell each number, date and digit-bearing word
        # not bound is, whole and exactly, a token of a quoted slice (sign, separators and
        # exponent included).
        if kind == "quantity":
            loose = quantities.unbound_numbers(
                cell.value_text, [q.model_dump(mode="json") for q in cell.quantities]
            )
            if loose:
                raise _invalid(
                    "the value states numbers that are not bound quantities", numbers=loose[:20]
                )
        unquoted = quantities.unquoted_tokens(
            cell.value_text, slices, numbers=kind != "quantity", known_id=lambda i: _known(txn, i)
        )
        if unquoted:
            raise _invalid(
                "the value states numbers that are not in the quoted evidence",
                numbers=unquoted[:20],
            )
    elif _numeric(txn, cell.value_text):
        # An unsupported cell makes no claim, so its reason states no number (review F9).
        raise _invalid(
            "an unsupported cell's reason states no number, date or numbered name",
            numbers=_numeric(txn, cell.value_text)[:20],
        )
    return rank


def _known(txn: Txn, record_id: str) -> bool:
    """A record ID in a value is exempt from the number check only when it names a record the
    caller can see (review round 2, R2-F2): a forged ID is checked like any other token."""
    return live(txn.get(record_id)) is not None


def _numeric(txn: Txn, text: str) -> list[str]:
    return [t.text for t in quantities.tokens(text) if not (t.kind == "id" and _known(txn, t.text))]


def check_extraction(
    txn: Txn,
    store_dir: Path,
    record_id: str,
    model: ExtractionBody,
    base: RecordState | None,
) -> tuple[list[str], int]:
    """Ground every cell; return the protected (owner) effects and the highest rank cited."""
    mref, wref = model.matrix_ref, model.work_ref
    matrix = _require(txn, mref.id, mref.rev, "matrix")
    rubric = body_at(txn.conn, mref.id, mref.rev)
    assert rubric is not None
    work = _require(txn, wref.id, wref.rev, "work")
    if base is not None and (
        base.body["matrix_ref"]["id"] != mref.id or base.body["work_ref"]["id"] != wref.id
    ):
        raise _invalid("a row keeps its matrix and its work", id=record_id)
    if wref.id not in {str(r["id"]) for r in rubric["work_refs"]}:
        raise _invalid("the work is not in the matrix", work_id=wref.id)
    merged = work.body.get("merged_into")
    if merged is not None:
        raise _invalid(
            "the work is merged into another work; extract that one", merged_into=merged["id"]
        )
    if base is None and any(r != record_id for r in row_of(txn.conn, mref.id, wref.id)):
        raise PrometheosError(
            "revision_conflict", "the work already has a row in this matrix; revise it", {}
        )
    if mref.rev != matrix.rev:
        _check_older_revision(model, base, mref.rev, int(rubric["rubric_version"]))
    columns = {str(c["id"]): c for c in rubric["columns"]}
    sources = WorkSources(txn, work)
    rank = max(SENSITIVITY_ORDER[work.sensitivity], SENSITIVITY_ORDER[matrix.sensitivity])
    for cell in model.cells:
        column = columns.get(cell.column_id)
        try:
            if column is None:
                raise _invalid(
                    "no such column in the matrix revision the row names",
                    rubric_version=rubric["rubric_version"],
                )
            rank = max(rank, _ground_cell(txn, store_dir, cell, column, sources))
        except PrometheosError as exc:
            details = {**exc.details, "cell": cell.column_id}
            raise PrometheosError(
                exc.code, f"cell {cell.column_id}: {exc.message}", details
            ) from exc
    return review_effects(txn, record_id, model, base), rank


def _check_older_revision(
    model: ExtractionBody, base: RecordState | None, rev: int, rubric_version: int
) -> None:
    """A row extracted under an older matrix revision (a stale row) may only be reviewed and
    have cells marked unsupported (review F6): a new row, a new cell or a changed value names
    the current revision, so its cells are checked against the current rubric."""
    stale = f"the row names an older matrix revision (rubric {rubric_version})"
    if base is None:
        raise _invalid(f"{stale}: a new row is extracted under the current revision", rev=rev)
    if int(base.body["matrix_ref"]["rev"]) != rev:
        raise _invalid(f"{stale}: name the current revision or keep the row's own", rev=rev)
    before = {str(c["column_id"]): c for c in base.body["cells"]}
    for cell in model.cells:
        prev = before.get(cell.column_id)
        now = cell.model_dump(mode="json")
        if cell.state == "unsupported":
            continue
        if prev is None or _content(prev) != _content(now):
            raise _invalid(
                f"cell {cell.column_id}: {stale}; re-extract the row under the current "
                "revision to change or add a cell",
                cell=cell.column_id,
            )


def _content(cell: dict[str, Any]) -> str:
    return _canonical({k: v for k, v in cell.items() if k != "state"})


def flagged(conn: sqlite3.Connection, record_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM view_flags WHERE record_id = ? AND cleared_seq IS NULL LIMIT 1",
            (record_id,),
        ).fetchone()
        is not None
    )


def review_effects(
    txn: Txn, record_id: str, model: ExtractionBody, base: RecordState | None
) -> list[str]:
    """The owner's acts in this change: a cell that becomes reviewed (or stays reviewed with a
    changed value or evidence), a reviewed cell marked unsupported or removed, and an unchanged
    revision of a row that needs review (a re-review). An agent changing a reviewed cell must
    return it to proposed."""
    before = {str(c["column_id"]): c for c in (base.body["cells"] if base else [])}
    effects: list[str] = []
    seen = set()
    for cell in model.cells:
        seen.add(cell.column_id)
        now = cell.model_dump(mode="json")
        prev = before.get(cell.column_id)
        was_reviewed = prev is not None and prev["state"] == "reviewed"
        if cell.state == "reviewed" and (not was_reviewed or _content(prev or {}) != _content(now)):
            effects.append(f"review matrix cell {cell.column_id} (an owner decision)")
        elif cell.state == "unsupported" and was_reviewed:
            effects.append(f"mark the reviewed matrix cell {cell.column_id} unsupported")
    for column_id, prev in before.items():
        if column_id not in seen and prev["state"] == "reviewed":
            effects.append(f"remove the reviewed matrix cell {column_id}")
    if (
        base is not None
        and not effects
        and flagged(txn.conn, record_id)
        and _canonical(model.model_dump(mode="json")) == _canonical(base.body)
    ):
        effects.append(f"re-review the matrix row {record_id} (it needs review)")
    return effects


def unsupported_text(reason: str) -> str:
    """The text of an unsupported cell: "not found" and the reason, with every number, date or
    numbered name replaced by '#' (an unsupported cell claims no value; review F9)."""
    reason = " ".join(reason.split())[:3000]
    for token in reversed(quantities.tokens(reason)):
        reason = reason[: token.start] + "#" + reason[token.end :]
    return f"{NOT_FOUND}: {reason}" if reason else NOT_FOUND
