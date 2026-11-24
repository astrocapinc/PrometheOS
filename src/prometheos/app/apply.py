"""The canonical batch mutation: parse, validate and apply request schema v1.

All transports call :func:`apply_request`. Bodies are validated against the
per-kind models, references against this transaction's snapshot, and
protected effects are refused here so no route (create, revise, relation,
import) can bypass them.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel, ValidationError

from prometheos.app import grounding, outbox, paper_rules, quantities, research, results
from prometheos.app.reads import current_record
from prometheos.audio import transcript as transcripts
from prometheos.audio import wav
from prometheos.domain.bodies import (
    BODY_MODELS,
    ActionBody,
    AnchorBody,
    AssessmentBody,
    ClaimBody,
    CorrectionBody,
    EntryBody,
    ExecutionBody,
    ExtractionBody,
    ItemBody,
    MatrixBody,
    PaperBody,
    ProjectSettings,
    QuantityBinding,
    SourceBody,
)
from prometheos.domain.canonical import (
    CanonicalJSONError,
    JSONDepthError,
    canonical_sha256,
    dumps_canonical,
    loads_internal,
    loads_strict,
)
from prometheos.domain.events import DependentKind
from prometheos.domain.ids import kind_of
from prometheos.domain.models import (
    MAX_REQUEST_BYTES,
    SENSITIVITY_ORDER,
    TYPED_ERROR_DETAILS,
    ArchiveOp,
    CorrectionApplyOp,
    CreateOp,
    EvidenceRef,
    ProjectUpdateOp,
    PrometheosError,
    RelationAddOp,
    Request,
    ReviseOp,
    TransitionOp,
)
from prometheos.library import index as library_index
from prometheos.library.views import check_pdf_locator
from prometheos.store import capabilities, derived
from prometheos.store.project import ProjectStore, Txn, live

# Kinds an ordinary apply may create. Sources/versions are registered only by
# the importer so digests and availability always describe real bytes.
APPLY_CREATABLE = {
    "entry",
    "claim",
    "assessment",
    "correction",
    "person",
    "role",
    "meeting",
    "item",
    "action",
    "paper",
    "anchor",
    "execution",  # A02, CP10: observe-only registration (decision 0015)
    "matrix",  # A02, CP11 S11.4 (decision 0064): evidence matrices and their rows, grounded
    "extraction",  # by grounding.check_extraction whoever the caller
}
# CP11 S11.1 (decision 0060): works are registered only by the library importer for now.
IMPORTER_ONLY = {"source", "version", "task", "package", "run", "job", "work"}
SERVICE_MANAGED = IMPORTER_ONLY - {"source"}

# Allowed (source kinds, target kinds) pairs per relation. A relation is valid when one
# pair admits both ends. A02 (CP10, decision 0015) adds: claims and entries depending on an
# assumption entry, and entries and assessments citing an execution.
RELATION_PAIRS: dict[str, tuple[tuple[frozenset[str], frozenset[str]], ...]] = {
    "cites": (
        (
            frozenset(
                {
                    "entry",
                    "claim",
                    "assessment",
                    "anchor",
                    "item",
                    "meeting",
                    "action",
                    "paper",
                    "correction",
                }
            ),
            frozenset({"source", "version"}),
        ),
        (frozenset({"entry", "assessment"}), frozenset({"execution"})),
    ),
    "supports": ((frozenset({"entry", "assessment", "source", "version"}), frozenset({"claim"})),),
    "challenges": (
        (frozenset({"entry", "assessment", "source", "version"}), frozenset({"claim"})),
    ),
    "supersedes": ((frozenset({"claim", "entry"}), frozenset({"claim", "entry"})),),
    "depends_on": (
        (frozenset({"action"}), frozenset({"action"})),
        # The target entry must be of category ``assumption`` (checked against the store).
        (frozenset({"claim", "entry"}), frozenset({"entry"})),
        # CP11 S11.4 (decision 0064): a matrix row relies on what it cites, so an applied
        # correction reaches it (spec/03 RP-00: matrix rows join the closure).
        (frozenset({"extraction"}), frozenset({"work", "version", "claim", "entry"})),
    ),
    "used_in": (
        (frozenset({"claim", "entry", "source", "version"}), frozenset({"paper", "anchor"})),
        # CP11 S11.4: the rows (and the matrix) a generated brief reports.
        (frozenset({"extraction", "matrix"}), frozenset({"version"})),
    ),
    # CP11 S11.2: a preprint and its published version (decision 0062).
    "version_of": ((frozenset({"work"}), frozenset({"work"})),),
    # A06.1 (CP20.2, decision 0083): the research map. The entry ends are checked against the
    # store: answers targets a question; addresses goes from a question to a paper or to the
    # project's objective entry; draws_on goes from a claim or a question to a track.
    "answers": ((frozenset({"claim"}), frozenset({"entry"})),),
    "addresses": ((frozenset({"entry"}), frozenset({"paper", "entry"})),),
    "draws_on": ((frozenset({"claim", "entry"}), frozenset({"entry"})),),
}

# The entry categories each research-map relation requires at its entry ends (decision 0083).
MAP_CATEGORIES: dict[str, tuple[str | None, str | None]] = {
    "answers": (None, "question"),
    "addresses": ("question", None),
    "draws_on": ("question", "track"),
}


EVENT_DEPENDENT_KINDS = frozenset(get_args(DependentKind))


def relation_pair_allowed(relation: str, source_kind: str, target_kind: str) -> bool:
    return any(
        source_kind in sources and target_kind in targets
        for sources, targets in RELATION_PAIRS.get(relation, ())
    )


def validation(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


def parse_request(raw: bytes) -> tuple[Request, str]:
    if len(raw) > MAX_REQUEST_BYTES:
        raise PrometheosError(
            "budget_exceeded", "request exceeds the 8 MiB limit", {"bytes": len(raw)}
        )
    try:
        value = loads_strict(raw)
    except CanonicalJSONError as exc:
        raise validation(f"request is not strict JSON: {exc}") from exc
    try:
        request = Request.model_validate_json(raw, strict=True)
    except ValidationError as exc:
        raise validation("request does not match schema v1", errors=_errors(exc)) from exc
    return request, canonical_sha256(value)


def load_source_json(data: bytes) -> Any:
    """A retained JSON source is the researcher's own document, of any shape (a tree, an
    AST), not request input: it is read to the internal ceiling, and beyond it the refusal says
    why instead of pretending the pointer or the number is wrong."""
    try:
        return loads_internal(data)
    except JSONDepthError as exc:
        raise validation(
            f"the cited JSON source is nested more than {exc.limit} levels deep",
            limit=exc.limit,
        ) from exc


def parse_model_value[M: BaseModel](model: type[M], raw: bytes) -> M:
    """Strict JSON (duplicate keys, NaN rejected) into a strict model."""
    try:
        loads_strict(raw)
    except CanonicalJSONError as exc:
        raise ValueError(str(exc)) from exc
    return model.model_validate_json(raw, strict=True)


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": "/".join(str(p) for p in err["loc"]), "msg": err["msg"]}
        for err in exc.errors()[:20]
    ]


def validate_body(kind: str, body: dict[str, Any]) -> BaseModel:
    model = BODY_MODELS.get(kind)
    if model is None:
        raise PrometheosError(
            "unsupported", f"records of kind {kind} are not available yet", {"kind": kind}
        )
    try:
        return model.model_validate_json(dumps_canonical(body), strict=True)
    except (ValidationError, CanonicalJSONError) as exc:
        details = _errors(exc) if isinstance(exc, ValidationError) else [{"msg": str(exc)}]
        raise validation(f"invalid {kind} body", errors=details) from exc


# ----- evidence and references -----------------------------------------------------


def read_version_bytes(txn: Txn, store_dir: Path, version_body: dict[str, Any]) -> bytes | None:
    return read_bytes(txn.conn, store_dir, version_body)


def read_bytes(conn: Any, store_dir: Path, version_body: dict[str, Any]) -> bytes | None:
    """A retained version's bytes (digest-checked for media), else ``None``."""
    storage = version_body["storage"]
    if storage == "body":
        row = conn.execute(
            "SELECT data FROM payloads WHERE sha256 = ?", (version_body["sha256"],)
        ).fetchone()
        return bytes(row["data"]) if row else None
    if storage == "media":
        path = store_dir / "media" / str(version_body["storage_key"])
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == version_body["sha256"] else None
    return None


