"""Exact MCP tool argument and result schemas (spec/04: advertise exact input/output schemas).

Arguments use the same constraints the services enforce (ID patterns, UUID4 request IDs,
bounded integers, literal enums, the request and task-result models). Results are typed
envelopes whose top-level fields are exact; nested view items that are themselves record
bodies or rendered view rows are typed as JSON objects (their record-body schemas are the
contract schemas under ``contracts/schemas``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, WithJsonSchema

from prometheos.app.context import MAX_BUDGET, MIN_BUDGET
from prometheos.app.tasks import MeetingExtractResult, NextStepsResult, NotebookDraftResult
from prometheos.domain.bodies import CheckResult, ExecutionReceiptV1
from prometheos.domain.ids import RECORD_ID_PATTERN
from prometheos.domain.models import (
    _TS_PATTERN as TS_PATTERN,
)
from prometheos.domain.models import (
    MAX_INT,
    JsonObject,
    RecordEnvelope,
    RecordRef,
    Request,
    StrictModel,
    SuccessResponse,
)
from prometheos.store.operational import RunView

MAX_LIST = 200
UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


def _inline(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$defs`` references so the schema can be embedded in a tool's input
    schema; the recursive JSON value type becomes "any JSON value"."""
    defs = schema.get("$defs", {})

    def walk(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name in seen or name == "JsonValue":
                    return {"description": "any JSON value"}
                return walk(defs[name], seen | {name})
            return {k: walk(v, seen) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v, seen) for v in node]
        return node

    result: dict[str, Any] = walk(schema, frozenset())
    return result


def advertised(base: Any, exact: Any) -> Any:
    """Advertise ``exact``'s JSON Schema but accept ``base`` at the transport, so the
    service performs the (identical) validation and errors keep the CLI's envelope."""
    return Annotated[base, WithJsonSchema(_inline(TypeAdapter(exact).json_schema()))]


RecordIdArg = advertised(str, Annotated[str, Field(pattern=RECORD_ID_PATTERN, max_length=80)])
RequestIdArg = advertised(
    str, Annotated[str, Field(pattern=UUID4, description="Canonical lowercase UUID4")]
)
SeqArg = advertised(int, Annotated[int, Field(ge=0, le=MAX_INT)])
LimitArg = advertised(int, Annotated[int, Field(ge=1, le=MAX_LIST)])
BudgetArg = advertised(int, Annotated[int, Field(ge=MIN_BUDGET, le=MAX_BUDGET)])
ContextPurposeArg = advertised(str, Annotated[str, Field(pattern=r"^[a-z][a-z_-]{0,31}$")])
TaskPurposeArg = advertised(str, Literal["meeting_extract", "notebook_draft", "next_steps"])
SensitivityArg = advertised(str, Literal["normal", "restricted", "private"])
RetentionArg = advertised(str, Literal["copy", "reference"])
KindArg = advertised(
    str,
    Literal[
        "project", "source", "version", "entry", "claim", "assessment", "correction", "person",
        "role", "meeting", "item", "action", "paper", "anchor", "task", "job", "package", "run",
        "execution", "work",
    ],
)  # fmt: skip
RootAliasArg = advertised(str, Annotated[str, Field(min_length=1, max_length=64)])
PathArg = advertised(str, Annotated[str, Field(min_length=1, max_length=1024)])
QueryArg = advertised(str, Annotated[str, Field(min_length=1, max_length=500)])
RequestArg = advertised(dict[str, Any], Request)


class RequestFile(BaseModel):
    """A request JSON file inside an approved research root (decision 0079, P22)."""

    model_config = ConfigDict(extra="forbid")
    root_alias: Annotated[str, Field(min_length=1, max_length=64)]
    path: Annotated[str, Field(min_length=1, max_length=1024)]


RequestFileArg = advertised(dict[str, Any], RequestFile)


def omittable(base: Any, exact: Any) -> Any:
    """Like ``advertised`` for an argument that may be left out: the advertised schema stays the
    exact object schema (no ``anyOf`` with null), and the transport accepts its absence; the
    service decides what an absent argument means (review ux-p22-a F1)."""
    return Annotated[base | None, WithJsonSchema(_inline(TypeAdapter(exact).json_schema()))]


