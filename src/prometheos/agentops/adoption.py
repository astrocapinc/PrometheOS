"""Adoption of an existing folder: read operations (A05, CP17; decision 0039).

MCP family tool ``adoption_read``. The scan itself is the hand-written long operation
``adoption_scan`` (an operation run). Replies name files by path relative to the adoption
root; nothing here reads the adopted folder, only what a scan stored.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue

from prometheos.adoption.assist import TaskKind
from prometheos.adoption.contracts import (
    AnalyzerId,
    Band,
    Container,
    FileStatus,
    Predicate,
    Tier,
    Zone,
)
from prometheos.agentops import Ctx, Params, RequestId, RootAlias, family, operation
from prometheos.domain.models import MAX_INT

family(
    "adoption_read",
    "read",
    "Adoption of an existing folder (an approved research root): the scan generations with "
    "their counts and root digests, and the inventory rows of one scan (path, size, digests, "
    "container, zone, and what could not be read and why). Reads stored state only.",
)

ScanGenArg = Annotated[int, Field(ge=1, le=MAX_INT, description="Scan generation number")]


class ScansParams(Params):
    root_alias: RootAlias | None = None


@operation(
    "adoption_scans",
    ScansParams,
    "Scan generations, newest first, with state, counts and root digests",
    tool="adoption_read",
    sub="scans",
    http="GET /api/v1/adoption/scans",
)
def adoption_scans(ctx: Ctx, p: ScansParams) -> Any:
    return ctx.service.adoption_scans(ctx.project_id, ctx.caller, p.root_alias)


class FilesParams(Params):
    scan_gen: ScanGenArg
    status: FileStatus | None = None
    zone: Zone | None = None
    container: Container | None = None
    prefix: Annotated[str, Field(min_length=1, max_length=1024)] | None = None
    offset: Annotated[int, Field(ge=0, le=MAX_INT)] = 0
    limit: Annotated[int, Field(ge=1, le=500)] = 200


@operation(
    "adoption_files",
    FilesParams,
    "Inventory rows of one scan, paged and filtered by status, zone, container or path prefix",
    tool="adoption_read",
    sub="files",
    http="GET /api/v1/adoption/scans/{scan_gen}/files",
)
def adoption_files(ctx: Ctx, p: FilesParams) -> Any:
    return ctx.service.adoption_files(
        ctx.project_id, ctx.caller, p.scan_gen, status=p.status, zone=p.zone,
        container=p.container, prefix=p.prefix, offset=p.offset, limit=p.limit,
    )  # fmt: skip


class EvidenceParams(Params):
    scan_gen: ScanGenArg
    path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    predicate: Predicate | None = None
    tier: Tier | None = None
    analyzer: AnalyzerId | None = None
    offset: Annotated[int, Field(ge=0, le=MAX_INT)] = 0
    limit: Annotated[int, Field(ge=1, le=500)] = 200


@operation(
    "adoption_evidence",
    EvidenceParams,
    "Typed evidence of one scan with its tier, analyzer, locator and verbatim quote; a path "
    "selects everything said about that file",
    tool="adoption_read",
    sub="evidence",
    http="GET /api/v1/adoption/scans/{scan_gen}/evidence",
)
def adoption_evidence(ctx: Ctx, p: EvidenceParams) -> Any:
    return ctx.service.adoption_evidence(
        ctx.project_id, ctx.caller, p.scan_gen, path=p.path, predicate=p.predicate,
        tier=p.tier, analyzer=p.analyzer, offset=p.offset, limit=p.limit,
    )  # fmt: skip


class NoParams(Params):
    pass


@operation(
    "adoption_analyzers",
    NoParams,
    "The analyzer declarations: what each reads, the evidence it emits and its tiers",
    tool="adoption_read",
    sub="analyzers",
    http="GET /api/v1/adoption/analyzers",
)
def adoption_analyzers(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.adoption_analyzers(ctx.project_id, ctx.caller)


class FamiliesParams(Params):
    scan_gen: ScanGenArg
    band: Band | None = None
    offset: Annotated[int, Field(ge=0, le=MAX_INT)] = 0
    limit: Annotated[int, Field(ge=1, le=500)] = 200


@operation(
    "adoption_families",
    FamiliesParams,
    "Families of one scan (files that are versions or copies of one thing) with the band and "
    "rule of each ranked current member; ranks are proposals until the owner accepts a baseline",
    tool="adoption_read",
    sub="families",
    http="GET /api/v1/adoption/scans/{scan_gen}/families",
)
def adoption_families(ctx: Ctx, p: FamiliesParams) -> Any:
    return ctx.service.adoption_families(
        ctx.project_id, ctx.caller, p.scan_gen, band=p.band, offset=p.offset, limit=p.limit
    )


class FamilyParams(Params):
    scan_gen: ScanGenArg
    family_id: Annotated[str, Field(pattern=r"^fam_[0-9a-f]{16}$")]


@operation(
    "adoption_family",
    FamilyParams,
    "One family: every member with its rank, outcome and reasons (rule, tier, evidence IDs), "
    "and the conflicts kept",
    tool="adoption_read",
    sub="family",
    http="GET /api/v1/adoption/scans/{scan_gen}/families/{family_id}",
)
def adoption_family(ctx: Ctx, p: FamilyParams) -> Any:
    return ctx.service.adoption_family(ctx.project_id, ctx.caller, p.scan_gen, p.family_id)


# ----- agent and AI analyzers (CP17.4; decision 0042) ----------------------------------------

family(
    "adoption_manage",
    "write_new",
    "Adoption work the coding agent reports: the result of an analysis task prepared with "
    "adoption_read (assist_prepare). PrometheOS re-reads every quote against the file's exact "
    "bytes, checks paths against the inventory and needs agreeing samples for high-impact "
    "tasks; accepted results are proposals that never override stronger evidence.",
)

# Every task kind the service knows, including the research map's two (CP20.4; decision 0107):
# the agent transports offered only the first three, so an agent could not submit them.
TaskKindArg = TaskKind


class AssistTasksParams(Params):
    scan_gen: ScanGenArg


@operation(
    "adoption_assist_tasks",
    AssistTasksParams,
    "Suggested analysis tasks for the agent (status statements in notes, MATLAB dependency "
    "analysis, file roles), most useful first; read-only",
    tool="adoption_read",
    sub="assist_tasks",
    http="GET /api/v1/adoption/scans/{scan_gen}/assist",
)
def adoption_assist_tasks(ctx: Ctx, p: AssistTasksParams) -> Any:
    return ctx.service.adoption_assist_tasks(ctx.project_id, ctx.caller, p.scan_gen)


class AssistPrepareParams(Params):
    scan_gen: ScanGenArg
    task_kind: TaskKindArg
    subject: Annotated[str, Field(min_length=1, max_length=4096)]


@operation(
    "adoption_assist_prepare",
    AssistPrepareParams,
    "One task's input (the file's visible text as numbered data lines, digest-pinned), its "
    "closed output schema and instructions; read-only",
    tool="adoption_read",
    sub="assist_prepare",
    http="POST /api/v1/adoption/assist/prepare",
)
def adoption_assist_prepare(ctx: Ctx, p: AssistPrepareParams) -> Any:
    return ctx.service.adoption_assist_prepare(
        ctx.project_id, ctx.caller, p.scan_gen, p.task_kind, p.subject
    )


class AssistSubmitParams(AssistPrepareParams):
    sample: Annotated[int, Field(ge=1, le=9)] = 1
    model: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    response: dict[str, JsonValue]


@operation(
    "adoption_assist_submit",
    AssistSubmitParams,
    "Submit one sample of a task's result; it is validated (schema, quotes re-read, paths) and, "
    "once enough samples agree, recorded as proposed evidence",
    tool="adoption_manage",
    sub="assist_submit",
    http="POST /api/v1/adoption/assist/submit",
)
def adoption_assist_submit(ctx: Ctx, p: AssistSubmitParams) -> Any:
    return ctx.service.adoption_assist_submit(
        ctx.project_id, ctx.caller, p.scan_gen, p.task_kind, p.subject, p.sample, p.response,
        p.model,
    )  # fmt: skip


# ----- the consolidation plan, owner decisions, baselines (CP17.5; decision 0043) ------------

StepId = Annotated[str, Field(pattern=r"^st_[0-9a-f]{16,64}$")]
FamilyId = Annotated[str, Field(pattern=r"^fam_[0-9a-f]{16}$")]
Note = Annotated[str, Field(min_length=1, max_length=2000)]


class PlanParams(Params):
    scan_gen: ScanGenArg


@operation(
    "adoption_plan",
    PlanParams,
    "The consolidation plan of a scan's folder: each step with who acts, inputs, allowed and "
    "forbidden actions, its result schema, state and work item; read-only",
    tool="adoption_read",
    sub="plan",
    http="GET /api/v1/adoption/scans/{scan_gen}/plan",
)
def adoption_plan(ctx: Ctx, p: PlanParams) -> Any:
    return ctx.service.adoption_plan(ctx.project_id, ctx.caller, p.scan_gen)


@operation(
    "adoption_plan_create",
    PlanParams,
    "Derive a new plan version for a complete scan; each step becomes a proposed work item "
    "(the owner adopts it or not; never start work on an item the owner has not adopted)",
    tool="adoption_manage",
    sub="plan_create",
    http="POST /api/v1/adoption/plan",
)
def adoption_plan_create(ctx: Ctx, p: PlanParams) -> Any:
    return ctx.service.adoption_plan_create(ctx.project_id, ctx.caller, p.scan_gen)


class StepReportParams(Params):
    scan_gen: ScanGenArg
    step_id: StepId
    result: dict[str, JsonValue]


@operation(
    "adoption_step_report",
    StepReportParams,
    "Report one agent step's result in its closed schema; it is checked against the inventory "
    "and becomes validated or rejected (the owner accepts results with a baseline)",
    tool="adoption_manage",
    sub="step_report",
    http="POST /api/v1/adoption/steps/report",
)
def adoption_step_report(ctx: Ctx, p: StepReportParams) -> Any:
    return ctx.service.adoption_step_report(
        ctx.project_id, ctx.caller, p.scan_gen, p.step_id, p.result
    )


class DecideParams(Params):
    scan_gen: ScanGenArg
    family_id: FamilyId
    current: Annotated[str, Field(min_length=1, max_length=4096)]
    note: Note | None = None
    request_id: RequestId | None = None


@operation(
    "adoption_decide",
    DecideParams,
    "The owner's choice of a family's current file (strongest evidence); from an agent this "
    "is an approval request that waits in the hub's Approvals",
    tool="adoption_manage",
    sub="decide",
    http="POST /api/v1/adoption/decide",
)
def adoption_decide(ctx: Ctx, p: DecideParams) -> Any:
    if not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id, ctx.caller, "adoption_decide",
            p.model_dump(mode="json", exclude={"request_id"}), p.request_id,
        )  # fmt: skip
    return ctx.service.adoption_decide(
        ctx.project_id, p.scan_gen, p.family_id, p.current, p.note, authority=ctx.authority
    )


class ClassifyParams(Params):
    scan_gen: ScanGenArg
    family_id: FamilyId
    decision: Literal["variant", "noise", "split", "revoked"]
    note: Note | None = None
    request_id: RequestId | None = None


@operation(
    "adoption_classify",
    ClassifyParams,
    "The owner says what a family is when it is not competition: variant (each file is current "
    "in its own context), noise (none matters) or split (not one family); it files no work item. "
    "From an agent this is an approval request that waits in the hub's Approvals",
    tool="adoption_manage",
    sub="classify",
    http="POST /api/v1/adoption/classify",
)
def adoption_classify(ctx: Ctx, p: ClassifyParams) -> Any:
    if not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id, ctx.caller, "adoption_classify",
            p.model_dump(mode="json", exclude={"request_id"}), p.request_id,
        )  # fmt: skip
    return ctx.service.adoption_classify(
        ctx.project_id, p.scan_gen, p.family_id, p.decision, p.note, authority=ctx.authority
    )


class OutlineParams(Params):
    scan_gen: ScanGenArg


@operation(
    "adoption_outline",
    OutlineParams,
    "The scan's proposed outline: tracks (the folders it forks by), deliverables (LaTeX main "
    "files), questions (lines of notes that ask one), results with their producers, and stale "
    "candidates (results whose producer changed after them), each citing its file and line, "
    "with each question's proposed placement and the outline's SHA-256; read-only",
    tool="adoption_read",
    sub="outline",
    http="GET /api/v1/adoption/scans/{scan_gen}/outline",
)
def adoption_outline(ctx: Ctx, p: OutlineParams) -> Any:
    return ctx.service.adoption_outline(ctx.project_id, ctx.caller, p.scan_gen)


OutlineSha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ItemIdArg = Annotated[str, Field(pattern=r"^oi_[0-9a-f]{16}$")]


class OutlineEdits(Params):
    drop: list[ItemIdArg] = Field(default_factory=list, max_length=400)
    titles: dict[ItemIdArg, Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=dict, max_length=400
    )
    addresses: dict[ItemIdArg, ItemIdArg | Literal["objective"] | None] = Field(
        default_factory=dict, max_length=400
    )
    draws_on: dict[ItemIdArg, list[ItemIdArg]] = Field(default_factory=dict, max_length=400)


class OutlinePreviewParams(Params):
    scan_gen: ScanGenArg
    proposal_sha256: OutlineSha
    edits: OutlineEdits = Field(default_factory=OutlineEdits)


@operation(
    "adoption_outline_preview",
    OutlinePreviewParams,
    "What confirming the outline with these corrections (items left out, new titles, where each "
    "question belongs and which tracks it draws on) would record: counts of records created and "
    "reused and links added; read-only",
    tool="adoption_read",
    sub="outline_preview",
    http="POST /api/v1/adoption/outline/preview",
)
def adoption_outline_preview(ctx: Ctx, p: OutlinePreviewParams) -> Any:
    return ctx.service.adoption_outline_preview(
        ctx.project_id, ctx.caller, p.scan_gen, p.proposal_sha256, p.edits.model_dump(mode="json")
    )


class OutlineConfirmParams(Params):
    scan_gen: ScanGenArg
    proposal_sha256: OutlineSha
    edits: OutlineEdits = Field(default_factory=OutlineEdits)
    note: Note | None = None
    request_id: RequestId | None = None


@operation(
    "adoption_outline_confirm",
    OutlineConfirmParams,
    "The owner confirms the scan's outline once, after correcting it: the kept tracks, questions "
    "and deliverables become records and the questions' placements become links, in one "
    "transaction; nothing is written into the folder. From an agent this is an approval request "
    "that waits in the hub's Approvals",
    tool="adoption_manage",
    sub="outline_confirm",
    http="POST /api/v1/adoption/outline/confirm",
)
def adoption_outline_confirm(ctx: Ctx, p: OutlineConfirmParams) -> Any:
    edits = p.edits.model_dump(mode="json")
    if not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id, ctx.caller, "adoption_outline_confirm",
            {"scan_gen": p.scan_gen, "proposal_sha256": p.proposal_sha256, "edits": edits,
             "note": p.note},
            p.request_id,
        )  # fmt: skip
    return ctx.service.adoption_outline_confirm(
        ctx.project_id, p.scan_gen, p.proposal_sha256, edits, p.note, authority=ctx.authority
    )


class CiteParams(Params):
    baseline_id: Annotated[str, Field(pattern=r"^bl_[0-9a-f]{24}$")]
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    request_id: RequestId | None = None


@operation(
    "adoption_cite",
    CiteParams,
    "Cite a file of an accepted baseline as a reference-only version bound to the digest the "
    "scan recorded, without copying it; it reads as unverified until a source check confirms "
    "its bytes, and an already registered file is never rebound",
    tool="adoption_manage",
    sub="cite",
    http="POST /api/v1/adoption/cite",
)
def adoption_cite(ctx: Ctx, p: CiteParams) -> Any:
    return ctx.service.adoption_cite(
        ctx.project_id, ctx.caller, p.baseline_id, p.path, p.request_id
    )


class BaselineAcceptParams(Params):
    scan_gen: ScanGenArg
    note: Note | None = None
    request_id: RequestId | None = None


@operation(
    "adoption_baseline_accept",
    BaselineAcceptParams,
    "The owner accepts a scan as the folder's baseline (families, current files, decisions, "
    "validated results); from an agent this is an approval request",
    tool="adoption_manage",
    sub="baseline_accept",
    http="POST /api/v1/adoption/baselines",
)
def adoption_baseline_accept(ctx: Ctx, p: BaselineAcceptParams) -> Any:
    if not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id, ctx.caller, "adoption_baseline_accept",
            p.model_dump(mode="json", exclude={"request_id"}), p.request_id,
        )  # fmt: skip
    return ctx.service.adoption_baseline_accept(
        ctx.project_id, p.scan_gen, p.note, authority=ctx.authority
    )


class BaselineParams(Params):
    baseline_id: Annotated[str, Field(pattern=r"^bl_[0-9a-f]{24}$")]


@operation(
    "adoption_baseline",
    BaselineParams,
    "One accepted baseline with its payload and SHA-256; read-only",
    tool="adoption_read",
    sub="baseline",
    http="GET /api/v1/adoption/baselines/{baseline_id}",
)
def adoption_baseline(ctx: Ctx, p: BaselineParams) -> Any:
    return ctx.service.adoption_baseline(ctx.project_id, ctx.caller, p.baseline_id)


# ----- re-adoption, vault import, export (CP17.6; decision 0045) -----------------------------


class DriftParams(Params):
    scan_gen: ScanGenArg
    against: Annotated[str, Field(pattern=r"^(previous|bl_[0-9a-f]{24})$")] = "previous"
    change: Literal["added", "changed", "moved", "deleted"] | None = None
    offset: Annotated[int, Field(ge=0, le=MAX_INT)] = 0
    limit: Annotated[int, Field(ge=1, le=500)] = 200


@operation(
    "adoption_drift",
    DriftParams,
    "What changed in a scan since the previous scan of the folder or since an accepted "
    "baseline: added, changed, moved and deleted files, and each accepted family's current "
    "file; read-only",
    tool="adoption_read",
    sub="drift",
    http="GET /api/v1/adoption/scans/{scan_gen}/drift",
)
def adoption_drift(ctx: Ctx, p: DriftParams) -> Any:
    return ctx.service.adoption_drift(
        ctx.project_id, ctx.caller, p.scan_gen, p.against, p.change, p.offset, p.limit
    )


@operation(
    "adoption_baseline_export",
    BaselineParams,
    "An accepted baseline as a self-checking package (scan, inventory, evidence, decisions) "
    "that the owner can import into another store; read-only",
    tool="adoption_read",
    sub="baseline_export",
    http="GET /api/v1/adoption/baselines/{baseline_id}/export",
)
def adoption_baseline_export(ctx: Ctx, p: BaselineParams) -> Any:
    return ctx.service.adoption_baseline_export(ctx.project_id, ctx.caller, p.baseline_id)


class VaultImportParams(Params):
    root_alias: RootAlias
    offset: Annotated[int, Field(ge=0, le=MAX_INT)] = 0
    retention: Literal["copy", "reference"] | None = None
    sensitivity: Literal["normal", "restricted", "private"] = "normal"


@operation(
    "adoption_vault_import",
    VaultImportParams,
    "Plan a source import of one batch (at most 100) of an approved notes vault's Markdown "
    "notes; nothing is written into the vault; commit the plan with import_commit",
    tool="adoption_manage",
    sub="vault_import",
    http="POST /api/v1/adoption/vault/import",
)
def adoption_vault_import(ctx: Ctx, p: VaultImportParams) -> Any:
    return ctx.service.adoption_vault_import(
        ctx.project_id, ctx.caller, p.root_alias, p.offset, p.retention, p.sensitivity
    )


class ExportMarkdownParams(Params):
    baseline_id: Annotated[str, Field(pattern=r"^bl_[0-9a-f]{24}$")]
    root_alias: RootAlias
    folder: Annotated[str, Field(max_length=512)] = ""
    request_id: RequestId | None = None


@operation(
    "adoption_export_markdown",
    ExportMarkdownParams,
    "Write the baseline into a note (adoption-<folder>.md) in an approved folder, inside "
    "PrometheOS's fenced block; text outside the block is kept; one-way. The owner's act: from "
    "an agent this is an approval request",
    tool="adoption_manage",
    sub="export_markdown",
    http="POST /api/v1/adoption/export/markdown",
)
def adoption_export_markdown(ctx: Ctx, p: ExportMarkdownParams) -> Any:
    if not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id, ctx.caller, "adoption_export_markdown",
            p.model_dump(mode="json", exclude={"request_id"}), p.request_id,
        )  # fmt: skip
    return ctx.service.adoption_export_markdown(
        ctx.project_id, p.baseline_id, p.root_alias, p.folder, authority=ctx.authority
    )


@operation(
    "adoption_baselines",
    ScansParams,
    "Accepted baselines, newest first, optionally of one folder; read-only",
    tool="adoption_read",
    sub="baselines",
    http="GET /api/v1/adoption/baselines",
)
def adoption_baselines(ctx: Ctx, p: ScansParams) -> Any:
    return ctx.service.adoption_baselines(ctx.project_id, ctx.caller, p.root_alias)