def text_lines(data: bytes) -> list[str] | None:
    """Split retained UTF-8 text on LF only (a trailing CR is removed per line).

    str.splitlines would also split on form feeds, U+2028 and other separators,
    which would make line numbers disagree with ordinary editors.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def quote_lines(data: bytes, start: int, end: int) -> bytes | None:
    lines = text_lines(data)
    if lines is None:
        return None
    if end > len(lines):
        return None
    return "\n".join(lines[start - 1 : end]).encode("utf-8")


def evidence_key(value: Any) -> str | None:
    """A canonical identity of one evidence reference (its JSON form), or None."""
    try:
        return dumps_canonical(EvidenceRef.model_validate(value).model_dump(mode="json")).decode()
    except (ValidationError, CanonicalJSONError, TypeError):
        return None


def carried_evidence(body: Any) -> frozenset[str]:
    """Every evidence reference a stored body holds (any field, any depth), as canonical keys:
    a revision that keeps one of them unchanged is not re-checked against rules added after it
    was recorded (CP11 S11.1 review F1; decision 0061)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if {"source_id", "version_id", "locator", "quote_sha256"} == set(node):
                key = evidence_key(node)
                if key is not None:
                    found.add(key)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return frozenset(found)


def check_evidence(txn: Txn, store_dir: Path, ev: EvidenceRef, *, carried: bool = False) -> int:
    """Validate an evidence reference; return the highest cited sensitivity rank. ``carried``:
    the reference is unchanged from the base revision, so the PDF page-text rules of CP11
    (decision 0061 section 8), which postdate it, are not applied to it again."""
    # Purged records read as absent, like hidden ones (review 0009 M1: a purged version's
    # tombstone body has no source_id and used to raise an internal error).
    source = live(txn.get(ev.source_id))
    version = live(txn.get(ev.version_id))
    if source is None or source.kind != "source":
        raise PrometheosError("not_found", "cited source does not exist", {"id": ev.source_id})
    if version is None or version.kind != "version":
        raise PrometheosError("not_found", "cited version does not exist", {"id": ev.version_id})
    if version.body["source_id"] != ev.source_id:
        raise validation("version does not belong to the cited source", version_id=ev.version_id)
    retained = version.body["storage"] in ("body", "media")
    loc = ev.locator
    if not retained and ev.quote_sha256 is not None:
        raise validation(
            "a reference-only version cannot carry a quotation digest", version_id=ev.version_id
        )
    if retained and loc.type == "audio":
        _check_audio_locator(txn, store_dir, version, ev)
    if loc.type == "pdf" and not carried and library_index.installed(txn.conn):
        # CP11 S11.1 (decision 0060): page range, offsets and quote digest against the
        # extracted page text, when the PDF has been through library import and the caller
        # may see that text (review F6: text above the caller's rank reads as absent).
        check_pdf_locator(
            txn.conn,
            {"id": ev.version_id, "body": version.body},
            loc,
            ev.quote_sha256,
            lambda record_id: live(txn.get(record_id)) is not None,
        )
    if retained and loc.type in ("text", "json"):
        data = read_version_bytes(txn, store_dir, version.body)
        if data is None:
            raise PrometheosError(
                "source_unavailable", "retained bytes are missing or corrupt", {"id": ev.version_id}
            )
        if loc.type == "text":
            quoted = quote_lines(data, loc.start_line, loc.end_line)
            if quoted is None:
                raise validation("text locator is outside the version", version_id=ev.version_id)
            if (
                ev.quote_sha256 is not None
                and hashlib.sha256(quoted).hexdigest() != ev.quote_sha256
            ):
                raise validation("quote digest does not match the cited lines")
        else:
            try:
                resolve_pointer(load_source_json(data), loc.pointer)
            except (CanonicalJSONError, KeyError, IndexError, ValueError) as exc:
                raise validation("JSON pointer does not resolve in the version") from exc
    return max(SENSITIVITY_ORDER[source.sensitivity], SENSITIVITY_ORDER[version.sensitivity])


