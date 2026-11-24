"""Fixed nested protocol structures used by the operation-family result contracts.

RecordEnvelope bodies and Recipe input/output JSON Schemas keep the separate domain
contracts that govern those documents. Variable map keys have typed values. Optional
protocol fields serialize only when actually returned, preserving transport parity.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from prometheos.domain.bodies import JobBody
from prometheos.domain.ids import RecordId
from prometheos.domain.models import (
    EvidenceRef,
    Lifecycle,
    Locator,
    RecordRef,
    Sensitivity,
    Sha256,
    StrictModel,
    Timestamp,
)
from prometheos.packages import Component, Parent


class ProtocolPart(StrictModel):
    @model_serializer(mode="wrap")
    def _returned_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        shown: dict[str, Any] = handler(self)
        names = {
            type(self).model_fields[n].serialization_alias or type(self).model_fields[n].alias or n
            for n in self.model_fields_set
        }
        return {k: v for k, v in shown.items() if k in names}


class FileOperand(StrictModel):
    root_alias: str
    path: str


class AudioDevice(StrictModel):
    id: str
    name: str
    kind: str
    channels: int
    sample_rate: int
    default: bool


class RecordingSession(StrictModel):
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
    sources: list[AudioDevice]
    started_at: str
    state: str
    stop_requested: bool
    title: str
    updated_at: str
    version_id: str | None


class TranscriptionRow(ProtocolPart):
    run_id: str
    version_id: str
    state: str
    engine: str
    engine_version: str
    model: str | None
    language: str | None
    input_sha256: Sha256
    config_digest: Sha256
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


class HistoryRevision(StrictModel):
    rev: int
    commit_seq: int
    recorded_at: str
    occurred_at: str | None
    sensitivity: Sensitivity
    lifecycle: Lifecycle
    operation: str | None
    content_purged: bool


class HistoryTransition(StrictModel):
    rev: int
    from_state: str = Field(alias="from")
    to: str
    reason: str
    evidence: list[EvidenceRef]
    protected_effects: list[str]
    commit_seq: int


class NextCandidate(StrictModel):
    id: str
    kind: str
    source_refs: list[str]
    why_now: str
    expected_output: str
    dependencies: list[str]
    effort: str
    would_change: str
    adopted_action_id: str | None


class PriorCommitment(StrictModel):
    item_id: str
    text: str
    action_id: str
    action_status: str
    completion_evidence: list[EvidenceRef]


class AgendaQuestion(StrictModel):
    item_id: str
    text: str
    proposed_owner: RecordRef | None


class AgendaDiscussion(StrictModel):
    item_id: str
    type: str
    text: str


class AudioEvidence(StrictModel):
    version_id: str
    start_ms: int
    end_ms: int
    clock: str
    segment_ids: list[str]
    quote: str
    uncertain: bool
    newer_version_id: str | None
    text_changed_since: bool


class RelatedAction(StrictModel):
    action_id: str
    status: str
    overlap: float
    linked: bool


class ReconcileItem(StrictModel):
    item_id: str
    type: str
    text: str
    status: str
    classification: str
    owner: str
    due: str
    evidence: list[EvidenceRef]
    audio_spans: list[AudioEvidence]
    related_actions: list[RelatedAction]


class HumanReview(StrictModel):
    by: str
    at: str


class PackageTrust(StrictModel):
    origin_identified: bool
    license_reviewed: bool
    automated_checks_passed: bool
    human_review: HumanReview | None
    live_use_observed: bool


class CatalogRow(StrictModel):
    name: str
    version: str
    package_digest: Sha256
    status: Literal["available", "quarantined", "revoked"]
    trust: PackageTrust
    imported_at: str


class PackageOrigin(ProtocolPart):
    kind: Literal["folder", "zip", "research_root"]
    path: str | None = None
    root_alias: str | None = None
    archive_sha256: Sha256 | None = None


class PackageFile(StrictModel):
    sha256: Sha256
    license: str | None
    executable: bool


class CatalogEntry(CatalogRow):
    description: str
    source: PackageOrigin
    license_expression: str | None
    capabilities: list[str]
    skills: list[str]
    recipes: list[str]
    parent: Parent | None
    files: dict[str, PackageFile]
    components: dict[str, Component]
    rights_resolved: bool
    import_plan_sha256: Sha256


class InspectionFile(StrictModel):
    path: str
    sha256: Sha256
    bytes: int
    license: str | None
    executable: bool
    install_hook: bool
    modified: bool
    origin_url: str | None
    origin_revision: str | None
    license_resolved: bool


class ImportFile(StrictModel):
    path: str
    sha256: Sha256
    license: str | None
    executable: bool
    install_hook: bool


class InspectionFinding(StrictModel):
    path: str
    line: int
    kind: str
    excerpt: str


class ProjectionOperation(StrictModel):
    path: str
    action: Literal[
        "create",
        "adopt_identical",
        "keep_adopted",
        "keep_modified",
        "delete",
        "unchanged",
        "replace",
        "overwrite_local_edit",
    ]
    sha256: Sha256 | None


class RemovalOperation(StrictModel):
    path: str
    action: Literal["already_absent", "keep_adopted", "delete", "keep_modified"]


class ProjectionConflict(StrictModel):
    path: str
    reason: str


class ProjectionChanges(StrictModel):
    from_version: str
    to_version: str
    licenses: list[str]
    capabilities_added: list[str]
    capabilities_removed: list[str]


class SkillSummary(StrictModel):
    name: str
    description: str | None
    skill_md_bytes: int
    all_files_bytes: int


class ProjectionPlan(StrictModel):
    kind: Literal["package_install", "package_update"]
    target: str
    host: Literal["claude-code", "opencode"]
    package: str
    package_digest: Sha256
    operations: list[ProjectionOperation]
    conflicts: list[ProjectionConflict]
    changes: ProjectionChanges | None
    skills: list[SkillSummary]
    executable_files: list[str]
    unknown_rights: list[str]
    resolution: Literal["keep-local", "take-package"] | None
    plan_sha256: Sha256
    note: str


class RemovalPlan(StrictModel):
    kind: Literal["package_remove"]
    target: str
    host: Literal["claude-code", "opencode"]
    package: str
    operations: list[RemovalOperation]
    plan_sha256: Sha256
    note: str


class HostSetupFile(StrictModel):
    path: str
    action: str


class PaperFile(StrictModel):
    path: str
    role: str
    source_id: str | None
    version_id: str | None
    sha256: Sha256
    byte_length: int


class HandoffFile(PaperFile):
    action: Literal["new", "identical", "conflict"]
    existing_sha256: Sha256 | None


class UpcomingDue(ProtocolPart):
    due_utc: str
    local: str | None = None
    note: str | None = None


class UpcomingSkipped(StrictModel):
    local_date: str
    skipped: str


UpcomingSlot = UpcomingDue | UpcomingSkipped


class JobRecord(StrictModel):
    id: RecordId
    kind: Literal["job"]
    rev: int
    recorded_at: Timestamp
    occurred_at: Timestamp | None
    sensitivity: Sensitivity
    lifecycle: Lifecycle
    body: JobBody


class ManualResolution(StrictModel):
    from_state: Literal["unknown"] = Field(alias="from")
    as_state: Literal["succeeded", "failed", "abandoned"] = Field(alias="as")
    reason: str
    by: str
    at: str


class JobUsage(StrictModel):
    provider_calls: int
    estimated_cost: str | None


class SlotMetadata(ProtocolPart):
    finding: bool | None = None
    manual_resolution: ManualResolution | None = None


class PaperSlotResult(SlotMetadata):
    findings_digest: Sha256
    counts: dict[str, int]
    keys: list[str]
    project_seq: int
    usage: JobUsage


class SourceSlotResult(SlotMetadata):
    states: dict[str, str]
    changed: list[str]
    usage: JobUsage


class DigestSlotResult(SlotMetadata):
    project_seq: int
    since_seq: int
    kinds: dict[str, int]
    usage: JobUsage


class CoalescedSlotResult(SlotMetadata):
    coalesced_slots: int
    not_itemised: int | None = None


class ResolvedSlotResult(StrictModel):
    manual_resolution: ManualResolution


SlotResult = (
    PaperSlotResult | SourceSlotResult | DigestSlotResult | CoalescedSlotResult | ResolvedSlotResult
)


class JobSlot(StrictModel):
    due_utc: str
    state: str
    outcome: str
    attempts: int
    reason: str | None
    external_started_at: str | None
    manual_resolution: ManualResolution | None
    result: SlotResult | None
    updated_at: str


SlotState = Literal["pending", "running", "succeeded", "failed", "skipped", "unknown", "abandoned"]


class JobRow(StrictModel):
    job: JobRecord
    recent_slots: list[JobSlot]
    slot_counts: dict[SlotState, int]
    next_slots: list[UpcomingSlot]


class ScheduleReport(StrictModel):
    coalesced: int
    pending: int


class EventSubject(StrictModel):
    kind: str
    id: str


class EmittedEvent(StrictModel):
    event_id: str
    type: str
    subject_ref: EventSubject


class DetectorReport(StrictModel):
    """The event detector pass a tick runs (CP18.2, decision 0035): what it emitted."""

    checked_at: str
    emitted: list[EmittedEvent]


class TickRun(ProtocolPart):
    job_id: str
    due_utc: str
    state: str
    finding: bool | None = None
    reason: str | None = None
    lost: str | None = None


class RecoveredSlot(StrictModel):
    job_id: str
    due_utc: str
    state: str
    reason: str


class JobNotification(StrictModel):
    id: int
    job_id: str
    due_utc: str
    commit_seq: int
    category: str
    title: str
    summary: str
    record_ids: list[str]
    created_at: str
    dismissed_at: str | None


class DoctorProject(StrictModel):
    project_id: str
    store: str
    schema_version: int
    journal_mode: str
    integrity_check: str
    project_seq: int
    location: str


class ProjectQuarantine(StrictModel):
    quarantine_id: str
    damaged_store_path: str
    damaged_integrity: str
    switched_at: Timestamp


class ProjectRow(StrictModel):
    project_id: str
    title: str
    registered_at: str
    location_note: str
    store_path: str
    previous_store_path: str | None
    quarantined_stores: list[ProjectQuarantine]


class Orphan(StrictModel):
    area: Literal["media", "staging"]
    name: str
    age_seconds: int
    collectable: bool
    removed: bool


class ProviderUsage(ProtocolPart):
    calls: int | None = None
    unknown_outcomes: int | None = None
    spent_estimate_usd: str
    cap_usd: str | None
    remaining_usd: str | None
    label: str


class TokenUsage(StrictModel):
    prompt_tokens: int | None
    completion_tokens: int | None


class ProviderCall(StrictModel):
    call_id: str
    run_key: str
    purpose: str
    route: str
    endpoint_host: str
    model: str
    attempt: int
    state: str
    reserved_cost_usd: str | None
    actual_cost_usd: str | None
    cost_basis: str | None
    usage: TokenUsage | None
    input_digest: str
    output_digest: str | None
    task_id: str | None
    error: str | None
    acknowledges: str | None
    created_at: str
    updated_at: str


class CredentialHealth(StrictModel):
    reference: str | None
    present: bool
    store: str


class PaperLatest(StrictModel):
    version_id: str
    sha256: Sha256
    mime: str
    captured_at: str
    availability: str


class PaperManuscript(StrictModel):
    source_id: str | None
    latest: PaperLatest | None


class VenueSource(StrictModel):
    version_id: str
    source_id: str
    captured_at: str
    availability: str


class PaperVenue(StrictModel):
    known: bool
    sources: list[VenueSource]


class PaperAnchor(StrictModel):
    id: str
    rev: int
    status: str
    manuscript_version_id: str
    locator: Locator


class RemapCandidate(StrictModel):
    version_id: str
    start_line: int
    end_line: int


class PaperFindingDetail(ProtocolPart):
    reason: str | None = None
    requirement: str | None = None
    applicability: str | None = None
    status: str | None = None
    last_checked_at: str | None = None
    state: str | None = None
    superseded_by: str | None = None
    remap_candidates: list[RemapCandidate] | None = None
    bound_version: str | None = None
    latest_version: str | None = None
    review_round: str | None = Field(default=None, alias="round")
    item: str | None = None


class PaperFinding(StrictModel):
    code: str
    record_ids: list[str]
    detail: PaperFindingDetail


class PaperReport(StrictModel):
    paper_id: str
    paper_rev: int
    project_seq: int
    title: str
    status: str
    deadline: str | None
    manuscript: PaperManuscript
    venue: PaperVenue
    anchors: list[PaperAnchor]
    findings: list[PaperFinding]
    counts: dict[str, int]
    hidden_references: int
    findings_digest: Sha256
    limitations: list[str]