OmittableRequestArg = omittable(dict[str, Any], Request)
OmittableRequestFileArg = omittable(dict[str, Any], RequestFile)
ReceiptArg = advertised(dict[str, Any], ExecutionReceiptV1)
TitleArg = advertised(str, Annotated[str, Field(min_length=1, max_length=300)])
TaskResultArg = advertised(
    dict[str, Any],
    Annotated[
        MeetingExtractResult | NotebookDraftResult | NextStepsResult,
        Field(discriminator="purpose"),
    ],
)

# ----- CP4 workflow tools ---------------------------------------------------------------

StepIdArg = advertised(str, Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")])
PackageRefArg = advertised(
    str, Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:@[0-9A-Za-z.+-]{5,100})?$")]
)
JsonObjectArg = advertised(dict[str, Any], dict[str, JsonValue])
RevArg = advertised(int, Annotated[int, Field(ge=1, le=MAX_INT)])
StepStatusArg = advertised(str, Literal["pending", "in_progress", "blocked", "done", "skipped"])
OutcomeArg = advertised(str, Literal["completed", "cancelled"])
ReasonArg = advertised(str | None, Annotated[str, Field(min_length=1, max_length=2000)] | None)
ArtifactRefsArg = advertised(
    list[dict[str, Any]] | None, Annotated[list[RecordRef], Field(max_length=100)] | None
)
CheckResultsArg = advertised(
    list[dict[str, Any]] | None, Annotated[list[CheckResult], Field(max_length=50)] | None
)


class Out(StrictModel):
    """Result models validate what the services return; extra keys are errors."""


class ProjectInfoOut(Out):
    project_id: str
    project_seq: int
    settings_rev: int
    settings: JsonObject
    record_counts: dict[str, int]
    store: str


class RecordPageOut(Out):
    project_seq: int
    items: list[RecordEnvelope]
    next_cursor: str | None


class ContextOut(Out):
    text: str
    manifest: JsonObject


class SearchOut(Out):
    project_seq: int
    query: str
    mode: str
    hits: list[JsonObject]


class ClaimStatusOut(Out):
    generation: str
    project_seq: int
    counts: dict[str, int]
    claims: list[JsonObject]


class ResearchMapOut(Out):
    generation: str
    project_seq: int
    objective: JsonObject
    deliverables: list[JsonObject]
    unplaced_questions: list[JsonObject]
    tracks: list[JsonObject]
    # Decision 0109: claims answering no question, each with the layer's proposal (or null).
    unplaced_claims: list[JsonObject] = Field(default_factory=list)


class VerificationPlanOut(Out):
    generation: str
    project_seq: int
    budget: JsonObject
    checks: list[JsonObject]


class VerificationProposeOut(Out):
    project_seq: int
    created: list[str]


class DecisionCardsOut(Out):
    generation: str
    project_seq: int
    cards: list[JsonObject]
    recommended: list[str]


class LinksOut(Out):
    project_seq: int
    id: str
    outgoing: list[JsonObject]
    incoming: list[JsonObject]
    relations: list[JsonObject]
    # A02, CP10.4: attributed assumptions relied on (and, for an assumption, its dependents).
    relies_on: list[JsonObject]
    relied_on_by: list[JsonObject]


class NotebookOut(Out):
    project_seq: int
    generation: str
    subject: str | None
    purged_records_omitted: int
    entries: list[JsonObject]


class ImportPlanOut(Out):
    plan_id: str
    plan_sha256: str
    project_id: str
    request_id: str
    expected_seq: int
    created_at: str
    expires_at: str
    root_alias: str
    retention: str
    sensitivity: str
    description: str
    items: list[JsonObject]
    retained: JsonValue
    reference_only: JsonValue
    transmitted: JsonValue


class TaskOut(Out):
    task_id: str
    task: RecordEnvelope
    instructions: str
    allowed_operations: list[str]
    output_schema: JsonObject
    context: str | None
    manifest: JsonObject | None
    commit: SuccessResponse | None = None