def _check_audio_locator(txn: Txn, store_dir: Path, version: Any, ev: EvidenceRef) -> None:
    """An audio span must fit what it cites (decision 0013 item 8): a transcript span must
    overlap its segments (and match the quote digest), a WAV span must lie inside the file."""
    loc = ev.locator
    assert loc.type == "audio"
    body = version.body
    if body.get("payload_schema") == "transcript_v1":
        data = read_version_bytes(txn, store_dir, body)
        if data is None:
            raise PrometheosError(
                "source_unavailable", "retained bytes are missing or corrupt", {"id": ev.version_id}
            )
        segments = transcripts.validate_locator(
            transcripts.parse_payload(data), loc.start_ms, loc.end_ms
        )
        if ev.quote_sha256 is not None and transcripts.quote_sha256(segments) != ev.quote_sha256:
            raise validation("quote digest does not match the cited transcript span")
        return
    if not str(body.get("mime", "")).lower().startswith("audio/"):
        raise validation(
            "audio locators cite a transcript or a recording", version_id=ev.version_id
        )
    if ev.quote_sha256 is not None:
        raise validation("a recording has no text to quote; cite its transcript")
    if body["storage"] == "media":
        try:
            info = wav.inspect_wav(store_dir / "media" / str(body["storage_key"]))
        except PrometheosError:
            info = None
    else:
        data = read_version_bytes(txn, store_dir, body)
        info = wav.wav_info_bytes(data) if data is not None else None
    if info is None:
        raise validation(
            "this recording's format cannot be checked without decoding; cite its transcript",
            version_id=ev.version_id,
        )
    if loc.end_ms > info.duration_ms:
        raise validation("audio locator extends beyond the recording", duration_ms=info.duration_ms)


def resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    current = document
    for raw in pointer.split("/")[1:]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not part.isdigit() or (part != "0" and part.startswith("0")):
                raise ValueError("bad array index")
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(part)
    return current


def require_ref(txn: Txn, record_id: str, rev: int, kinds: set[str]) -> None:
    kind = kind_of(record_id)
    if kind not in kinds:
        raise validation(f"reference to {kind} is not allowed here", id=record_id)
    if not txn.rev_exists(record_id, rev):
        raise PrometheosError(
            "not_found", "referenced revision does not exist", {"id": record_id, "rev": rev}
        )


def check_quantity(txn: Txn, store_dir: Path, quantity: QuantityBinding) -> int:
    """Validate the evidence and the exact characters a structured quantity is bound to."""
    rank = check_evidence(txn, store_dir, quantity.source_ref)
    version = txn.get(quantity.source_ref.version_id)
    assert version is not None
    data = read_version_bytes(txn, store_dir, version.body)
    if data is None:
        raise PrometheosError(
            "source_unavailable",
            "structured quantities need retained, intact bytes to verify against",
            {"id": quantity.source_ref.version_id},
        )
    q = quantity.model_dump(mode="json")
    loc = quantity.source_ref.locator
    if loc.type == "text":
        lines = text_lines(data)
        if lines is None:
            raise validation("quantity source is not UTF-8 text")
        quantities.verify_text(lines, loc.start_line, loc.end_line, q)
    elif loc.type == "json":
        try:
            document = load_source_json(data)
        except CanonicalJSONError as exc:
            raise validation("quantity source is not strict JSON") from exc
        quantities.verify_json(document, q)
    else:
        raise validation("structured quantities need a text or JSON locator")
    return rank


def _evidence_lists(model: BaseModel) -> list[EvidenceRef]:
    if isinstance(model, EntryBody | ClaimBody | CorrectionBody | ItemBody):
        return list(model.evidence)
    if isinstance(model, AssessmentBody):
        return [ev for check in model.checks for ev in check.evidence]
    if isinstance(model, ActionBody):
        return list(model.completion_evidence)
    if isinstance(model, AnchorBody):
        return list(model.evidence)
    if isinstance(model, PaperBody):
        found = [ev for row in model.requirements for ev in (row.source_ref, *row.evidence)]
        for review_round in model.review_rounds:
            for item in review_round.items:
                found += [item.comment_source_ref, *item.resolution_evidence]
        return found
    return []


def check_body_references(
    txn: Txn,
    store_dir: Path,
    kind: str,
    model: BaseModel,
    carried: frozenset[str] = frozenset(),
) -> int:
    """Validate cross-record references; return the highest cited sensitivity rank.
    ``carried``: evidence keys of the base revision (a revise), see :func:`check_evidence`."""
    highest = 0
    for ev in _evidence_lists(model):
        same = evidence_key(ev.model_dump(mode="json")) in carried
        highest = max(highest, check_evidence(txn, store_dir, ev, carried=same))
    if isinstance(model, EntryBody):
        for q in model.quantities:
            highest = max(highest, check_quantity(txn, store_dir, q))
        for ref in model.claim_refs:
            require_ref(txn, ref.id, ref.rev, {"claim"})
        if model.author_ref is not None:
            require_ref(txn, model.author_ref.id, model.author_ref.rev, {"person"})
        if model.superseded_by is not None:
            ref = model.superseded_by
            require_ref(txn, ref.id, ref.rev, {"entry"})
    elif isinstance(model, ClaimBody):
        # A06.1 (CP20.1): a claim's own quantities are bound like an entry's, and its one-line
        # summary may state only numbers it binds.
        for q in model.quantities:
            highest = max(highest, check_quantity(txn, store_dir, q))
        if model.summary is not None:
            loose = quantities.unbound_numbers(
                model.summary, [q.model_dump() for q in model.quantities]
            )
            if loose:
                raise validation(
                    "a claim summary states numbers that are not bound quantities of the claim",
                    numbers=loose[:20],
                )
    elif isinstance(model, ExecutionBody):
        for ref in model.receipt_refs:
            require_ref(txn, ref.id, ref.rev, {"version"})
            version = live(txn.get(ref.id))
            if version is None or version.body.get("payload_schema") != "execution_receipt_v1":
                raise validation(
                    "receipt_refs must name execution-receipt source versions", id=ref.id
                )
            highest = max(highest, SENSITIVITY_ORDER[version.sensitivity])
    elif isinstance(model, ProjectSettings) and model.objective_entry_ref is not None:
        ref = model.objective_entry_ref
        require_ref(txn, ref.id, ref.rev, {"entry"})
    return highest


