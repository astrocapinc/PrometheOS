"""Evidence-matrix changes (A02, CP11 S11.4; decision 0064 §2): the transactions the service
submits through ``apply`` (create, propose a cell, review, mark unsupported), the rule
extraction run and the generated brief.

Every row change is an ordinary ``apply`` operation, so ``app.grounding`` checks it whoever asks;
reviewing is a protected effect (the owner's act, ``approval_required`` for an agent). The rule
run writes each row through the same ``op_create``/``op_revise`` in its own transaction, under a
request key derived from the run, so a resumed run replays instead of writing twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from prometheos.app import reads, runs
from prometheos.app.grounding import unsupported_text
from prometheos.app.reads import Caller, current_record
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.ids import derived_request_id, new_id, new_request_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.library import matrix as views
from prometheos.library import rules as rule_engine

LEVELS = {v: k for k, v in SENSITIVITY_ORDER.items()}


def _highest(*levels: str) -> str:
    return LEVELS[max(SENSITIVITY_ORDER[level] for level in levels)]


def _invalid(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


def request(project_id: str, seq: int, ops: list[dict[str, Any]], request_id: str | None) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "project_id": project_id,
            "request_id": request_id or new_request_id(),
            "expected_seq": seq,
            "operations": ops,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _work(conn: sqlite3.Connection, caller: Caller, work_id: str) -> dict[str, Any]:
    work = views._visible(conn, caller, work_id)
    if work is None or work["kind"] != "work":
        raise PrometheosError("not_found", "work not found", {"id": work_id})
    return work


def row_record(
    conn: sqlite3.Connection, caller: Caller, matrix_id: str, work_id: str
) -> dict[str, Any] | None:
    from prometheos.app.grounding import row_of

    for row_id in row_of(conn, matrix_id, work_id):
        record = views._visible(conn, caller, row_id)
        if record is not None:
            return record
        raise PrometheosError(
            "not_found", "the row of this work is above your scope", {"work_id": work_id}
        )
    return None


def _sensitivity_of(conn: sqlite3.Connection, ids: list[str], floor: str = "normal") -> str:
    level = floor
    for record_id in ids:
        record = current_record(conn, record_id)
        if record is not None:
            level = _highest(level, str(record["sensitivity"]))
    return level


# ----- create ------------------------------------------------------------------------------------


def create_ops(
    conn: sqlite3.Connection,
    caller: Caller,
    *,
    title: str,
    columns: list[dict[str, Any]],
    work_ids: list[str],
    question_id: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    refs = []
    for work_id in work_ids:
        work = _work(conn, caller, work_id)
        refs.append({"id": work_id, "rev": work["rev"]})
    question_ref = None
    if question_id is not None:
        question = views._visible(conn, caller, question_id)
        if question is None:
            raise PrometheosError("not_found", "question entry not found", {"id": question_id})
        question_ref = {"id": question_id, "rev": question["rev"]}
    body = {
        "title": title,
        "question_ref": question_ref,
        "columns": columns,
        "rubric_version": 1,
        "work_refs": refs,
    }
    matrix_id = new_id("matrix")
    sensitivity = _sensitivity_of(conn, [*work_ids, *([question_id] if question_id else [])])
    op = {
        "op": "create", "kind": "matrix", "id": matrix_id, "base_rev": 0,
        "sensitivity": sensitivity, "occurred_at": None, "body": body,
    }  # fmt: skip
    return matrix_id, [op]


# ----- cells -------------------------------------------------------------------------------------


def _slice(conn: sqlite3.Connection, store_dir: Path, caller: Caller, ev: dict[str, Any]) -> Any:
    """``(text, to_pointer)``: the quoted text of ``ev`` and how an offset in it becomes a
    quantity pointer; ``None`` when it cannot be read (grounding then explains why)."""
    from prometheos.app.apply import read_bytes, text_lines
    from prometheos.library import index as library_index
    from prometheos.library.views import _text_version_of

    version = views._visible(conn, caller, str(ev.get("version_id")))
    loc = ev.get("locator") or {}
    if version is None:
        return None
    if loc.get("type") == "pdf" and loc.get("start") is not None:
        text_id = _text_version_of(conn, version["id"])
        text = views._visible(conn, caller, text_id) if text_id else None
        payload = library_index.load_payload(conn, text["body"]) if text else None
        if payload is None or not 1 <= int(loc["page"]) <= int(payload["page_count"]):
            return None
        page = str(payload["pages"][int(loc["page"]) - 1]["text"])
        lo, hi = int(loc["start"]), int(loc["end"])
        return page, lo, hi, lambda s, e: f"pdf:{s}:{e}"
    if loc.get("type") == "text":
        data = read_bytes(conn, store_dir, version["body"])
        lines = text_lines(data) if data is not None else None
        if lines is None or int(loc["end_line"]) > len(lines):
            return None
        first = int(loc["start_line"])
        chosen = lines[first - 1 : int(loc["end_line"])]
        starts, position = [], 0
        for line in chosen:
            starts.append(position)
            position += len(line) + 1

        def pointer(s: int, e: int) -> str:
            index = max(i for i, b in enumerate(starts) if b <= s)
            return f"text:{first + index}:{s - starts[index]}:{e - starts[index]}"

        return "\n".join(chosen), 0, position, pointer
    return None


def bind_quantities(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    column_id: str,
    evidence: list[dict[str, Any]],
    given: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Each requested quantity bound to the cell's evidence: an explicit ``source_pointer`` is
    kept as given (grounding checks it); otherwise the first place where the decimal and unit
    stand verbatim, as a whole token, inside the quoted span. A value that is not there is
    refused here with the same rule grounding applies (no tolerance)."""
    from prometheos.app import quantities

    out = []
    for item in given:
        index = int(item.get("evidence_index", 0))
        if not 0 <= index < len(evidence):
            raise _invalid(f"cell {column_id}: evidence_index names no evidence of the cell")
        ev = evidence[index]
        decimal, unit = str(item["decimal"]), str(item.get("unit") or "")
        pointer = item.get("source_pointer")
        if pointer is None:
            if views._visible(conn, caller, str(ev.get("version_id"))) is None:
                raise PrometheosError(
                    "not_found", "cited version does not exist", {"id": ev.get("version_id")}
                )
            found = _slice(conn, store_dir, caller, ev)
            if found is None:
                raise _invalid(f"cell {column_id}: the cited span cannot be read to bind {decimal}")
            text, lo, hi, to_pointer = found
            span = quantities.find_token(text, decimal, unit, lo, hi)
            if span is None:
                written = f"{decimal} {unit}".strip()
                raise _invalid(
                    f"cell {column_id}: {written} does not occur verbatim in the quoted evidence "
                    "(no tolerance; numbers below ten are checked too)",
                    decimal=decimal,
                    unit=unit,
                )
            pointer = to_pointer(*span)
        out.append({"decimal": decimal, "unit": unit, "source_ref": ev, "source_pointer": pointer})
    return out