__all__ = [
    "Any",
    "BudgetArg",
    "ContextOut",
    "ContextPurposeArg",
    "ImportPlanOut",
    "KindArg",
    "LimitArg",
    "LinksOut",
    "NotebookOut",
    "PathArg",
    "ProjectInfoOut",
    "QueryArg",
    "ReceiptArg",
    "RecordIdArg",
    "RecordPageOut",
    "RequestArg",
    "RequestIdArg",
    "RetentionArg",
    "RootAliasArg",
    "SearchOut",
    "SensitivityArg",
    "SeqArg",
    "SuccessResponse",
    "TaskOut",
    "TaskPurposeArg",
    "TaskResultArg",
    "TitleArg",
]


# ----- CP6: audio ------------------------------------------------------------------------

EngineArg = advertised(str, Literal["faster-whisper", "synthetic"])
ModelNameArg = advertised(
    str | None, Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")] | None
)
LanguageArg = advertised(str | None, Annotated[str, Field(pattern=r"^[a-z]{2,3}$")] | None)
MsArg = advertised(int, Annotated[int, Field(ge=0, le=MAX_INT)])
ChunkSecondsArg = advertised(int, Annotated[int, Field(ge=4, le=600)])
OverlapSecondsArg = advertised(int, Annotated[int, Field(ge=0, le=300)])


class SegmentEdit(StrictModel):
    segment_id: Annotated[str, Field(pattern=r"^seg-[0-9]{5,6}$")]
    text: Annotated[str, Field(max_length=5000)] | None = None
    speaker_label: Annotated[str, Field(max_length=100)] | None = None
    uncertain: bool | None = None


SegmentEditsArg = advertised(
    list[dict[str, Any]], Annotated[list[SegmentEdit], Field(min_length=1, max_length=500)]
)


class AudioImportOut(Out):
    source_id: str
    version_id: str
    sha256: str
    byte_length: int
    retention: str
    sensitivity: str
    audio: JsonObject | None
    commit: JsonObject


class TranscriptionRunOut(Out):
    run_id: str
    version_id: str
    state: str
    engine: str
    engine_version: str
    model: str | None
    language: str | None
    input_sha256: str
    config_digest: str
    total_ms: int | None
    chunks_done: int
    chunk_count: int | None
    cancel_requested: bool
    attempts: int
    error: str | None
    result_version_id: str | None
    created_at: str
    updated_at: str
    resumed: bool | None = None
    source_id: str | None = None
    segments: int | None = None


class RunCancelOut(Out):
    run_id: str
    outcome: str


class TranscriptOut(Out):
    version_id: str
    source_id: str
    sensitivity: str
    payload: JsonObject
    duration_ms: int
    transformation: JsonObject | None
    audio_version_id: str | None
    audio_available: bool
    latest_version_id: str | None
    superseded_by_correction: bool


class SpanOut(Out):
    version_id: str
    source_id: str
    locator: JsonObject
    segments: list[JsonObject]
    quote: str
    quote_sha256: str
    clock: str


class CorrectionOut(Out):
    version_id: str
    previous_version_id: str
    segments_changed: int
    commit: SuccessResponse


class MeetingAudioOut(Out):
    meeting_id: str
    sensitivity: str
    commit: SuccessResponse


class ModelPlanOut(Out):
    model: str
    title: str
    engine: str
    repository: str
    revision: str
    license: str
    files: list[JsonObject]
    total_bytes: int
    total_mib: float
    location: str
    hosts_contacted: list[str]
    transmits: str
    installed: bool
    plan_sha256: str


class ModelsStatusOut(Out):
    models: list[JsonObject]


# ----- CP6.3: providers -------------------------------------------------------------------


class ProviderStatusOut(Out):
    revision: int
    config: JsonObject
    policy: str
    route: str
    endpoint_host: str
    capabilities: list[JsonObject]
    credential: JsonObject
    usage: JsonObject
    digest: str
    silent_remote_fallback: bool


class ProviderPreviewOut(Out):
    purpose: str
    meeting_id: str | None
    capability: str
    policy: str
    route: str
    adapter: str
    endpoint_host: str
    model: str | None
    inputs: JsonObject
    transmits: JsonObject
    cost: JsonObject
    calls: JsonObject
    credential: JsonObject
    blockers: list[str]
    unacknowledged_unknown_calls: list[str]
    would_send: bool
    needs_egress_grant: bool
    digest: str
    fallback: str


class WorkflowRenderOut(Out):
    package: str
    package_digest: str
    recipe: JsonObject
    text: str