# ----- operations ----------------------------------------------------------------


def _check_sensitivity(requested: str, cited_rank: int) -> None:
    if SENSITIVITY_ORDER[requested] < cited_rank:
        raise PrometheosError(
            "policy_denied",
            "derived records inherit the highest sensitivity of their evidence",
            {"requested": requested},
        )


def _exists_at_all(txn: Txn, record_id: str) -> bool:
    return (
        txn.conn.execute("SELECT 1 FROM records WHERE id = ?", (record_id,)).fetchone() is not None
    )


REVIEWED_RESULT_EFFECT = (
    "reviewed entry or researcher assessment citing an execution result (leaves the queue)"
)


def _lookup(txn: Txn) -> results.Lookup:
    """Existing records by ID, not filtered by the caller's rank (see ``results.Lookup``)."""

    def find(record_id: str) -> tuple[str, dict[str, Any]] | None:
        record = current_record(txn.conn, record_id)
        return None if record is None else (record["kind"], record["body"])

    return find


def is_result(txn: Txn, record_id: str) -> bool:
    found = _lookup(txn)(record_id)
    return found is not None and results.is_execution_result(*found)


def entry_results(txn: Txn, entry_id: str | None, body: dict[str, Any]) -> set[str]:
    """The execution results an entry body (and its live ``cites`` relations) cites: the same
    definition the awaiting-interpretation queue uses (``results.cited_result_ids``).

    A reviewed entry that cites a result takes it out of the queue, which the researcher
    decides (spec/05, decision 0018): creating one, marking an entry reviewed, or revising a
    reviewed entry so that it cites more is a protected effect, so an agent without a grant
    gets ``approval_required`` and nothing leaves the queue silently."""
    targets: list[str] = []
    if entry_id is not None:
        targets = [
            r[0]
            for r in txn.conn.execute(
                "SELECT target_id FROM relations WHERE source_id = ? AND relation = 'cites' "
                "AND retired_seq IS NULL",
                (entry_id,),
            )
        ]
    return results.cited_result_ids(_lookup(txn), body, targets)


def op_create(txn: Txn, store_dir: Path, op: CreateOp) -> list[str]:
    if op.kind in IMPORTER_ONLY:
        how = (
            "their own commands"
            if op.kind in ("task", "job", "run", "package")
            else ("library import" if op.kind == "work" else "source import")
        )
        raise PrometheosError(
            "policy_denied", f"{op.kind} records are registered only through {how}", {}
        )
    if op.kind not in APPLY_CREATABLE:
        raise PrometheosError("unsupported", f"records of kind {op.kind} are not available yet", {})
    if _exists_at_all(txn, op.id):
        raise PrometheosError("revision_conflict", "record already exists", {"id": op.id})
    model = validate_body(op.kind, dict(op.body))
    rank = check_body_references(txn, store_dir, op.kind, model)
    protected, rank = research.check_create(txn, model, op.id, op.sensitivity, rank)
    if (
        isinstance(model, EntryBody)
        and model.review == "reviewed"
        and entry_results(txn, None, model.model_dump(mode="json"))
    ):
        protected.append(REVIEWED_RESULT_EFFECT)
    protected, rank = _check_paper_kinds(txn, store_dir, op.id, model, None, protected, rank)
    protected, rank = _check_matrix_kinds(txn, store_dir, op.id, model, None, protected, rank)
    _check_sensitivity(op.sensitivity, rank)
    if isinstance(model, EntryBody) and model.origin == "generated":
        loose = quantities.unbound_numbers(model.text, [q.model_dump() for q in model.quantities])
        if loose:
            raise validation(
                "generated text states numbers without structured quantity bindings",
                numbers=loose[:20],
            )
    body = model.model_dump(mode="json")
    body_id = txn.insert_body(body)
    txn.write_revision(
        op.id, op.kind, 1, body_id, op.sensitivity, "active", op.occurred_at, "create", body=body
    )
    if op.kind == "action":  # every action is a work item (A05; decision 0035, N1)
        outbox.work_item_created(txn, op.id, body)
    txn.result(op.id, 1)
    return protected


def _check_paper_kinds(
    txn: Txn,
    store_dir: Path,
    record_id: str,
    model: BaseModel,
    previous: Any,
    protected: list[str],
    rank: int,
) -> tuple[list[str], int]:
    """CP7 paper/anchor rules shared by create and revise (decision 0007)."""
    if isinstance(model, PaperBody):
        extra, paper_rank = paper_rules.check_paper(txn, record_id, model, previous)
        return protected + extra, max(rank, paper_rank)
    if isinstance(model, AnchorBody):
        return protected, max(rank, paper_rules.check_anchor(txn, store_dir, model, previous))
    return protected, rank


def _check_matrix_kinds(
    txn: Txn,
    store_dir: Path,
    record_id: str,
    model: BaseModel,
    previous: Any,
    protected: list[str],
    rank: int,
) -> tuple[list[str], int]:
    """CP11 S11.4 grounding, shared by create and revise (decision 0064)."""
    if isinstance(model, MatrixBody):
        return protected, max(rank, grounding.check_matrix(txn, model, previous))
    if isinstance(model, ExtractionBody):
        extra, cited = grounding.check_extraction(txn, store_dir, record_id, model, previous)
        return protected + extra, max(rank, cited)
    return protected, rank


def _current(txn: Txn, record_id: str, base_rev: int) -> Any:
    state = txn.get(record_id)
    if state is None:
        raise PrometheosError("not_found", "record does not exist", {"id": record_id})
    if state.rev != base_rev:
        raise PrometheosError(
            "revision_conflict",
            "base_rev does not match the current revision",
            {"id": record_id, "base_rev": base_rev, "current_rev": state.rev},
        )
    if state.lifecycle == "deleted":
        raise PrometheosError("validation_error", "purged records cannot change", {"id": record_id})
    return state


