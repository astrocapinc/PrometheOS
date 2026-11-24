"""Closed result contracts for every CP10 operation-family member.

The operation discriminator binds each result to its sub-operation. JSON documents
(record bodies, recipes and schemas) retain their own separately registered contracts.
Planning and applying are separate closed result alternatives, including optional
project record writes. Unexpected keys and scalar type changes are rejected.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, JsonValue

from prometheos.adoption.contracts import (
    AdoptionAnalyzer,
    AdoptionBaselinePackage,
    AdoptionEvidence,
    AdoptionFamily,
    AdoptionFile,
    AdoptionScan,
    AnalyzerUnavailable,
)
from prometheos.adoption.plan import PlanStep, StepState
from prometheos.agentops import output_parts as parts
from prometheos.domain.bodies import JobBody, Requirement
from prometheos.domain.models import (
    JsonObject,
    OpResult,
    RecordEnvelope,
    RecordRef,
    StrictModel,
    SuccessResponse,
)
from prometheos.packages import Recipe, Sidecar
from prometheos.providers.config import ProviderConfig

RESULT_MODELS: dict[str, Any] = {}


class AudioDevicesResult1(StrictModel):
    backend: str
    devices: list[parts.AudioDevice]


RESULT_MODELS["audio_devices"] = AudioDevicesResult1


class TranscriptionListResult1(StrictModel):
    runs: list[parts.TranscriptionRow]


RESULT_MODELS["transcription_list"] = TranscriptionListResult1


class RecordingListResult1(StrictModel):
    consent_reminder: str
    sessions: list[parts.RecordingSession]


RESULT_MODELS["recording_list"] = RecordingListResult1


class RecordingStatusResult1(StrictModel):
    abandoned: bool
    backend: str
    cancel_requested: bool
    consent_acknowledged: bool
    elapsed_ms: int
    heartbeat_at: str
    level_dbfs: float
    level_fraction: float
    pause_requested: bool
    reason: str | None
    recoverable: bool
    segments: int
    sensitivity: str
    session_id: str
    sources: list[parts.AudioDevice]
    started_at: str
    state: str
    stop_requested: bool
    title: str
    updated_at: str
    version_id: str | None


RESULT_MODELS["recording_status"] = RecordingStatusResult1


class RecordingControlResult1(StrictModel):
    abandoned: bool
    backend: str
    cancel_requested: bool
    consent_acknowledged: bool
    elapsed_ms: int
    heartbeat_at: str
    level_dbfs: float
    level_fraction: float
    pause_requested: bool
    reason: str | None
    recoverable: bool
    sensitivity: str
    session_id: str
    sources: list[parts.AudioDevice]
    started_at: str
    state: str
    stop_requested: bool
    title: str
    updated_at: str
    version_id: str | None


class RecordingControlResult2(StrictModel):
    abandoned: bool
    backend: str
    cancel_requested: bool
    consent_acknowledged: bool
    duration_ms: int
    elapsed_ms: int
    heartbeat_at: str
    level_dbfs: float
    level_fraction: float
    partial: bool
    segments: int
    pause_requested: bool
    reason: str | None
    recoverable: bool
    sensitivity: str
    session_id: str
    source_id: str
    sources: list[parts.AudioDevice]
    started_at: str
    state: str
    stop_requested: bool
    title: str
    updated_at: str
    version_id: str | None


RESULT_MODELS["recording_control"] = (
    RecordingControlResult1 | RecordingControlResult2 | RecordingStatusResult1
)


class RecordingDiscardResult1(StrictModel):
    session_id: str
    state: str


RESULT_MODELS["recording_discard"] = RecordingDiscardResult1


class RecordHistoryResult1(StrictModel):
    id: str
    project_seq: int
    revisions: list[parts.HistoryRevision]
    transitions: list[parts.HistoryTransition]


RESULT_MODELS["record_history"] = RecordHistoryResult1


class VersionTextResult1(StrictModel):
    availability: str
    byte_length: int
    mime: str
    next_offset: int | None
    offset: int
    reason: str | None
    source_id: str
    text: str
    total_chars: int
    truncated: bool
    version_id: str | None


class VersionTextUnavailable(StrictModel):
    availability: str
    byte_length: int
    mime: str
    reason: str
    source_id: str
    text: None
    version_id: str


RESULT_MODELS["version_text"] = VersionTextResult1 | VersionTextUnavailable


class VersionContentResult1(StrictModel):
    chunk_sha256: str
    data: str
    encoding: str
    length: int
    media_type: str
    next_offset: int | None
    offset: int
    total_bytes: int
    version_id: str | None


RESULT_MODELS["version_content"] = VersionContentResult1


class ContextNextResult1(StrictModel):
    candidates: list[parts.NextCandidate]
    generation: str
    ordering: list[str]
    project_seq: int
    purged_records_omitted: int


RESULT_MODELS["context_next"] = ContextNextResult1


class NotebookExportResult1(StrictModel):
    entries: int
    format: str
    next_offset: int | None
    offset: int
    project_seq: int
    text: str
    total_chars: int


RESULT_MODELS["notebook_export"] = NotebookExportResult1


class ContextAgendaResult1(StrictModel):
    meeting_id: str
    prior_commitments: list[parts.PriorCommitment]
    project_seq: int
    proposed_discussion: list[parts.AgendaDiscussion]
    unresolved_questions: list[parts.AgendaQuestion]


RESULT_MODELS["context_agenda"] = ContextAgendaResult1


class MeetingReconcileResult1(StrictModel):
    held_at: str | None
    items: list[parts.ReconcileItem]
    meeting_id: str
    note: str
    project_seq: int
    recorded_seq: int


RESULT_MODELS["meeting_reconcile"] = MeetingReconcileResult1


class MeetingCaptureResult1(StrictModel):
    commit: SuccessResponse
    meeting_id: str
    sha256: str
    source_id: str
    version_id: str | None


RESULT_MODELS["meeting_capture"] = MeetingCaptureResult1


class ReceiptArtifactAt(StrictModel):
    root_alias: str
    path: str
    source_id: str
    version_id: str
    sha256: str
    role: str


class ReceiptFromPathsResult1(StrictModel):
    """Decision 0112: a receipt recorded from file paths, with the version each path pinned."""

    commit: SuccessResponse
    inputs: list[ReceiptArtifactAt]
    outputs: list[ReceiptArtifactAt]


RESULT_MODELS["source_receipt_from_paths"] = ReceiptFromPathsResult1


class WorkflowExportResult1(StrictModel):
    ready_steps: list[str]
    recipe: Recipe | None
    run: RecordEnvelope


RESULT_MODELS["workflow_export"] = WorkflowExportResult1


class SkillCatalogResult1(StrictModel):
    denylist: list[str]
    packages: list[parts.CatalogRow]


RESULT_MODELS["skill_catalog"] = SkillCatalogResult1


class SkillVerifyResult1(StrictModel):
    capabilities: list[str]
    description: str
    digests_ok: bool
    files: dict[str, parts.PackageFile]
    license_complete: bool
    note: str
    origin: parts.PackageOrigin
    package: str
    package_digest: str
    problems: list[str]
    recipes: list[Recipe]
    status: str
    trust: parts.PackageTrust
    unknown_rights: list[str]


RESULT_MODELS["skill_verify"] = SkillVerifyResult1


class SkillInspectResult1(StrictModel):
    blocking: list[str]
    declared_capabilities: list[str]
    description: str | None
    executable_or_hooks: list[str]
    files: list[parts.InspectionFile]
    findings: list[parts.InspectionFinding]
    name: str | None
    note: str
    package_digest: str
    problems: list[str]
    rights_resolved: bool
    sidecar: Sidecar | None
    source: parts.PackageOrigin
    unknown_rights: list[str]
    version: str | None


RESULT_MODELS["skill_inspect"] = SkillInspectResult1


class SkillValidateResult1(StrictModel):
    skill: str
    valid: bool


RESULT_MODELS["skill_validate"] = SkillValidateResult1


class SkillImportPlanResult1(StrictModel):
    action: str
    blocking: list[str]
    declared_capabilities: list[str]
    files: list[parts.ImportFile]
    findings: list[parts.InspectionFinding]
    kind: str
    name: str | None
    note: str
    package_digest: str
    plan_sha256: str
    problems: list[str]
    rights_resolved: bool
    source: parts.PackageOrigin
    status_after: str
    version: str | None


RESULT_MODELS["skill_import_plan"] = SkillImportPlanResult1


class SkillImportResult1(StrictModel):
    imported: bool
    package: parts.CatalogEntry


RESULT_MODELS["skill_import"] = SkillImportResult1


class SkillInstallResult1(StrictModel):
    plan: parts.ProjectionPlan
    target: parts.FileOperand


class SkillInstallResult2(StrictModel):
    host: str
    installed: str
    plan: parts.ProjectionPlan
    record: SuccessResponse
    target: parts.FileOperand


class SkillInstallResult3(StrictModel):
    host: str
    installed: str
    plan: parts.ProjectionPlan
    target: parts.FileOperand


RESULT_MODELS["skill_install"] = SkillInstallResult1 | SkillInstallResult2 | SkillInstallResult3


class SkillUpdateResult1(StrictModel):
    plan: parts.ProjectionPlan
    target: parts.FileOperand


class SkillUpdateResult2(StrictModel):
    host: str
    installed: str
    plan: parts.ProjectionPlan
    record: SuccessResponse
    target: parts.FileOperand


class SkillUpdateResult3(StrictModel):
    host: str
    installed: str
    plan: parts.ProjectionPlan
    target: parts.FileOperand


RESULT_MODELS["skill_update"] = SkillUpdateResult1 | SkillUpdateResult2 | SkillUpdateResult3


class SkillRemoveResult1(StrictModel):
    plan: parts.RemovalPlan
    target: parts.FileOperand


class SkillRemoveResult2(StrictModel):
    kept: list[str]
    removed: str
    target: parts.FileOperand


class SkillRemoveResult3(StrictModel):
    kept: list[str]
    record: SuccessResponse
    removed: str
    target: parts.FileOperand


RESULT_MODELS["skill_remove"] = SkillRemoveResult1 | SkillRemoveResult2 | SkillRemoveResult3


class SkillCreateResult1(StrictModel):
    created: str
    out: parts.FileOperand
    sidecar: Sidecar | None


RESULT_MODELS["skill_create"] = SkillCreateResult1


class SkillRefreshResult1(StrictModel):
    sidecar: Sidecar | None


RESULT_MODELS["skill_refresh"] = SkillRefreshResult1


class SkillExportResult1(StrictModel):
    distributable: bool
    exported: str
    out: parts.FileOperand
    sha256: str


RESULT_MODELS["skill_export"] = SkillExportResult1


class SkillAdaptResult1(StrictModel):
    adapted: str
    note: str
    out: parts.FileOperand
    parent: str


RESULT_MODELS["skill_adapt"] = SkillAdaptResult1


class HostSetupResult1(StrictModel):
    applied: bool
    files: list[parts.HostSetupFile]
    host: str
    notes: list[str]
    scope: str
    target: parts.FileOperand
    written: list[str]


RESULT_MODELS["host_setup"] = HostSetupResult1


class SkillRevokeResult1(StrictModel):
    package: str
    status: str
    warning: str | None


RESULT_MODELS["skill_revoke"] = SkillRevokeResult1


class PaperGetResult1(StrictModel):
    anchors: list[RecordEnvelope]
    manuscript_versions: list[RecordEnvelope]
    paper: RecordEnvelope
    project_seq: int
    used_versions: list[RecordEnvelope]


RESULT_MODELS["paper_get"] = PaperGetResult1


class PaperRequirementsDraftResult1(StrictModel):
    captured_at: str
    note: str
    requirements: list[Requirement]
    source_id: str
    venue_source_ref: RecordRef
    version_id: str | None


RESULT_MODELS["paper_requirements_draft"] = PaperRequirementsDraftResult1


class PaperNumbersResult1(StrictModel):
    """Decision 0111: where every number in the manuscript comes from."""

    paper_id: str
    title: str
    project_seq: int
    generation: str
    rule: str
    limitations: list[str]
    manuscript: dict[str, Any] | None
    headline: str
    counts: dict[str, int]
    files: list[dict[str, Any]]
    inputs_not_recorded: list[str]
    inputs_skipped: list[str]
    macro_files: list[dict[str, Any]]
    macros: list[dict[str, Any]]
    numbers: list[dict[str, Any]]
    truncated: bool


RESULT_MODELS["paper_numbers"] = PaperNumbersResult1


class CompassDirection(StrictModel):
    kind: Literal["interpret", "resolve", "frame", "verify", "rerun", "trace", "answer", "novelty"]
    paper_id: str | None
    title: str
    why: str
    do: str
    done_when: str
    refs: list[str]
    skill: str | None


class ResearchCompassResult1(StrictModel):
    """Decision 0113: where to look next."""

    generation: str
    rule: str
    project_seq: int
    paper_id: str | None
    library: dict[str, Any]
    counts: dict[str, int]
    total: int
    directions: list[CompassDirection]
    truncated: bool


RESULT_MODELS["research_compass"] = ResearchCompassResult1


class PaperHandoffPlanResult1(StrictModel):
    digest: str
    entries: list[parts.HandoffFile]
    into: parts.FileOperand
    limits: list[str]
    not_included: list[str]
    paper_id: str
    paper_rev: int
    project_seq: int
    target_only_count: int
    target_only_preserved: list[str]
    withheld_by_sensitivity: int


RESULT_MODELS["paper_handoff_plan"] = PaperHandoffPlanResult1


class PaperHandoffApplyResult1(StrictModel):
    digest: str
    into: parts.FileOperand
    target_only_preserved_count: int
    unchanged: list[str]


RESULT_MODELS["paper_handoff_apply"] = PaperHandoffApplyResult1


class PaperCheckSaveResult1(StrictModel):
    report: parts.PaperReport
    saved: SuccessResponse


RESULT_MODELS["paper_check_save"] = PaperCheckSaveResult1


class PaperExportResult1(StrictModel):
    files: list[parts.PaperFile]
    findings_digest: str
    not_included: list[str]
    out: parts.FileOperand
    paper_id: str
    paper_rev: int
    project_seq: int
    sha256: str
    withheld_by_sensitivity: int


RESULT_MODELS["paper_export"] = PaperExportResult1


class JobPlanResult1(StrictModel):
    coalescing: str
    digest: str
    dst: str
    job: JobBody
    next_slots: list[parts.UpcomingSlot]
    runs_only_when: str
    sensitivity: str
    side_effects: str
    transmits_data: bool


RESULT_MODELS["job_plan"] = JobPlanResult1


class JobAddResult1(StrictModel):
    commit: SuccessResponse
    job_id: str


RESULT_MODELS["job_add"] = JobAddResult1


class JobListResult1(StrictModel):
    jobs: list[parts.JobRow]
    project_seq: int


RESULT_MODELS["job_list"] = JobListResult1


class JobSlotsResult1(StrictModel):
    job_id: str
    slots: list[parts.JobSlot]


RESULT_MODELS["job_slots"] = JobSlotsResult1


class JobSetEnabledResult1(StrictModel):
    ok: bool
    project_id: str
    project_seq: int
    replayed: bool
    request_id: str
    results: list[OpResult]
    schema_version: int


RESULT_MODELS["job_set_enabled"] = JobSetEnabledResult1


class JobsTickResult1(StrictModel):
    now: str
    quiet: list[parts.TickRun]
    ran: list[parts.TickRun]
    recovered: list[parts.RecoveredSlot]
    scheduled: parts.ScheduleReport
    events: parts.DetectorReport  # CP18.2: the detector pass every tick runs (decision 0035)


RESULT_MODELS["jobs_tick"] = JobsTickResult1


class JobsInboxResult1(StrictModel):
    items: list[parts.JobNotification]
    project_seq: int


RESULT_MODELS["jobs_inbox"] = JobsInboxResult1


class DoctorResult1(StrictModel):
    home: str
    platform: str
    project: parts.DoctorProject
    projects_registered: int
    prometheos: str
    python: str
    sqlite: str
    supported_schema_version: int


RESULT_MODELS["doctor"] = DoctorResult1


class SchemaListResult1(StrictModel):
    schemas: list[str]


RESULT_MODELS["schema_list"] = SchemaListResult1


class SchemaShowResult1(StrictModel):
    name: str
    document: JsonObject = Field(alias="schema", serialization_alias="schema")


RESULT_MODELS["schema_show"] = SchemaShowResult1


class ProjectListResult1(StrictModel):
    projects: list[parts.ProjectRow]


RESULT_MODELS["project_list"] = ProjectListResult1


class RetentionPurgePlanResult1(StrictModel):
    closure: list[str]
    created_at: str
    expected_seq: int
    limitations: list[str]
    media: list[str]
    payloads: list[str]
    plan_id: str
    plan_sha256: str
    project_id: str
    request_id: str
    targets: list[str]


RESULT_MODELS["retention_purge_plan"] = RetentionPurgePlanResult1


class MaintenanceOrphansResult1(StrictModel):
    grace_seconds: int
    orphans: list[parts.Orphan]


RESULT_MODELS["maintenance_orphans"] = MaintenanceOrphansResult1


class ProviderCallsResult1(StrictModel):
    calls: list[parts.ProviderCall]
    usage: parts.ProviderUsage


RESULT_MODELS["provider_calls"] = ProviderCallsResult1


class TexEnginePlanResult1(StrictModel):
    already_installed: bool
    bundle_url: str
    changes_path_or_system: bool
    destination: str
    download_bytes: int
    download_url: str
    engine: str
    license: str
    license_url: str
    network: str
    plan_digest: str
    release_tag: str
    sha256: str
    version: str


RESULT_MODELS["tex_engine_plan"] = TexEnginePlanResult1


class ProjectInitResult1(StrictModel):
    commit: SuccessResponse
    location: str
    project_id: str
    store: str


RESULT_MODELS["project_init"] = ProjectInitResult1


class RecordingRecoverResult1(StrictModel):
    abandoned: bool
    backend: str
    cancel_requested: bool
    consent_acknowledged: bool
    elapsed_ms: int
    heartbeat_at: str
    level_dbfs: float
    level_fraction: float
    pause_requested: bool
    reason: str | None
    recoverable: bool
    sensitivity: str
    session_id: str
    sources: list[parts.AudioDevice]
    started_at: str
    state: str
    stop_requested: bool
    title: str
    updated_at: str
    version_id: str | None


class RecordingRecoverResult2(StrictModel):
    abandoned: bool
    backend: str
    cancel_requested: bool
    consent_acknowledged: bool
    duration_ms: int
    elapsed_ms: int
    heartbeat_at: str
    level_dbfs: float
    level_fraction: float
    partial: bool
    segments: int
    pause_requested: bool
    reason: str | None
    recoverable: bool
    sensitivity: str
    session_id: str
    source_id: str
    sources: list[parts.AudioDevice]
    started_at: str
    state: str
    stop_requested: bool
    title: str
    updated_at: str
    version_id: str | None


RESULT_MODELS["recording_recover"] = (
    RecordingRecoverResult1 | RecordingRecoverResult2 | RecordingStatusResult1
)


class ProviderConfigurePreviewResult1(StrictModel):
    changes: list[str] | None
    config: ProviderConfig
    credential: parts.CredentialHealth
    current_revision: int
    digest: str
    policy: str
    route: str
    stale: bool


RESULT_MODELS["provider_configure_preview"] = ProviderConfigurePreviewResult1


class JobCancelResult1(StrictModel):
    due_utc: str
    job_id: str
    outcome: str


RESULT_MODELS["job_cancel"] = JobCancelResult1


class JobsDismissResult1(StrictModel):
    dismissed: bool
    id: int


RESULT_MODELS["jobs_dismiss"] = JobsDismissResult1


# ----- adoption (A05, CP17; decision 0039) ----------------------------------------------------


class AdoptionScansResult1(StrictModel):
    scans: list[AdoptionScan]
    total: int = Field(ge=0)


RESULT_MODELS["adoption_scans"] = AdoptionScansResult1


class AdoptionFilesResult1(StrictModel):
    scan_gen: int = Field(ge=1)
    root_alias: str
    state: str
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int | None
    files: list[AdoptionFile]


RESULT_MODELS["adoption_files"] = AdoptionFilesResult1


class AdoptionEvidenceResult1(StrictModel):
    scan_gen: int = Field(ge=1)
    root_alias: str
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int | None
    evidence: list[AdoptionEvidence]


RESULT_MODELS["adoption_evidence"] = AdoptionEvidenceResult1


class AdoptionAnalyzersResult1(StrictModel):
    analyzers: list[AdoptionAnalyzer]
    unavailable: list[AnalyzerUnavailable] = Field(default_factory=list)


RESULT_MODELS["adoption_analyzers"] = AdoptionAnalyzersResult1


class AdoptionFamilySummary(StrictModel):
    family_id: str
    key: str
    kind: str
    band: str
    deciding_rule: str | None
    current: str | None
    members: int = Field(ge=2)
    conflicts: int = Field(ge=0)


class AdoptionFamiliesResult1(StrictModel):
    scan_gen: int = Field(ge=1)
    root_alias: str
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int | None
    families: list[AdoptionFamilySummary]


RESULT_MODELS["adoption_families"] = AdoptionFamiliesResult1
RESULT_MODELS["adoption_family"] = AdoptionFamily


class AdoptionAssistTask(StrictModel):
    task_kind: str
    subject: str
    reason: str
    samples_needed: int = Field(ge=1)
    done: bool


class AdoptionAssistTasksResult1(StrictModel):
    scan_gen: int = Field(ge=1)
    root_alias: str
    total: int = Field(ge=0)
    tasks: list[AdoptionAssistTask]


class AdoptionAssistOutcome(StrictModel):
    state: str
    valid: int = Field(ge=0)
    rejected: int = Field(ge=0)
    needed: int = Field(ge=1)
    output: JsonObject | None = None
    models: list[str] = Field(default_factory=list, max_length=9)


class AdoptionAssistPrepareResult1(StrictModel):
    assist_id: str
    task_kind: str
    subject: str
    samples_needed: int = Field(ge=1)
    instructions: str
    output_schema: JsonObject
    input: JsonObject
    input_sha256: str
    template_sha256: str
    outcome: AdoptionAssistOutcome


class AdoptionAssistSubmitResult1(StrictModel):
    assist_id: str
    task_kind: str
    subject: str
    sample: int = Field(ge=1)
    accepted: bool
    problems: list[str]
    outcome: AdoptionAssistOutcome
    evidence_added: int = Field(ge=0)


RESULT_MODELS["adoption_assist_tasks"] = AdoptionAssistTasksResult1
RESULT_MODELS["adoption_assist_prepare"] = AdoptionAssistPrepareResult1
RESULT_MODELS["adoption_assist_submit"] = AdoptionAssistSubmitResult1


class AdoptionPlanStepView(StrictModel):
    step: PlanStep
    state: StepState
    action_id: str | None
    plan_version: int = Field(ge=1)
    result: dict[str, JsonValue] | None


class AdoptionPlanResult1(StrictModel):
    plan_id: str
    version: int = Field(ge=0)
    scan_gen: int = Field(ge=1)
    root_alias: str
    omitted_decisions: int = Field(ge=0)
    steps: list[AdoptionPlanStepView]


class AdoptionPlanCreateResult1(StrictModel):
    plan_id: str
    version: int = Field(ge=1)
    scan_gen: int = Field(ge=1)
    steps: int = Field(ge=0)
    work_items_created: int = Field(ge=0)


class AdoptionStepReportResult1(StrictModel):
    step_id: str
    state: Literal["validated", "rejected"]
    problems: list[str]
    outcome: dict[str, JsonValue]


class AdoptionClassifyResult1(StrictModel):
    """A06.1, CP20.4 (decision 0085): the recorded class decision."""

    family_id: str
    decision: Literal["variant", "noise", "split", "revoked"]


class AdoptionPlanRef(StrictModel):
    plan_id: str
    version: int = Field(ge=1)


class AdoptionBaselineAcceptResult1(StrictModel):
    baseline_id: str
    sha256: str
    scan_gen: int = Field(ge=1)
    plan: AdoptionPlanRef
    accepted_steps: int = Field(ge=0)


class AdoptionBaselineResult1(StrictModel):
    baseline_id: str
    scan_gen: int = Field(ge=1)
    plan_id: str
    plan_version: int = Field(ge=1)
    accepted_at: str
    accepted_by: str
    sha256: str
    payload: dict[str, JsonValue]


class OutlineCite(StrictModel):
    path: str
    sha256: str | None
    line: int | None = Field(ge=1)
    quote: str | None


class OutlineItem(StrictModel):
    item_id: str
    kind: Literal["track", "deliverable", "question", "result", "stale_candidate"]
    key: str
    title: str
    cites: list[OutlineCite]
    source: Literal["rule", "proposed", "rule+proposed"]
    models: list[str]
    files: int | None = Field(default=None, ge=0)
    path: str | None = None
    sha256: str | None = None
    producer: str | None = None
    producer_sha256: str | None = None
    reason: str | None = None
    addresses: str | None = None
    draws_on: list[str] | None = None


class AdoptionOutlineResult1(StrictModel):
    scan_gen: int = Field(ge=1)
    root_alias: str
    version: int = Field(ge=1)
    items: list[OutlineItem]
    counts: dict[str, int]
    truncated: bool
    sha256: str
    confirmed: dict[str, JsonValue] | None


class AdoptionOutlinePreviewResult1(StrictModel):
    scan_gen: int = Field(ge=1)
    proposal_sha256: str
    kept: dict[str, int]
    dropped: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_reused: int = Field(ge=0)
    relations: int = Field(ge=0)
    unplaced_questions: int = Field(ge=0)
    objective_set: bool
    edits_sha256: str


class AdoptionOutlineConfirmResult1(StrictModel):
    outline_id: str
    sha256: str
    scan_gen: int = Field(ge=1)
    seq: int | None = Field(ge=0)
    records_created: int = Field(ge=0)
    relations_added: int = Field(ge=0)
    records: dict[str, str]


class AdoptionCiteResult1(StrictModel):
    source_id: str
    version_id: str
    sha256: str
    created: bool
    confirmed: bool
    checked_at: str | None


RESULT_MODELS["adoption_cite"] = AdoptionCiteResult1
RESULT_MODELS["adoption_outline"] = AdoptionOutlineResult1
RESULT_MODELS["adoption_outline_preview"] = AdoptionOutlinePreviewResult1
RESULT_MODELS["adoption_outline_confirm"] = AdoptionOutlineConfirmResult1
RESULT_MODELS["adoption_plan"] = AdoptionPlanResult1
RESULT_MODELS["adoption_plan_create"] = AdoptionPlanCreateResult1
RESULT_MODELS["adoption_step_report"] = AdoptionStepReportResult1
RESULT_MODELS["adoption_decide"] = AdoptionFamily
RESULT_MODELS["adoption_classify"] = AdoptionClassifyResult1
RESULT_MODELS["adoption_baseline_accept"] = AdoptionBaselineAcceptResult1
RESULT_MODELS["adoption_baseline"] = AdoptionBaselineResult1


class AdoptionDriftEntry(StrictModel):
    change: Literal["added", "changed", "moved", "deleted"]
    path: str
    from_: str | None = Field(alias="from", serialization_alias="from")
    sha256_before: str | None
    sha256_after: str | None
    status_after: str | None


class AdoptionDriftAgainst(StrictModel):
    kind: Literal["previous", "baseline"]
    scan_gen: int | None
    baseline_id: str | None


class AdoptionDriftFamily(StrictModel):
    family_id: str
    key: str
    current: str | None
    change: Literal["kept", "changed", "moved", "deleted", "undecided"]
    now: str | None


class AdoptionDriftAnalysis(StrictModel):
    extracted: int = Field(ge=0)
    replayed: int = Field(ge=0)
    analyzers_unavailable: list[AnalyzerUnavailable] = Field(default_factory=list)


class AdoptionDriftResult1(StrictModel):
    scan_gen: int = Field(ge=1)
    root_alias: str
    against: AdoptionDriftAgainst
    counts: dict[str, int]
    analysis: AdoptionDriftAnalysis
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int | None
    entries: list[AdoptionDriftEntry]
    families: list[AdoptionDriftFamily]


class AdoptionVaultImportResult1(StrictModel):
    root_alias: str
    paths: list[str]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int | None
    skipped: dict[str, int]
    import_: JsonObject | None = Field(alias="import", serialization_alias="import")


class AdoptionExportMarkdownResult1(StrictModel):
    baseline_id: str
    root_alias: str
    note: str
    text_sha256: str


class AdoptionBaselineSummary(StrictModel):
    baseline_id: str
    root_alias: str
    scan_gen: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    accepted_at: str
    sha256: str


class AdoptionBaselinesResult1(StrictModel):
    baselines: list[AdoptionBaselineSummary]
    total: int = Field(ge=0)


RESULT_MODELS["adoption_baselines"] = AdoptionBaselinesResult1
RESULT_MODELS["adoption_drift"] = AdoptionDriftResult1
RESULT_MODELS["adoption_baseline_export"] = AdoptionBaselinePackage
RESULT_MODELS["adoption_vault_import"] = AdoptionVaultImportResult1
RESULT_MODELS["adoption_export_markdown"] = AdoptionExportMarkdownResult1


# ----- the literature library (A02, CP11 S11.1; decision 0060) --------------------------------


class LibraryLocator(StrictModel):
    type: Literal["pdf"]
    page: int = Field(ge=1)
    bbox: None
    start: int = Field(ge=0)
    end: int = Field(ge=1)


class LibraryEvidence(StrictModel):
    source_id: str
    version_id: str
    locator: LibraryLocator
    quote_sha256: str


class LibraryFile(StrictModel):
    alias: str
    path: str


class LibraryWorkSummary(StrictModel):
    work_id: str
    rev: int = Field(ge=1)
    title: str
    contributors: list[str]
    year: int | None
    venue: str | None
    identifiers: dict[str, str]
    access_state: Literal["metadata_only", "abstract_only", "full_text", "unavailable"]
    page_count: int | None = Field(ge=1)
    pages_without_text: int = Field(ge=0)
    text_version_id: str | None
    pdf_version_id: str | None
    pdf_source_id: str | None
    file: LibraryFile | None
    sources: int = Field(ge=0)  # bibliography versions that contributed (S11.2)
    merged_into: str | None
    sensitivity: Literal["normal", "restricted", "private"]


class LibraryWorksResult1(StrictModel):
    project_seq: int = Field(ge=0)
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int | None
    works: list[LibraryWorkSummary]
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["library_works"] = LibraryWorksResult1


class LibraryPageState(StrictModel):
    page: int = Field(ge=1)
    state: Literal["text", "no_text", "too_long"]
    chars: int = Field(ge=0)


class LibraryEngine(StrictModel):
    name: Literal["pypdfium2"]
    version: str
    pdfium: str


class LibraryVersionLink(StrictModel):
    work_id: str
    title: str
    role: Literal["preprint", "published"]


class LibraryWorkResult1(StrictModel):
    project_seq: int = Field(ge=0)
    work: LibraryWorkSummary
    record: RecordEnvelope
    pages: list[LibraryPageState]
    engine: LibraryEngine | None
    versions: list[LibraryVersionLink]
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["library_work"] = LibraryWorkResult1


class LibrarySnippet(StrictModel):
    before: str
    match: str
    after: str


class LibraryHit(StrictModel):
    work_id: str
    title: str
    text_version_id: str
    pdf_version_id: str
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    snippet: LibrarySnippet
    locator: LibraryLocator
    quote_sha256: str
    evidence: LibraryEvidence


class LibrarySearchResult1(StrictModel):
    project_seq: int = Field(ge=0)
    mode: Literal["fts5", "substring"]
    query: str
    hits: list[LibraryHit]
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["library_search"] = LibrarySearchResult1


class LibraryWhere(StrictModel):
    page: int = Field(ge=1)
    start: int = Field(ge=0)


class LibrarySpan(StrictModel):
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)


class LibraryHeader(StrictModel):
    work_id: str | None
    title: str | None
    text_version_id: str
    pdf_version_id: str
    pdf_source_id: str
    page_count: int = Field(ge=1)


class LibraryReadResult1(LibraryHeader):
    page: int = Field(ge=1)
    state: Literal["text", "no_text", "too_long"]
    page_chars: int = Field(ge=0)
    page_sha256: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    clamped: bool
    text: str
    next: LibraryWhere | None
    note: str | None
    locator: LibraryLocator | None
    quote_sha256: str | None
    evidence: LibraryEvidence | None
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["library_read"] = LibraryReadResult1


class LibraryQuoteCheckResult1(LibraryHeader):
    result: Literal["match", "mismatch"]
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    true_text: str
    true_sha256: str
    given_sha256: str
    found_at: list[LibrarySpan]
    locator: LibraryLocator
    quote_sha256: str
    evidence: LibraryEvidence
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["library_quote_check"] = LibraryQuoteCheckResult1


# ----- CP11 S11.2: candidates, decisions, export, annotations ------------------------------------


class LibraryCandidateWork(StrictModel):
    work_id: str
    rev: int = Field(ge=1)
    title: str
    first_author: str | None
    year: int | None
    venue: str | None
    identifiers: dict[str, str]


class LibraryCandidateEvidence(StrictModel):
    same_title_key: str
    first_author_family: str
    years: list[int | None]


class LibraryCandidate(StrictModel):
    candidate_id: str
    kind: Literal["duplicate", "version"]
    title_key: str
    evidence: LibraryCandidateEvidence
    works: list[LibraryCandidateWork]
    suggested: str


class LibraryDecision(StrictModel):
    decision_id: str
    work_a: str
    work_b: str
    choice: Literal["merge", "distinct", "versions", "unlinked"]
    note: str | None
    decided_by: str
    decided_at: str


class LibraryDedupLink(StrictModel):
    preprint: str
    preprint_title: str
    published: str
    published_title: str
    origin: Literal["import", "decision"]


class LibraryDedupResult1(StrictModel):
    project_seq: int = Field(ge=0)
    candidates: list[LibraryCandidate]
    total: int = Field(ge=0)
    decisions: list[LibraryDecision]
    links: list[LibraryDedupLink]
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["library_dedup"] = LibraryDedupResult1


class LibraryExportWork(StrictModel):
    work_id: str
    key: str


class LibraryExportResult1(StrictModel):
    project_seq: int = Field(ge=0)
    format: Literal["bibtex", "ris", "csv"]
    count: int = Field(ge=0)
    works: list[LibraryExportWork]
    content: str
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["library_export"] = LibraryExportResult1


class LibraryAnnotation(StrictModel):
    entry_id: str
    rev: int = Field(ge=1)
    title: str
    text: str
    page: int = Field(ge=1)
    start: int | None
    end: int | None
    quote_sha256: str | None
    recorded_at: str


class LibraryAnnotationsResult1(StrictModel):
    project_seq: int = Field(ge=0)
    work_id: str
    annotations: list[LibraryAnnotation]
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["library_annotations"] = LibraryAnnotationsResult1


class LibraryAnnotateResult1(StrictModel):
    entry_id: str
    work_id: str
    evidence: LibraryEvidence
    quote: str
    commit: SuccessResponse
    content_trust: Literal["untrusted_data"]


RESULT_MODELS["library_annotate"] = LibraryAnnotateResult1


class LibraryResolveResult1(StrictModel):
    choice: Literal["merge", "distinct", "versions"]
    work_a: str
    work_b: str
    merged: str | None
    into: str | None
    preprint: str | None
    published: str | None
    commit: SuccessResponse


RESULT_MODELS["library_dedup_resolve"] = LibraryResolveResult1


class LibraryUndoResult1(StrictModel):
    undone: Literal["merge", "distinct", "versions", "link", "unlinked"]
    work_a: str
    work_b: str
    commit: SuccessResponse


RESULT_MODELS["library_dedup_undo"] = LibraryUndoResult1


# ----- CP11 S11.4: evidence matrices (decision 0064) ----------------------------------------------


class MatrixColumnOut(StrictModel):
    id: str
    label: str
    value_kind: Literal["text", "quantity", "choice"]
    instructions: str
    choices: list[str] | None = None


class MatrixHeader(StrictModel):
    matrix_id: str
    rev: int = Field(ge=1)
    title: str
    question_ref: RecordRef | None
    columns: list[MatrixColumnOut]
    rubric_version: int = Field(ge=1)
    sensitivity: Literal["normal", "restricted", "private"]


class MatrixWhere(StrictModel):
    work_id: str
    page: int | None = Field(default=None, ge=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class MatrixEvidence(StrictModel):
    source_id: str
    version_id: str
    locator: JsonObject
    quote_sha256: str | None
    where: MatrixWhere


class MatrixQuantity(StrictModel):
    decimal: str
    unit: str
    source_pointer: str


class MatrixRuleRef(StrictModel):
    engine: str
    version: str


class MatrixCellOut(StrictModel):
    column_id: str
    value_text: str
    quantities: list[MatrixQuantity]
    evidence: list[MatrixEvidence]
    basis: Literal["full_text", "abstract", "metadata"]
    state: Literal["proposed", "reviewed", "unsupported"]
    method: Literal["manual", "rule", "model"]
    rule: MatrixRuleRef | None


class MatrixFlag(StrictModel):
    reason: str
    cause_id: str
    created_seq: int = Field(ge=0)


class MatrixRow(StrictModel):
    work_id: str
    work_title: str
    work_rev: int = Field(ge=1)
    access_state: Literal["metadata_only", "abstract_only", "full_text", "unavailable"]
    in_matrix: bool
    extraction_id: str | None
    rev: int | None
    sensitivity: Literal["normal", "restricted", "private"] | None
    rubric_version: int | None
    stale: bool
    needs_review: bool
    flags: list[MatrixFlag]
    cells: list[MatrixCellOut]


class MatrixBrief(StrictModel):
    version_id: str
    source_id: str
    sha256: str
    created_seq: int = Field(ge=0)
    recorded_at: str
    needs_review: bool
    flags: list[MatrixFlag]


class MatrixCellCounts(StrictModel):
    proposed: int = Field(ge=0)
    reviewed: int = Field(ge=0)
    unsupported: int = Field(ge=0)


class MatrixCounts(StrictModel):
    rows: int = Field(ge=0)
    works: int = Field(ge=0)
    cells: MatrixCellCounts
    access_states: dict[str, int]
    needs_review: int = Field(ge=0)
    stale: int = Field(ge=0)


class MatrixShowResult1(StrictModel):
    project_seq: int = Field(ge=0)
    matrix: MatrixHeader
    rows: list[MatrixRow]
    briefs: list[MatrixBrief]
    counts: MatrixCounts
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["matrix_show"] = MatrixShowResult1


class MatrixSummary(StrictModel):
    matrix_id: str
    rev: int = Field(ge=1)
    title: str
    rubric_version: int = Field(ge=1)
    columns: int = Field(ge=1)
    counts: MatrixCounts
    latest_brief: str | None
    brief_needs_review: bool


class MatrixListResult1(StrictModel):
    project_seq: int = Field(ge=0)
    matrices: list[MatrixSummary]
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["matrix_list"] = MatrixListResult1


class MatrixCreateResult1(StrictModel):
    matrix_id: str
    commit: SuccessResponse


RESULT_MODELS["matrix_create"] = MatrixCreateResult1


class MatrixRowChangeResult1(StrictModel):
    extraction_id: str
    commit: SuccessResponse


RESULT_MODELS["matrix_propose"] = MatrixRowChangeResult1
RESULT_MODELS["matrix_review"] = MatrixRowChangeResult1
RESULT_MODELS["matrix_unsupported"] = MatrixRowChangeResult1


class MatrixRuleRow(StrictModel):
    work_id: str
    extraction_id: str | None
    action: Literal["created", "revised", "unchanged", "refused"]
    cells: dict[str, Literal["proposed", "reviewed", "unsupported"]]
    reason: str | None
    refused_cells: dict[str, str]


class MatrixExtractResult1(StrictModel):
    run_id: str
    matrix_id: str
    rules_sha256: str
    rows: list[MatrixRuleRow]
    counts: dict[str, int]


RESULT_MODELS["matrix_extract"] = MatrixExtractResult1


class MatrixExportRow(StrictModel):
    work_id: str
    extraction_id: str | None
    rev: int | None


class MatrixBriefRecord(StrictModel):
    version_id: str
    source_id: str
    sha256: str
    built_at_seq: int | None
    rows: list[MatrixExportRow]
    commit: SuccessResponse


class MatrixExportResult1(StrictModel):
    project_seq: int = Field(ge=0)
    matrix_id: str
    format: Literal["csv", "markdown", "json"]
    rows: list[MatrixExportRow]
    content: str
    content_sha256: str
    brief: MatrixBriefRecord | None
    content_trust: Literal["untrusted_data"]
    notice: str


RESULT_MODELS["matrix_export"] = MatrixExportResult1


# ----- CP11 S11.4b: research folders (decision 0066) ---------------------------------------------


class ProjectRoot(StrictModel):
    alias: str
    path: str
    available: bool


class ProjectRootsResult1(StrictModel):
    settings_rev: int = Field(ge=1)
    roots: list[ProjectRoot]
    removal: str


RESULT_MODELS["project_roots"] = ProjectRootsResult1


class ProjectRootAddResult1(StrictModel):
    alias: str
    path: str
    commit: SuccessResponse


RESULT_MODELS["project_root_add"] = ProjectRootAddResult1


# ----- CP11 S11.5: the read-only Zotero adapter's probe (decision 0070) -------------------------


class ZoteroCollectionRow(StrictModel):
    key: str
    name: str
    parent: str | None


class ZoteroStatusResult1(StrictModel):
    state: Literal["available", "not_running", "local_api_disabled", "unexpected"]
    text: str
    endpoint: str | None
    version: str | None
    api_version: str | None
    schema_version: str | None = Field(default=None, exclude_if=lambda v: v is None)
    library_version: int | None = Field(default=None, exclude_if=lambda v: v is None)
    detail: str | None = Field(default=None, exclude_if=lambda v: v is None)
    collections: list[ZoteroCollectionRow] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    collections_error: str | None = Field(default=None, exclude_if=lambda v: v is None)


RESULT_MODELS["zotero_status"] = ZoteroStatusResult1


# ----- the intelligence layer (A07, CP22.4; decision 0094) -------------------------------------


class AiPolicyGranted(StrictModel):
    revision: int
    policy: JsonObject
    policy_sha256: str
    provider_revision: int
    granted_by: str
    granted_at: str


class AiPolicyShowResult1(StrictModel):
    policy: AiPolicyGranted | None
    in_force: bool
    void_reason: str | None
    provider_revision: int
    provider_is_openrouter: bool
    key: JsonObject
    spend: JsonObject
    tasks: list[str]


RESULT_MODELS["ai_policy_show"] = AiPolicyShowResult1


class AiResultRow(StrictModel):
    result_key: str
    task_kind: str
    task_version: int
    subject: str
    sample: int
    executor: Literal["rules", "gateway", "agent", "cassette"]
    outcome: Literal["accepted", "abstained", "rejected"]
    reasons: list[str]
    hint_only: bool
    model: str
    served_model: str | None
    run_id: str
    scan_gen: int | None
    input_sha256: str
    summary: str | None
    claim: str | None


class AiResultsResult1(StrictModel):
    results: list[AiResultRow]


RESULT_MODELS["ai_results"] = AiResultsResult1


class AiEvaluationRow(StrictModel):
    evaluation_id: str
    task_kind: str
    task_version: int
    model: str
    providers: list[str]
    executor: str
    trusted: bool
    coverage: float
    precision_lower_bound: float
    correct: int
    answered: int
    items: int
    why: str | None
    measured_at: str


class AiEvaluationsResult1(StrictModel):
    evaluations: list[AiEvaluationRow]


RESULT_MODELS["ai_evaluations"] = AiEvaluationsResult1


class RootSensitivityRow(StrictModel):
    root_alias: str
    sensitivity: Literal["normal", "restricted", "private"]
    set_by_owner: bool
    note: str | None


class RootSensitivityShowResult1(StrictModel):
    roots: list[RootSensitivityRow]


RESULT_MODELS["root_sensitivity_show"] = RootSensitivityShowResult1


class RootSensitivitySetResult1(StrictModel):
    root_alias: str
    sensitivity: Literal["normal", "restricted", "private"]
    lowered: bool


RESULT_MODELS["root_sensitivity_set"] = RootSensitivitySetResult1


class AiPolicyGrantResult1(StrictModel):
    revision: int
    policy_sha256: str
    provider_revision: int
    in_force: bool


RESULT_MODELS["ai_policy_grant"] = AiPolicyGrantResult1


class AiPolicyRevokeResult1(StrictModel):
    revoked: bool
    in_force: bool


RESULT_MODELS["ai_policy_revoke"] = AiPolicyRevokeResult1


class AiRunItem(StrictModel):
    subject: str
    input_sha256: str | None = Field(default=None, exclude_if=lambda v: v is None)
    outcome: Literal["accepted", "rejected", "abstained", "disagreed", "denied", "held", "not_run"]
    reasons: list[str]
    proposal: JsonObject | None = Field(default=None, exclude_if=lambda v: v is None)
    hint_only: bool | None = Field(default=None, exclude_if=lambda v: v is None)
    samples: list[str] | None = Field(default=None, exclude_if=lambda v: v is None)
    provenance: list[JsonObject] | None = Field(default=None, exclude_if=lambda v: v is None)


class AiRunResult1(StrictModel):
    run_id: str
    task_kind: str
    task_version: int
    executor: Literal["gateway", "cassette", "agent"]
    items: list[AiRunItem]
    counts: dict[str, int]
    stopped: JsonObject | None
    new_samples: int
    cached_samples: int
    outside_policy: bool | None = Field(default=None, exclude_if=lambda v: v is None)


class AiRunPerCallPreview(StrictModel):
    dry_run: Literal[True]
    outside_policy: Literal[True]
    task_kind: str
    scan_gen: int
    paths: list[str]
    max_output_tokens: int
    routing: str
    model: str
    endpoint_host: str
    provider_revision: int
    inputs: dict[str, str]
    denied: list[str]
    skipped: list[str]
    calls_at_most: int
    estimate_upper_usd: str
    data_policy: str


class AiRunEstimate(StrictModel):
    dry_run: Literal[True]
    task_kind: str
    subjects: int
    denied: int
    skipped: list[AiRunItem]
    calls_at_most: int
    estimate_upper_usd: str
    estimate_label: str
    spend: JsonObject
    key: JsonObject | None


RESULT_MODELS["ai_run"] = AiRunResult1 | AiRunEstimate | AiRunPerCallPreview
RESULT_MODELS["ai_map_suggest"] = AiRunResult1 | AiRunEstimate


class AiAcceptResult1(StrictModel):
    task_kind: str
    subject: str
    accepted_samples: int
    assist_outcome: str
    evidence_added: int


RESULT_MODELS["ai_accept"] = AiAcceptResult1


def result_model(operation: str) -> Any:
    return RESULT_MODELS[operation]