def _basis_default(work: dict[str, Any]) -> str:
    state = work["body"]["access_state"]
    return (
        "full_text"
        if state == "full_text"
        else "abstract"
        if state == "abstract_only"
        else ("metadata")
    )


def row_ops(
    conn: sqlite3.Connection,
    caller: Caller,
    matrix_id: str,
    work_id: str,
    change: Any,
    *,
    rebase: bool,
    extra_ids: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    """The create or revise of the row of ``work_id``, with ``change(cells, work, matrix)``
    applied to its cells. Returns (row ID, operations, new body, current row or None).

    ``rebase`` (a proposal, a rule run): the row is (re)extracted under the matrix's current
    revision and the work's current revision, and cells of columns the current rubric no longer
    has are dropped (dropping a reviewed one is the owner's act). Otherwise (review, mark
    unsupported) the row keeps the revisions it names, so a stale row stays stale."""
    matrix = views.matrix_record(conn, caller, matrix_id)
    work = _work(conn, caller, work_id)
    current = row_record(conn, caller, matrix_id, work_id)
    cells = [dict(c) for c in (current["body"]["cells"] if current else [])]
    if current is None or rebase:
        refs = {
            "matrix_ref": {"id": matrix_id, "rev": matrix["rev"]},
            "work_ref": {"id": work_id, "rev": work["rev"]},
        }
        rubric = matrix
        names = {c["id"] for c in matrix["body"]["columns"]}
        cells = [c for c in cells if c["column_id"] in names]
    else:
        refs = {k: dict(current["body"][k]) for k in ("matrix_ref", "work_ref")}
        from prometheos.app.grounding import body_at

        rubric = {**matrix, "body": body_at(conn, matrix_id, int(refs["matrix_ref"]["rev"]))}
    cells = change(cells, work, rubric)
    order = [c["id"] for c in rubric["body"]["columns"]]
    cells.sort(key=lambda c: order.index(c["column_id"]) if c["column_id"] in order else len(order))
    body = {**refs, "cells": cells}
    cited = [str(ev["version_id"]) for c in cells for ev in c["evidence"]]
    cited += [str(ev["source_id"]) for c in cells for ev in c["evidence"]]
    floor = _highest(work["sensitivity"], matrix["sensitivity"])
    if current is not None:
        floor = _highest(floor, current["sensitivity"])
    sensitivity = _sensitivity_of(conn, [*cited, *(extra_ids or [])], floor)
    if current is None:
        row_id = new_id("extraction")
        op = {
            "op": "create", "kind": "extraction", "id": row_id, "base_rev": 0,
            "sensitivity": sensitivity, "occurred_at": None, "body": body,
        }  # fmt: skip
    else:
        row_id = current["id"]
        op = {
            "op": "revise", "id": row_id, "base_rev": current["rev"],
            "sensitivity": sensitivity, "occurred_at": None, "body": body,
        }  # fmt: skip
    return row_id, [op], body, current


def _column(matrix: dict[str, Any], column_id: str) -> dict[str, Any]:
    for column in matrix["body"]["columns"]:
        if column["id"] == column_id:
            return dict(column)
    raise _invalid(f"no column {column_id} in the matrix", column_id=column_id)


def propose_ops(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    *,
    matrix_id: str,
    work_id: str,
    column_id: str,
    value_text: str,
    basis: str | None,
    evidence: list[dict[str, Any]],
    quantities_given: list[dict[str, Any]],
    method: str,
    model: str | None,
    relies_on: list[str],
) -> tuple[str, list[dict[str, Any]]]:
    """A proposed cell (an agent's, a model's or the owner's): it replaces the column's cell,
    so a reviewed cell whose value or evidence changes returns to proposed. ``relies_on``
    names claims or assumption entries the row depends on (``depends_on`` relations)."""

    def change(
        cells: list[dict[str, Any]], work: dict[str, Any], matrix: dict[str, Any]
    ) -> list[dict[str, Any]]:
        _column(matrix, column_id)
        bound = bind_quantities(conn, store_dir, caller, column_id, evidence, quantities_given)
        transformation = None
        if method == "model":
            transformation = {
                "type": "extraction",
                "engine": "agent-model",
                "engine_version": "1",
                "model": (model or "unspecified")[:200],
                "language": None,
                "parameters_digest": canonical_sha256(
                    {"value_text": value_text, "evidence": evidence}
                ),
                "input_version_refs": sorted({str(e["version_id"]) for e in evidence})[:200],
                "run_id": None,
            }
        cell = {
            "column_id": column_id,
            "value_text": value_text,
            "quantities": bound,
            "evidence": evidence,
            "basis": basis or _basis_default(work),
            "state": "proposed",
            "method": method,
            "transformation": transformation,
        }
        return [c for c in cells if c["column_id"] != column_id] + [cell]

    row_id, ops, _body, current = row_ops(
        conn, caller, matrix_id, work_id, change, rebase=True, extra_ids=relies_on
    )
    new_rev = 1 if current is None else int(current["rev"]) + 1
    for target_id in relies_on:
        target = views._visible(conn, caller, target_id)
        if target is None or target["kind"] not in ("claim", "entry", "work", "version"):
            raise PrometheosError(
                "not_found", "relies_on names no visible record", {"id": target_id}
            )
        already = conn.execute(
            "SELECT 1 FROM relations WHERE source_id = ? AND target_id = ? AND relation = "
            "'depends_on' AND retired_seq IS NULL",
            (row_id, target_id),
        ).fetchone()
        if already is None:
            ops.append(
                {
                    "op": "relation_add",
                    "source_ref": {"id": row_id, "rev": new_rev},
                    "target_ref": {"id": target_id, "rev": target["rev"]},
                    "relation": "depends_on",
                }
            )
    return row_id, ops


def review_ops(
    conn: sqlite3.Connection,
    caller: Caller,
    *,
    matrix_id: str,
    work_id: str,
    column_ids: list[str] | None,
) -> tuple[str, list[dict[str, Any]]]:
    """The owner reviews cells (all proposed cells when none are named). A row that needs
    review is re-reviewed even when its cells already are (an unchanged revision)."""

    def change(
        cells: list[dict[str, Any]], work: dict[str, Any], matrix: dict[str, Any]
    ) -> list[dict[str, Any]]:
        named = set(column_ids) if column_ids is not None else None
        present = {c["column_id"] for c in cells}
        if named is not None and named - present:
            raise _invalid("no cell to review in column(s) " + ", ".join(sorted(named - present)))
        out = []
        for cell in cells:
            wanted = (
                cell["column_id"] in named if named is not None else (cell["state"] == "proposed")
            )
            if wanted and cell["state"] == "unsupported":
                raise _invalid(
                    f"cell {cell['column_id']}: an unsupported cell has no value to review; "
                    "propose one or keep it unsupported"
                )
            out.append({**cell, "state": "reviewed"} if wanted else cell)
        return out

    row_id, ops, body, current = row_ops(conn, caller, matrix_id, work_id, change, rebase=False)
    if current is None:
        raise PrometheosError("not_found", "this work has no row to review", {"work_id": work_id})
    flagged = conn.execute(
        "SELECT 1 FROM view_flags WHERE record_id = ? AND cleared_seq IS NULL", (row_id,)
    ).fetchone()
    if body == current["body"] and not flagged:
        raise _invalid("nothing to review: the cells are already reviewed")
    return row_id, ops


def unsupported_ops(
    conn: sqlite3.Connection,
    caller: Caller,
    *,
    matrix_id: str,
    work_id: str,
    column_id: str,
    reason: str,
    basis: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Mark a cell unsupported (no value claim; the reason says why nothing was found)."""

    def change(
        cells: list[dict[str, Any]], work: dict[str, Any], matrix: dict[str, Any]
    ) -> list[dict[str, Any]]:
        _column(matrix, column_id)
        old = next((c for c in cells if c["column_id"] == column_id), None)
        cell: dict[str, Any] = {
            "column_id": column_id,
            "value_text": unsupported_text(reason),
            "quantities": [],
            "evidence": [],
            "basis": basis or (old["basis"] if old else _basis_default(work)),
            "state": "unsupported",
            "method": "manual",
            "transformation": None,
        }
        return [c for c in cells if c["column_id"] != column_id] + [cell]

    row_id, ops, _body, _current = row_ops(conn, caller, matrix_id, work_id, change, rebase=False)
    return row_id, ops


# ----- the generated brief -----------------------------------------------------------------------


def register_brief(
    store: Any, caller: Caller, matrix_id: str, actor: str, request_id: str
) -> dict[str, Any]:
    """Register the Markdown export as a generated version of the matrix's brief source, with
    ``used_in`` relations from the matrix and every reported row (one transaction). The brief
    records the project sequence and the matrix and row revisions it was built from."""
    from prometheos.domain.bodies import SourceBody, VersionBody
    from prometheos.store import derived
    from prometheos.store.project import Txn

    produced: dict[str, Any] = {}

    def handler(txn: Txn) -> None:
        reader = Caller(actor=caller.actor, scope="owner" if txn.max_rank >= 2 else "agent")
        out = views.export(txn.conn, reader, matrix_id, "markdown")
        view = out["view"]
        data = out["content"].encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        matrix = txn.get(matrix_id)
        assert matrix is not None
        rows = [r for r in view["rows"] if r["extraction_id"]]
        levels = [matrix.sensitivity] + [str(r["sensitivity"]) for r in rows]
        levels += [
            str(rec["sensitivity"])
            for r in view["rows"]
            if (rec := current_record(txn.conn, r["work_id"])) is not None
        ]
        sensitivity = _highest(*levels)
        briefs = views.brief_versions(txn.conn, reader, matrix_id)
        source_id = briefs[-1]["source_id"] if briefs else None
        if source_id is None:
            source_id = new_id("source")
            body = SourceBody(
                title=f"Brief: {view['matrix']['title']}"[:300],
                origin="file",
                origin_uri=None,
                root_binding=None,
                retention="copy",
                description=f"{views.BRIEF_TAG} generated from evidence matrix {matrix_id}",
            ).model_dump(mode="json")
            txn.write_revision(
                source_id, "source", 1, txn.insert_body(body), sensitivity, "active", None,
                "matrix_brief", body=body,
            )  # fmt: skip
            txn.result(source_id, 1)
        txn.conn.execute(
            "INSERT OR IGNORE INTO payloads (sha256, data, byte_length, created_seq) "
            "VALUES (?,?,?,?)",
            (sha, data, len(data), txn.seq),
        )
        inputs = sorted(
            {str(ev["version_id"]) for r in rows for c in r["cells"] for ev in c["evidence"]}
        )[:200]
        transformation = {
            "type": "conversion",
            "engine": "prometheos-matrix-brief",
            "engine_version": "1",
            "model": None,
            "language": None,
            "parameters_digest": canonical_sha256(
                {
                    "matrix": [matrix_id, view["matrix"]["rev"]],
                    "rows": [[r["extraction_id"], r["rev"]] for r in rows],
                    "project_seq": view["project_seq"],
                }
            ),
            "input_version_refs": inputs,
            "run_id": None,
        }
        version_id = new_id("version")
        version = VersionBody(
            source_id=source_id,
            sha256=sha,
            byte_length=len(data),
            mime="text/markdown",
            captured_at=txn.recorded_at,
            storage="body",
            storage_key=f"payload:{sha}",
            availability="retained",
            extraction="none",
            derived_from=None,
            payload_schema="text",
            transformation=transformation,  # type: ignore[arg-type]
        ).model_dump(mode="json")
        txn.write_revision(
            version_id, "version", 1, txn.insert_body(version), sensitivity, "active", None,
            "matrix_brief", body=version,
        )  # fmt: skip
        derived.index_text(txn.conn, version_id, "version", "", out["content"])
        txn.result(version_id, 1)
        uses = [(matrix_id, matrix.rev)] + [(r["extraction_id"], r["rev"]) for r in rows]
        for source, rev in uses:
            txn.conn.execute(
                "INSERT INTO relations (source_id, source_rev, target_id, target_rev, relation, "
                "created_seq) VALUES (?,?,?,?,?,?)",
                (source, rev, version_id, 1, "used_in", txn.seq),
            )
        txn.audit("matrix_brief", version_id, 1)
        produced.update(
            {
                "version_id": version_id,
                "source_id": source_id,
                "sha256": sha,
                "built_at_seq": view["project_seq"],
                "rows": [
                    {"work_id": r["work_id"], "extraction_id": r["extraction_id"], "rev": r["rev"]}
                    for r in rows
                ],
                "content": out["content"],
            }
        )

    commit = store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256({"op": "matrix_brief", "matrix_id": matrix_id}),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=caller.rank,
    )
    if commit.get("replayed"):
        version_id = next(
            r["id"] for r in commit["results"] if str(r.get("id", "")).startswith("version_")
        )
        conn = store.connect(readonly=True)
        try:
            version = current_record(conn, version_id)
            assert version is not None
            data = conn.execute(
                "SELECT data FROM payloads WHERE sha256 = ?", (version["body"]["sha256"],)
            ).fetchone()
        finally:
            conn.close()
        content = bytes(data[0]).decode("utf-8") if data else ""
        produced = {
            "version_id": version_id, "source_id": version["body"]["source_id"],
            "sha256": version["body"]["sha256"], "built_at_seq": None, "rows": [],
            "content": content,
        }  # fmt: skip
    return {**produced, "commit": commit}


# ----- the rule extraction run -------------------------------------------------------------------


def located_texts(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, work: dict[str, Any]
) -> list[rule_engine.Located]:
    """What rules search in a work: its extracted pages, else its bibliography abstract."""
    from prometheos.app.apply import read_bytes, text_lines
    from prometheos.library.views import Pages

    body = work["body"]
    if body["access_state"] == "full_text":
        try:
            return rule_engine.pdf_pages(Pages(conn, caller, work_id=work["id"]))
        except PrometheosError:
            return []
    abstract = body.get("abstract_ref")
    if abstract is None or abstract["locator"].get("type") != "text":
        return []
    version = views._visible(conn, caller, str(abstract["version_id"]))
    if version is None:
        return []
    data = read_bytes(conn, store_dir, version["body"])
    lines = text_lines(data) if data is not None else None
    loc = abstract["locator"]
    if lines is None or int(loc["end_line"]) > len(lines):
        return []
    return [
        rule_engine.abstract_lines(
            str(abstract["source_id"]),
            version["id"],
            lines,
            int(loc["start_line"]),
            int(loc["end_line"]),
        )
    ]


class MatrixExtractHandler(runs.Handler):
    operation = "matrix_extract"
    version = "matrix-rules-v1"

    def _matrix(self, env: runs.Env, caller: Caller, matrix_id: str) -> dict[str, Any]:
        conn = env.store.connect(readonly=True)
        try:
            return views.matrix_record(conn, caller, matrix_id)
        finally:
            conn.close()

    def fingerprint(
        self, env: runs.Env, params: dict[str, Any], caller: reads.Caller, run: Any = None
    ) -> runs.Fingerprint:
        rules = rule_engine.parse(params["rules"])
        matrix = self._matrix(env, caller, str(params["matrix_id"]))
        rule_engine.check_against(rules, matrix["body"]["columns"])
        digest = rule_engine.rules_digest(rules)
        return runs.Fingerprint(
            inputs=(
                ("rules", digest),
                ("matrix", canonical_sha256({"id": matrix["id"], "rev": matrix["rev"]})),
            ),
            config={"rules_sha256": digest, "matrix_rev": matrix["rev"]},
        )

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        params = ctx.params
        caller = ctx.caller
        rules = rule_engine.parse(params["rules"])
        matrix_id = str(params["matrix_id"])
        ctx.checkpoint("match")
        limit = rule_engine.deadline()
        plans: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        conn = ctx.store.connect(readonly=True)
        try:
            matrix = views.matrix_record(conn, caller, matrix_id)
            columns = list(matrix["body"]["columns"])
            rule_engine.check_against(rules, columns)
            for work_ref in matrix["body"]["work_refs"]:
                work = views._visible(conn, caller, str(work_ref["id"]))
                if work is None or work["kind"] != "work" or work["body"].get("merged_into"):
                    continue
                texts = located_texts(conn, ctx.store.directory, caller, work)
                fallback = _basis_default(work)
                produced = {}
                for column in columns:
                    mine = [r for r in rules.rules if r.column == column["id"]]
                    if mine:
                        produced[column["id"]] = rule_engine.cell_for(
                            column, mine, texts, fallback, limit
                        )
                plans.append({"work": work, "produced": produced})
        finally:
            conn.close()
        ctx.complete("match", {"works": len(plans)})
        for plan in plans:
            ctx.checkpoint("commit")
            results.append(self._commit_row(ctx, matrix_id, plan["work"], plan["produced"]))
        counts: dict[str, int] = {}
        for row in results:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        ctx.complete("commit", {"counts": counts})
        return {
            "matrix_id": matrix_id,
            "rules_sha256": rule_engine.rules_digest(rules),
            "rows": results,
            "counts": dict(sorted(counts.items())),
        }

    def _commit_row(
        self,
        ctx: runs.RunContext,
        matrix_id: str,
        work: dict[str, Any],
        produced: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Write one work's row. A rule cell that grounding refuses becomes an unsupported cell
        with the reason, and the row's other cells are still written (review F3); the reply
        reports the cells actually stored."""
        produced = dict(produced)
        refused: dict[str, str] = {}
        for _attempt in range(len(produced) + 1):
            try:
                return {**self._write_row(ctx, matrix_id, work, produced), "refused_cells": refused}
            except PrometheosError as exc:
                if exc.code in ("busy", "io_error"):
                    raise
                column = exc.details.get("cell")
                rule_cell = produced.get(str(column)) if column else None
                if rule_cell is None or rule_cell["state"] == "unsupported":
                    return {
                        "work_id": work["id"], "extraction_id": None, "cells": {},
                        "reason": exc.message[:500], "action": "refused",
                        "refused_cells": refused,
                    }  # fmt: skip
                refused[str(column)] = exc.message[:500]
                produced[str(column)] = {
                    **rule_cell,
                    "value_text": unsupported_text(
                        "the rule's match was refused by grounding: " + exc.message
                    )[:4000],
                    "quantities": [],
                    "evidence": [],
                    "state": "unsupported",
                }
        raise AssertionError(
            "unreachable: every refused rule cell was replaced"
        )  # pragma: no cover

    def _write_row(
        self,
        ctx: runs.RunContext,
        matrix_id: str,
        work: dict[str, Any],
        produced: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        from prometheos.app import apply as apply_mod
        from prometheos.domain.models import CreateOp, ReviseOp
        from prometheos.store.project import Txn

        caller = ctx.caller
        conn = ctx.store.connect(readonly=True)
        try:

            def change(
                cells: list[dict[str, Any]], _work: dict[str, Any], matrix: dict[str, Any]
            ) -> list[dict[str, Any]]:
                return rule_engine.merge_row(matrix["body"]["columns"], cells, produced)

            row_id, ops, body, current = row_ops(
                conn, caller, matrix_id, work["id"], change, rebase=True
            )
        finally:
            conn.close()
        states = {c["column_id"]: c["state"] for c in body["cells"]}
        out = {"work_id": work["id"], "extraction_id": row_id, "cells": states, "reason": None}
        if current is not None and current["body"] == body:
            return {**out, "action": "unchanged"}
        op_value = ops[0]
        parsed = (CreateOp if op_value["op"] == "create" else ReviseOp).model_validate(op_value)

        def handler(txn: Txn) -> None:
            txn.op_index = 0
            if isinstance(parsed, CreateOp):
                protected = apply_mod.op_create(txn, ctx.store.directory, parsed)
            else:
                protected = apply_mod.op_revise(txn, ctx.store.directory, parsed)
            if protected:
                raise PrometheosError(
                    "policy_denied", "a rule run only proposes cells", {"effects": protected}
                )

        digest = canonical_sha256({"op": "matrix_rule_row", "operation": op_value})
        ctx.store.commit(
            request_id=derived_request_id("matrix-rule-row", ctx.run.id, work["id"], digest),
            request_sha256=digest,
            expected_seq=None,
            actor=ctx.run.actor,
            handler=handler,
            max_rank=ctx.run.caller_rank,
        )
        return {**out, "action": "created" if current is None else "revised"}


runs.register(MatrixExtractHandler())