def op_revise(txn: Txn, store_dir: Path, op: ReviseOp) -> list[str]:
    state = _current(txn, op.id, op.base_rev)
    protected: list[str] = []
    if state.kind == "project":
        raise validation("use project_update to change project settings")
    if state.kind == "version":
        raise PrometheosError("policy_denied", "source versions are immutable", {"id": op.id})
    if state.kind in SERVICE_MANAGED:
        # Their transitions (task submit/cancel, ...) carry invariants that only the
        # dedicated service functions check; a generic revision could bypass them.
        raise PrometheosError(
            "policy_denied",
            f"{state.kind} records change only through their own commands",
            {"id": op.id},
        )
    if SENSITIVITY_ORDER[op.sensitivity] < SENSITIVITY_ORDER[state.sensitivity]:
        protected.append("lower sensitivity (expands egress)")
    elif op.sensitivity != state.sensitivity:
        # Raising hides the record (and everything derived from it) from agent views,
        # which could silently remove a contradiction; it is the owner's decision too.
        protected.append("raise sensitivity (hides from agent views)")
    model = validate_body(state.kind, dict(op.body))
    if isinstance(model, SourceBody):
        fixed = ("origin", "origin_uri", "root_binding", "retention")
        current = SourceBody.model_validate(state.body)
        if any(getattr(model, f) != getattr(current, f) for f in fixed):
            raise PrometheosError(
                "policy_denied", "source origin, binding and retention are immutable", {"id": op.id}
            )
    rank = check_body_references(txn, store_dir, state.kind, model, carried_evidence(state.body))
    if state.kind in research.IMMUTABLE_ON_REVISE or state.kind in (
        "person",
        "meeting",
        "execution",
    ):
        protected += research.check_revise(txn, state, model, op.sensitivity)
    protected, rank = _check_paper_kinds(txn, store_dir, op.id, model, state, protected, rank)
    before_matrix = len(protected)
    protected, rank = _check_matrix_kinds(txn, store_dir, op.id, model, state, protected, rank)
    rereviewed = isinstance(model, ExtractionBody) and len(protected) > before_matrix
    if isinstance(model, EntryBody) and model.review == "reviewed":
        before = (
            entry_results(txn, op.id, state.body)
            if state.body.get("review") == "reviewed"
            else set()
        )
        after = entry_results(txn, op.id, model.model_dump(mode="json"))
        if after - before:  # a reviewed entry that cites a result it did not cite before
            protected.append(REVIEWED_RESULT_EFFECT)
    _check_sensitivity(op.sensitivity, rank)
    body = model.model_dump(mode="json")
    body_id = txn.insert_body(body)
    txn.write_revision(
        op.id,
        state.kind,
        state.rev + 1,
        body_id,
        op.sensitivity,
        state.lifecycle,
        op.occurred_at,
        "revise",
        body=body,
    )
    if state.kind == "action":
        outbox.work_item_changed(txn, op.id, state.body, body)
    if rereviewed:
        # Only the owner's review of a row clears its needs-review flags (decision 0064): an
        # ordinary revision of a row keeps them (``Txn.write_revision``).
        derived.clear_flags(txn.conn, op.id, txn.seq)
    if state.kind == "item" and state.body.get("status") == "adopted":
        derived.flag_dependents(txn.conn, [op.id], "adopted item revised", op.id, txn.seq)
    if (
        state.kind == "entry"
        and state.body.get("category") == "assumption"
        and (body.get("text"), body.get("title"))
        != (state.body.get("text"), state.body.get("title"))
    ):
        _flag_revised_assumption(txn, op.id)
    if SENSITIVITY_ORDER[op.sensitivity] > SENSITIVITY_ORDER[state.sensitivity]:
        raise_derived_sensitivity(txn, op.id, op.sensitivity)
    txn.result(op.id, state.rev + 1)
    return protected


def _flag_revised_assumption(txn: Txn, record_id: str) -> None:
    """Revising an assumption's text flags what relied on an earlier revision, anchors
    included, in the same transaction (spec/05, decision 0018). Every dependent that exists was
    recorded against an earlier revision (none can name one not yet written), so each is
    stale; one recorded afterwards is bound to the new revision and is not touched."""
    closure = derived.affected_closure(txn.conn, [record_id], {record_id})
    paper_rules.flag_anchors_for_correction(
        txn,
        record_id,
        {record_id: "text revised"},
        closure,
        prefix=f"assumption {record_id} revised",
    )
    derived.flag_closure(txn.conn, closure, "assumption text revised", record_id, txn.seq)


def raise_derived_sensitivity(txn: Txn, root_id: str, sensitivity: str) -> list[str]:
    """Derived records inherit the highest sensitivity of what they derive from.

    When a record becomes more sensitive, every version of it (for a source) and every
    record that references it or relates to it (transitively, through the derived
    reference index and live relations) is
    raised to at least the same level in this transaction, so nothing that quotes or
    cites it stays visible at a lower level. Bodies are unchanged; each raised record
    gets a new revision. The project record is never changed.
    """
    rank = SENSITIVITY_ORDER[sensitivity]
    raised: list[str] = []
    frontier, seen = [root_id], {root_id}
    while frontier:
        current = frontier.pop()
        dependents = {
            r[0] for r in txn.conn.execute("SELECT from_id FROM refs WHERE to_id = ?", (current,))
        }
        dependents |= {
            r[0]
            for r in txn.conn.execute(
                # The research-map relations are descriptive (A06.1; review cp20-2-a F4): a claim
                # does not derive from the question it answers or the track it draws on.
                "SELECT source_id FROM relations WHERE target_id = ? AND retired_seq IS NULL "
                "AND relation NOT IN ('answers', 'addresses', 'draws_on')",
                (current,),
            )
        }
        if current.startswith("source_"):
            for row in txn.conn.execute(
                "SELECT r.id, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id "
                "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
                "WHERE r.kind = 'version'"
            ):
                if row[1] and loads_internal(row[1]).get("source_id") == current:
                    dependents.add(row[0])
        for dep in sorted(dependents - seen):
            seen.add(dep)
            row = txn.conn.execute(
                "SELECT r.kind, r.current_rev, r.sensitivity, r.lifecycle, v.body_id, "
                "v.occurred_at, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id "
                "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.id = ?",
                (dep,),
            ).fetchone()
            if row is None or row[0] == "project":
                continue
            frontier.append(dep)
            if SENSITIVITY_ORDER[row[2]] >= rank:
                continue
            body = loads_internal(row[6]) if row[6] else None
            txn.write_revision(
                dep,
                row[0],
                row[1] + 1,
                row[4],
                sensitivity,
                row[3],
                row[5],
                "sensitivity_inherited",
                body=body,
            )
            raised.append(dep)
    return raised