class WorkflowStatusOut(Out):
    run: RecordEnvelope
    recipe: JsonObject | None
    ready_steps: list[str]
    package_in_catalog: bool
    text: str


class PaperCheckOut(Out):
    paper_id: str
    paper_rev: int
    project_seq: int
    title: str
    status: str
    deadline: str | None
    manuscript: JsonObject
    venue: JsonObject
    anchors: list[JsonObject]
    findings: list[JsonObject]
    counts: dict[str, int]
    hidden_references: int
    findings_digest: str
    limitations: list[str]


# ----- CP9: LaTeX workspace, engine and Overleaf Git (decision 0012) --------------------------

WorkspacePathArg = advertised(str, Annotated[str, Field(min_length=1, max_length=1024)])
Sha256Arg = advertised(str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")])
FileTextArg = advertised(str, Annotated[str, Field(max_length=2 * 1024 * 1024)])
InsertKindArg = advertised(str, Literal["cite", "figure"])
CommitMessageArg = advertised(str, Annotated[str, Field(min_length=1, max_length=500)])
LineArg = advertised(int, Annotated[int, Field(ge=1, le=10_000_000)])
PageArg = advertised(int, Annotated[int, Field(ge=1, le=100_000)])
PointArg = advertised(float, Annotated[float, Field(ge=-100_000, le=100_000)])
RemoteUrlArg = advertised(str, Annotated[str, Field(min_length=1, max_length=2048)])


class TexEngineStatusOut(Out):
    engine: str
    pinned_version: str
    state: str
    problem: str | None
    location: str
    location_from: str
    installed: JsonObject | None
    license: str
    bundle_url: str
    network: str
    cache: dict[str, int]


class WorkspaceFilesOut(Out):
    workspace: JsonObject
    files: list[JsonObject]
    skipped: dict[str, int]
    limits: dict[str, int]
    limitations: list[str]


class WorkspaceFileOut(Out):
    workspace: JsonObject
    path: str
    sha256: str
    size: int
    text: str | None
    line_ending: str
    binary: bool


class WorkspaceWriteOut(Out):
    workspace: JsonObject
    path: str
    sha256: str
    size: int
    created: bool


class WorkspaceBindOut(Out):
    paper_id: str
    manuscript_source_id: str
    commit: SuccessResponse | None
    imported: SuccessResponse | None = None


class WorkspaceSnapshotOut(Out):
    workspace: JsonObject
    versions: dict[str, JsonObject]
    commits: list[SuccessResponse]
    recorded: int


class WorkspaceStructureOut(Out):
    workspace: JsonObject
    labels: list[JsonObject]
    refs: list[JsonObject]
    cites: list[JsonObject]
    bib_keys: list[JsonObject]
    outline: list[JsonObject]
    includes: list[JsonObject]
    undefined_refs: list[str]
    undefined_cites: list[str]
    truncated: bool
    anchors: list[JsonObject]


class WorkspaceInsertOut(Out):
    kind: str
    version_id: str
    source_id: str
    snippet: str
    written: bool
    relation: SuccessResponse | None
    key: str | None = None
    bib_file: str | None = None
    file: str | None = None
    needs_package: str | None = None


class CompileOut(Out):
    workspace: JsonObject
    inputs: dict[str, str]
    snapshot_commits: list[int]
    engine: JsonObject
    fetch: bool
    status: str
    diagnostics: list[JsonObject]
    outputs: dict[str, str]
    commit: SuccessResponse | None
    seconds: float
    exit_code: int | None = None


class CompileStatusOut(Out):
    paper_id: str
    project_seq: int
    last_compile: JsonObject | None
    pdf: JsonObject | None
    synctex: JsonObject | None
    history: list[JsonObject]


class SynctexForwardOut(Out):
    pdf_version_id: str
    file: str
    line: int
    target: JsonObject | None


class SynctexInverseOut(Out):
    pdf_version_id: str
    page: int
    x: float
    y: float
    source: JsonObject | None


class OverleafStatusOut(Out):
    workspace: JsonObject
    remote: JsonObject
    branch: str
    head: str | None
    remote_head_as_of_last_fetch: str | None
    ahead: int | None
    behind: int | None
    local_changes: list[str]
    blocked_settings: list[str]
    limitations: list[str]
    scope_notice: str | None = None  # agent scope (review cp9-a R2-3)
    restricted_files: list[JsonObject] | None = None  # owner scope


class OverleafPreviewOut(Out):
    workspace: JsonObject
    plan: JsonObject
    digest: str
    applicable: bool
    limitations: list[str]
    capability_operation: str | None = None


class OverleafPullOut(Out):
    applied: str
    head: str | None
    snapshot: JsonObject
    limitations: list[str]


class OverleafCommitOut(Out):
    head: str | None
    message: str


class OverleafLinkOut(Out):
    cloned: str
    bind: JsonObject
    status: JsonObject


# ----- CP10.1 capability discovery (decision 0015) ------------------------------------


class CapabilityBlockerOut(Out):
    code: str
    message: str


class PendingAuthorityOut(Out):
    operation: str
    declared: str
    enforced: str


class CapabilityFeatureOut(Out):
    feature: str
    description: str
    version: str | None
    available: bool
    blocker: CapabilityBlockerOut | None
    required_authority: list[str]
    authority_pending: list[PendingAuthorityOut]
    execution: str
    cost_class: str
    operations: list[str]


class CapabilitiesOut(Out):
    registry_version: int
    software_version: str
    project_id: str | None
    authorities: dict[str, str]
    features: list[CapabilityFeatureOut]


# ----- CP10.2 pending approvals (decision 0016) ------------------------------------------

ApprovalIdArg = advertised(
    str,
    Annotated[
        str,
        Field(
            pattern=r"^apreq_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    ],
)
ApprovalStateArg = advertised(
    str | None,
    Literal["pending", "granted", "denied", "withdrawn", "expired", "revoked", "used"] | None,
)
OptionalRequestIdArg = advertised(
    str | None,
    Annotated[str, Field(pattern=UUID4, description="Canonical lowercase UUID4")] | None,
)
ResolutionArg = advertised(str, Literal["succeeded", "failed", "abandoned"])
DueArg = advertised(str, Annotated[str, Field(min_length=1, max_length=40)])
SlotReasonArg = advertised(str, Annotated[str, Field(min_length=1, max_length=500)])
SkillRefArg = advertised(str, Annotated[str, Field(min_length=1, max_length=200)])
DeviceIdsArg = advertised(
    list[str],
    Annotated[
        list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=1, max_length=2)
    ],
)
SegmentSecondsArg = advertised(int, Annotated[int, Field(ge=5, le=300)])
ProviderPurposeArg = advertised(str, Literal["meeting_extract", "notebook_draft", "next_steps"])
ContextBudgetArg = advertised(int, Annotated[int, Field(ge=1000, le=200_000)])
PlanIdArg = advertised(str, Annotated[str, Field(min_length=1, max_length=80)])


class DecisionPreviewOut(Out):
    digest: str
    expected_seq: int
    operations: list[str]
    protected_effects: list[str]
    root_changes: list[JsonObject] = Field(default_factory=list)
    would_apply: bool
    error: JsonObject | None


class OwnerRouteOut(Out):
    hub: str
    cli: str


class ApprovalOut(Out):
    approval_request_id: str
    operation: str
    state: str
    caller_rank: int
    actor: str
    request_id: str | None
    plan: JsonObject
    plan_sha256: str
    required_scope: str
    summary: list[str]
    owner_route: OwnerRouteOut
    created_at: str
    expires_at: str
    decided_at: str | None
    decided_by: str | None
    decision_note: str | None
    used_at: str | None
    used_seq: int | None
    closed_at: str | None
    result: JsonObject | None


class ApprovalListOut(Out):
    approvals: list[ApprovalOut]


class ApprovalResultOut(Out):
    """``approval_resume``: the single execution's result (``replayed`` when it already ran).
    The request tools answer ``approval_required`` instead; this is what a later resume of
    their request returns."""

    approval_request_id: str
    operation: str
    state: str
    replayed: bool
    result: JsonObject | None


class ApprovalStopOut(Out):
    approval_request_id: str
    session_id: str
    state: str | None


# ----- CP10.4 research continuity (decision 0018) ----------------------------------------

ExecutionStateArg = advertised(
    str,
    Literal[
        "reported_running",
        "reported_completed",
        "reported_failed",
        "reported_cancelled",
        "status_unknown",
    ],
)
ExecutionFilterArg = advertised(
    str | None,
    Literal[
        "registered",
        "reported_running",
        "reported_completed",
        "reported_failed",
        "reported_cancelled",
        "status_unknown",
    ]
    | None,
)
ShortLabelArg = advertised(str, Annotated[str, Field(min_length=1, max_length=200)])
OptionalSummaryArg = advertised(
    str | None, Annotated[str, Field(min_length=1, max_length=2000)] | None
)
OptionalExternalRefArg = advertised(
    str | None, Annotated[str, Field(min_length=1, max_length=300)] | None
)
OptionalAliasArg = advertised(
    str | None, Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")] | None
)
ExpectedOutputsArg = advertised(
    list[str] | None,
    Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(max_length=50)]
    | None,
)
OptionalTimestampArg = advertised(
    str | None,
    Annotated[str, Field(pattern=TS_PATTERN)] | None,
)
ReceiptVersionIdsArg = advertised(
    list[str] | None,
    Annotated[list[Annotated[str, Field(pattern=RECORD_ID_PATTERN)]], Field(max_length=50)] | None,
)
ResolutionKindArg = advertised(str, Literal["interpreted", "dismissed"])
ResolutionTextArg = advertised(str, Annotated[str, Field(min_length=1, max_length=65536)])
SilenceHoursArg = advertised(int, Annotated[int, Field(ge=1, le=2160)])
DueDaysArg = advertised(int, Annotated[int, Field(ge=1, le=365)])


class ExecutionListOut(Out):
    executions: list[JsonObject]
    truncated: bool


class AwaitingOut(Out):
    generation: str
    rule: str
    count: int
    items: list[JsonObject]


class CoverageTotalsOut(Out):
    versions: int
    retained: int
    reference_only: int
    unavailable: int
    backed_up: int
    not_backed_up: int


class CoverageOut(Out):
    generation: str
    totals: CoverageTotalsOut
    verified_backups: int
    items: list[JsonObject]
    next_cursor: str | None


class NowSectionOut(Out):
    count: int
    items: list[JsonObject]
    truncated: bool


class NowChangedOut(NowSectionOut):
    # How many of the section's items, before truncation, are agent-checked results (0078).
    checked_by_agent: int


class NowOut(Out):
    generation: str
    project_seq: int
    generated_at: str
    since_seq: int | None
    due_within_days: int
    silence_after_hours: int
    changed: NowChangedOut
    due: NowSectionOut
    running: NowSectionOut
    awaiting_interpretation: NowSectionOut
    approvals: NowSectionOut
    # A06.1 (decision 0107): the research map in counts (claims not to quote, cards, checks).
    research_map: dict[str, int] | None = None
    # Decision 0109: the brief (at most five items in a fixed order; no model).
    brief: JsonObject | None = None


class SourceCheckOut(Out):
    observations: list[JsonObject]
    saved: bool
    commit: JsonObject | None = None


# ----- A05, CP18.2-CP18.3: research events and the agent-visible queue (decision 0035) --------

EVENT_ID_PATTERN = r"^evt_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
EventIdArg = advertised(str, Annotated[str, Field(pattern=EVENT_ID_PATTERN, max_length=40)])
EventStateArg = advertised(str, Literal["open", "acknowledged", "all"])
EventTypeArg = advertised(
    str | None, Annotated[str, Field(pattern=r"^[a-z_]+\.[a-z_]+$", max_length=64)] | None
)
EventNoteArg = advertised(str | None, Annotated[str, Field(min_length=1, max_length=2000)] | None)
EventCursorArg = advertised(str | None, Annotated[str, Field(max_length=120)] | None)


class QueueCountsOut(Out):
    work_items: int
    proposed: int
    events: int


class QueueGroupOut(Out):
    label: str | None
    count: int
    events: int
    oldest_since: str
    next_due_at: str | None
    examples: list[str]


QueueProposedArg = advertised(str, Literal["fold", "list"])
RunDetailArg = advertised(str, Literal["full", "summary"])


class QueueOut(Out):
    generation: str
    read_only: bool
    generated_at: str
    timezone: str | None
    counts: QueueCountsOut
    proposed: str
    proposed_groups: list[QueueGroupOut]
    entries: list[JsonObject]
    truncated: bool


class EventListOut(Out):
    generation: str
    state: str
    items: list[JsonObject]
    next_cursor: str | None


# ----- A02, CP10.3: operation runs (decision 0017) ----------------------------------------------

RUN_ID_PATTERN = r"^(?:oprun_)?[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
RunIdArg = advertised(str, Annotated[str, Field(pattern=RUN_ID_PATTERN, max_length=42)])
WaitArg = advertised(int, Annotated[int, Field(ge=0, le=20)])
RunStateArg = advertised(
    str,
    Literal[
        "queued", "running", "waiting_for_input", "paused_budget", "completed", "failed",
        "cancelled", "unknown_external",
    ],
)  # fmt: skip
OperationNameArg = advertised(str, Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")])
AmountArg = advertised(str, Annotated[str, Field(pattern=r"^\d{1,9}(\.\d{1,6})?$")])
NoteArg = advertised(str, Annotated[str, Field(min_length=1, max_length=500)])


class RunListOut(Out):
    runs: list[RunView]


class RunControlOut(Out):
    """``run_cancel``: ``cancelled`` when nothing was executing the run, ``cancel_requested``
    while a worker does (not a confirmed stop: the run view's ``cancel_confirmed`` turns true
    only when the worker stopped)."""

    run_id: str
    outcome: Literal["cancelled", "cancel_requested"]
    state: str
    run: RunView


# ----- CP10.5 maintenance service (decision 0021) -----------------------------------------

BackupRefArg = advertised(
    str,
    Annotated[
        str,
        Field(
            min_length=1,
            max_length=4096,
            description="An archive name from maintenance_inspect, or an absolute path",
        ),
    ],
)
StoreLocationArg = advertised(
    str,
    Annotated[str, Field(min_length=1, max_length=4096, description="An absolute folder location")],
)
OptionalStoreLocationArg = advertised(
    str | None,
    Annotated[str, Field(min_length=1, max_length=4096, description="An absolute folder location")]
    | None,
)
QuarantineIdArg = advertised(
    str | None,
    Annotated[
        str,
        Field(
            pattern=r"^quarantine_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    ]
    | None,
)
MaintenanceApprovalIdArg = advertised(
    str,
    Annotated[
        str,
        Field(
            pattern=r"^mapreq_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    ],
)


class MaintenanceApprovalOut(Out):
    """A maintenance request (``contracts/schemas/maintenance_approval_view``)."""

    approval_request_id: str
    operation: str
    state: str
    project_id: str
    caller_rank: int
    actor: str
    request_id: str | None
    plan: JsonObject
    plan_sha256: str
    summary: list[str]
    owner_route: OwnerRouteOut
    damaged_store_path: str | None = None  # absent over MCP (labels only; round 2, F9)
    damaged_store_label: str
    damaged_db_sha256: str
    backup_manifest_sha256: str | None
    target_store_path: str | None = None
    target_store_label: str | None
    created_at: str
    expires_at: str
    decided_at: str | None
    decided_by: str | None
    grant_expires_at: str | None
    used_at: str | None
    closed_at: str | None


class MaintenanceInspectOut(Out):
    project_id: str
    title: str | None
    health: str
    opens: bool
    store: JsonObject
    database: JsonObject
    lock: JsonObject
    backups: list[JsonObject]
    approvals: list[MaintenanceApprovalOut]
    quarantine: list[JsonObject]
    environment: JsonObject
    recovery: JsonObject


class BackupVerifyOut(Out):
    verified: bool
    project_id: str
    backup: JsonObject
    backup_manifest_sha256: str
    manifest: JsonObject


class MaintenanceRestoreOut(Out):
    restored: bool
    registry_switched: bool
    project_id: str
    target_store_path: str | None = None  # absent over MCP (labels only; round 2, F9)
    target_store_label: str
    target_managed: bool
    damaged_store_label: str
    backup: JsonObject
    backup_manifest_sha256: str
    manifest: JsonObject
    next: str


class JournalRecoveryOut(MaintenanceRestoreOut):
    journal_recovered: bool
    recovered_project_seq: int
    recovered_schema_version: int