def op_archive(txn: Txn, op: ArchiveOp) -> list[str]:
    state = _current(txn, op.id, op.base_rev)
    if state.kind == "project":
        raise validation("projects cannot be archived through apply")
    if state.kind in SERVICE_MANAGED:
        raise PrometheosError(
            "policy_denied",
            f"{state.kind} records change only through their own commands",
            {"id": op.id},
        )
    if state.lifecycle != "active":
        raise validation("only active records can be archived", id=op.id)
    occurred = txn.conn.execute(
        "SELECT occurred_at FROM revisions WHERE record_id = ? AND rev = ?", (op.id, state.rev)
    ).fetchone()[0]
    txn.write_revision(
        op.id,
        state.kind,
        state.rev + 1,
        state.body_id,
        state.sensitivity,
        "archived",
        occurred,
        "archive",
        body=state.body,
    )
    txn.result(op.id, state.rev + 1)
    return ["archive (hides the record from current views)"]


def op_transition(txn: Txn, store_dir: Path, op: TransitionOp) -> list[str]:
    state = _current(txn, op.id, op.base_rev)
    rank = 0
    for ev in op.evidence:
        rank = max(rank, check_evidence(txn, store_dir, ev))
    _check_sensitivity(state.sensitivity, rank)
    if state.kind == "paper":
        protected = paper_rules.paper_transition(state, op.to, op.reason)
        body = dict(state.body, status=op.to)
    elif state.kind == "anchor":
        protected = paper_rules.anchor_transition(state, op.to, op.reason)
        body = paper_rules.anchor_body_after(state, op.to, op.reason)
    else:
        body, protected = research.transition(txn, store_dir, state, op.to, op.reason, op.evidence)
    model = validate_body(state.kind, body)
    body = model.model_dump(mode="json")
    body_id = txn.insert_body(body)
    txn.write_revision(
        op.id,
        state.kind,
        state.rev + 1,
        body_id,
        state.sensitivity,
        state.lifecycle,
        None,
        f"transition:{op.to}",
        body=body,
    )
    field = research.STATE_FIELD[state.kind]
    txn.conn.execute(
        "INSERT INTO transition_notes (record_id, rev, from_state, to_state, reason, evidence, "
        "protected, commit_seq) VALUES (?,?,?,?,?,?,?,?)",
        (
            op.id,
            state.rev + 1,
            state.body[field],
            op.to,
            op.reason,
            dumps_canonical([e.model_dump(mode="json") for e in op.evidence]).decode(),
            dumps_canonical(protected).decode(),
            txn.seq,
        ),
    )
    if state.kind == "action":
        outbox.work_item_changed(txn, op.id, state.body, body)
    txn.result(op.id, state.rev + 1)
    return protected


def op_correction_apply(txn: Txn, op: CorrectionApplyOp) -> list[str]:
    correction = _current(txn, op.id, op.base_rev)
    if correction.kind != "correction":
        raise validation("correction_apply needs a correction record", id=op.id)
    updates = research.apply_correction(txn, correction)
    for target, new_body in updates:
        model = validate_body(target.kind, new_body)
        body = model.model_dump(mode="json")
        body_id = txn.insert_body(body)
        # Replacement text comes from the correction, so a target whose text is replaced
        # inherits the correction's sensitivity; state-only effects copy no content.
        copies_text = new_body.get("text") != target.body.get("text")
        sensitivity = (
            max(target.sensitivity, correction.sensitivity, key=lambda s: SENSITIVITY_ORDER[s])
            if copies_text
            else target.sensitivity
        )
        txn.write_revision(
            target.id,
            target.kind,
            target.rev + 1,
            body_id,
            sensitivity,
            target.lifecycle,
            None,
            "correction_effect",
            body=body,
        )
        if sensitivity != target.sensitivity:
            raise_derived_sensitivity(txn, target.id, sensitivity)
    # The affected closure (decision 0018): direct dependents as before, then whatever relies
    # on them (an assumption reaches its claims, and through them anchors, papers, actions and
    # notes). Anchors move to needs_review and every member gets a review flag, in this same
    # transaction as the effects themselves.
    effects = {e["target_ref"]["id"]: e["effect"] for e in correction.body["effects"]}
    excluded = {op.id, *effects} | {r["id"] for r in correction.body["replacement_refs"]}
    closure = derived.affected_closure(txn.conn, [t.id for t, _ in updates], excluded)
    paper_rules.flag_anchors_for_correction(txn, op.id, effects, closure)
    applied = dict(correction.body, status="applied", applied_seq=txn.seq)
    body = validate_body("correction", applied).model_dump(mode="json")
    body_id = txn.insert_body(body)
    txn.write_revision(
        op.id,
        "correction",
        correction.rev + 1,
        body_id,
        correction.sensitivity,
        correction.lifecycle,
        None,
        "correction_apply",
        body=body,
    )
    derived.flag_closure(txn.conn, closure, "applied correction", op.id, txn.seq)
    # The event type keeps the kinds of its CP18.2 definition (spec/04: never redefined); matrix
    # rows (CP11 S11.4) are flagged like every member but are not counted in it (decision 0065).
    counted = [d for d in closure if kind_of(d) in EVENT_DEPENDENT_KINDS]
    if counted:
        counts: dict[str, int] = {}
        for dependent in counted:
            counts[kind_of(dependent)] = counts.get(kind_of(dependent), 0) + 1
        outbox.emit_txn(
            txn,
            "correction.flagged_dependents",
            {"kind": "correction", "id": op.id},
            {"dependents": dict(sorted(counts.items())), "total": len(counted)},
        )
    txn.result(op.id, correction.rev + 1)
    return ["apply correction"]


def _check_map_ends(txn: Txn, op: RelationAddOp, source_kind: str, target_kind: str) -> None:
    """The entry ends of a research-map relation have the categories it needs; ``addresses``
    to an entry names the project's current objective entry."""
    source_category, target_category = MAP_CATEGORIES[op.relation]
    for ref in (op.source_ref, op.target_ref):
        # A link to an archived record would never show and cannot be removed (review
        # cp20-2-a F7).
        state = txn.get(ref.id)
        if state is None or state.lifecycle != "active":
            raise validation(f"{op.relation} links active records only", record=ref.id)
    for ref, kind, category in (
        (op.source_ref, source_kind, source_category),
        (op.target_ref, target_kind, target_category),
    ):
        if kind != "entry" or category is None:
            continue
        record = txn.get(ref.id)
        if record is None or record.body.get("category") != category:
            raise validation(f"{op.relation} needs a {category} entry at this end", record=ref.id)
    if op.relation == "addresses" and target_kind == "entry":
        row = txn.conn.execute("SELECT id FROM records WHERE kind = 'project' LIMIT 1").fetchone()
        project = txn.get(row[0]) if row else None
        objective = (project.body.get("objective_entry_ref") or {}) if project else {}
        if objective.get("id") != op.target_ref.id:
            raise validation(
                "a question addresses a paper or the project's objective entry",
                target=op.target_ref.id,
            )


def op_relation_add(txn: Txn, op: RelationAddOp) -> list[str]:
    if op.relation == "version_of":
        # CP11 S11.2 (review F2): recorded by library import (identifiers that name each other)
        # or by the owner's dedup resolve, never by an ordinary transaction.
        raise PrometheosError(
            "policy_denied",
            "version_of is recorded by library import or the owner's dedup resolve",
            {},
        )
    source_kind, target_kind = kind_of(op.source_ref.id), kind_of(op.target_ref.id)
    if not relation_pair_allowed(op.relation, source_kind, target_kind):
        raise validation(
            f"{op.relation} does not relate {source_kind} to {target_kind}",
            source=op.source_ref.id,
            target=op.target_ref.id,
        )
    require_ref(txn, op.source_ref.id, op.source_ref.rev, {source_kind})
    require_ref(txn, op.target_ref.id, op.target_ref.rev, {target_kind})
    if op.source_ref.id == op.target_ref.id:
        raise validation("a record cannot relate to itself")
    if op.relation == "depends_on" and target_kind == "entry":
        target = txn.get(op.target_ref.id)
        if target is None or target.body.get("category") != "assumption":
            raise validation(
                "claims and entries depend only on assumption entries", target=op.target_ref.id
            )
    if op.relation in MAP_CATEGORIES:
        _check_map_ends(txn, op, source_kind, target_kind)
    txn.conn.execute(
        "INSERT INTO relations (source_id, source_rev, target_id, target_rev, relation, "
        "created_seq) VALUES (?,?,?,?,?,?)",
        (
            op.source_ref.id,
            op.source_ref.rev,
            op.target_ref.id,
            op.target_ref.rev,
            op.relation,
            txn.seq,
        ),
    )
    txn.audit(f"relation_add:{op.relation}", op.source_ref.id, op.source_ref.rev)
    txn.result(None, None)
    if op.relation == "cites" and source_kind in ("entry", "assessment"):
        source = txn.get(op.source_ref.id)
        # A citation from a record that speaks for the researcher (a reviewed entry, a
        # researcher-method assessment) to a result is the owner's decision, whoever adds the
        # link: otherwise an agent could credit the researcher with an interpretation. An agent's
        # own (reported_agent) assessment may cite freely; Now shows the owner that it did.
        speaks_for_researcher = source is not None and (
            source.body.get("review") == "reviewed" or source.body.get("method") == "researcher"
        )
        if speaks_for_researcher and is_result(txn, op.target_ref.id):
            return [REVIEWED_RESULT_EFFECT]
    return []


def op_project_update(txn: Txn, store_dir: Path, project_id: str, op: ProjectUpdateOp) -> list[str]:
    state = _current(txn, project_id, op.base_rev)
    model = validate_body("project", dict(op.settings))
    check_body_references(txn, store_dir, "project", model)
    body = model.model_dump(mode="json")
    body_id = txn.insert_body(body)
    txn.write_revision(
        project_id,
        "project",
        state.rev + 1,
        body_id,
        state.sensitivity,
        "active",
        None,
        "project_update",
        body=body,
    )
    txn.result(project_id, state.rev + 1)
    effects = ["project settings change"]
    before = state.body.get("verification_budget")
    if before != body.get("verification_budget"):
        # Review cp20-3-a F4: the owner sees a budget change by name, with both values.
        effects.append(f"verification budget change: {_budget_text(before)} -> "
                       f"{_budget_text(body.get('verification_budget'))}")  # fmt: skip
    return effects


def _budget_text(budget: dict[str, Any] | None) -> str:
    from prometheos.domain.bodies import DEFAULT_VERIFICATION_BUDGET

    b = budget or DEFAULT_VERIFICATION_BUDGET.model_dump(mode="json")
    folder = f", output {b['output_root_alias']}/{b['output_path']}" if b.get("output_path") else ""
    return f"{b['max_minutes_each']} min each, {b['max_minutes_per_day']} min a day{folder}"


def plan_digest(raw_value: dict[str, Any]) -> str:
    """What an owner approves: project, expected sequence and exact operations.

    The request ID is excluded so the submitting client may choose it; the expected
    sequence is included, so an approval cannot be replayed after the project moved.
    """
    return canonical_sha256({k: v for k, v in raw_value.items() if k != "request_id"})


class ApprovalNeeded(Exception):
    """Raised inside the commit handler (rolling the transaction back) when a request with
    protected effects arrives without any capability and the caller asked for an approval
    request instead of a refusal (A02, CP10.2)."""

    def __init__(self, protected: list[str]) -> None:
        super().__init__("approval needed")
        self.protected = protected


def _handler(
    request: Request,
    store: ProjectStore,
    capability: capabilities.Secret,
    digest: str,
    collected: list[str] | None,
    owner_local: str | None = None,
    request_approval: bool = False,
) -> Any:
    def handler(txn: Txn) -> None:
        protected: list[str] = []
        for index, op in enumerate(request.operations):
            txn.op_index = index
            try:
                if isinstance(op, CreateOp):
                    protected += op_create(txn, store.directory, op)
                elif isinstance(op, ReviseOp):
                    protected += op_revise(txn, store.directory, op)
                elif isinstance(op, ArchiveOp):
                    protected += op_archive(txn, op)
                elif isinstance(op, TransitionOp):
                    protected += op_transition(txn, store.directory, op)
                elif isinstance(op, CorrectionApplyOp):
                    protected += op_correction_apply(txn, op)
                elif isinstance(op, RelationAddOp):
                    protected += op_relation_add(txn, op)
                elif isinstance(op, ProjectUpdateOp):
                    protected += op_project_update(txn, store.directory, store.project_id, op)
            except PrometheosError as exc:
                if exc.code not in TYPED_ERROR_DETAILS:  # closed details admit no op_index
                    exc.details.setdefault("op_index", index)
                raise
        if collected is not None:
            collected.extend(protected)
            return
        if protected and owner_local is not None:
            # In-process owner action already authorised by its own owner gate (e.g. the
            # explicit --rebind of a full-data restore). Recorded, never available to
            # transports.
            txn.audit(f"owner_local_action:{owner_local}", None, None)
        elif protected and capability is None and request_approval:
            raise ApprovalNeeded(protected)
        elif protected:
            capability_id = capabilities.consume(
                txn.conn,
                secret=capability,
                project_id=store.project_id,
                operation="exact_request",
                digest=digest,
            )
            txn.audit(f"owner_decision:{capability_id}", None, None)

    return handler


def apply_request(
    store: ProjectStore,
    raw: bytes,
    actor: str,
    max_rank: int = 0,
    capability: capabilities.Secret = None,
    owner_local: str | None = None,
    request_key_sha256: str | None = None,
    request_approval: bool = False,
    approval_actor: str | None = None,
) -> dict[str, Any]:
    """``max_rank`` is the caller's visibility ceiling (agent scope: 0, normal only).

    ``capability`` is transport metadata (never hashed or stored): an owner decision
    bound to this request's plan digest, required when any protected effect occurs.

    ``request_key_sha256`` replaces the request-body digest in the stored request key,
    for conveniences whose body depends on the snapshot and which are retried through
    :meth:`ProjectStore.prior_response` (a saved paper check). Leave it unset otherwise.

    ``request_approval`` (A02, CP10.2): a request whose protected effects arrive with no
    capability at all stores its exact plan as an approval request and raises
    ``approval_required`` instead of ``policy_denied``; nothing commits. A capability that
    is presented and fails is still ``policy_denied``.
    """
    request, digest, value = _parse_with_value(raw)
    if request.project_id != store.project_id:
        raise PrometheosError(
            "validation_error",
            "request targets a different project",
            {"project_id": request.project_id},
        )
    try:
        return store.commit(
            request_id=request.request_id,
            request_sha256=request_key_sha256 or digest,
            expected_seq=request.expected_seq,
            actor=actor,
            handler=_handler(
                request,
                store,
                capability,
                plan_digest(value),
                None,
                owner_local,
                request_approval and request_key_sha256 is None,
            ),
            max_rank=max_rank,
        )
    except ApprovalNeeded as needed:
        from prometheos.app import approvals

        plan = {k: v for k, v in value.items() if k != "request_id"}
        row = approvals.create(
            store,
            operation="apply",
            caller_rank=max_rank,
            actor=approval_actor or actor,
            request_id=request.request_id,
            plan=plan,
            required_scope="exact_request",
            summary=list(dict.fromkeys(needed.protected)),
        )
        raise approvals.answer(row) from None


def preview(store: ProjectStore, raw: bytes, max_rank: int = 0) -> dict[str, Any]:
    """Pure dry run: protected effects, validity and the digest an owner would approve."""
    request, _, value = _parse_with_value(raw)
    if request.project_id != store.project_id:
        raise PrometheosError("validation_error", "request targets a different project", {})
    protected: list[str] = []
    error = None
    try:
        store.dry_run(
            expected_seq=request.expected_seq,
            handler=_handler(request, store, None, "", protected),
            max_rank=max_rank,
        )
    except PrometheosError as exc:
        error = exc.to_response().model_dump(mode="json")["error"]
    root_changes: list[dict[str, Any]] = []
    if error is None and max_rank >= 2:
        conn = store.connect(readonly=True)
        try:
            current = current_record(conn, store.project_id)
        finally:
            conn.close()
        assert current is not None
        old: dict[str, str] = {r["alias"]: r["path"] for r in current["body"]["allowed_roots"]}
        for op in request.operations:
            if isinstance(op, ProjectUpdateOp):
                settings = ProjectSettings.model_validate(op.settings)
                new = {r.alias: r.path for r in settings.allowed_roots}
                root_changes.extend(
                    {"alias": alias, "old_path": old.get(alias), "new_path": new.get(alias)}
                    for alias in sorted(old.keys() | new.keys())
                    if old.get(alias) != new.get(alias)
                )
                old = new
    return {
        "digest": plan_digest(value),
        "expected_seq": request.expected_seq,
        "operations": [op.op for op in request.operations],
        "protected_effects": protected,
        "root_changes": root_changes,
        "would_apply": error is None,
        "error": error,
    }


def _parse_with_value(raw: bytes) -> tuple[Request, str, dict[str, Any]]:
    request, digest = parse_request(raw)
    value = loads_strict(raw)
    return request, digest, value
