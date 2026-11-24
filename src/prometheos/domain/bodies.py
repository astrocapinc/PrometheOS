"""Typed record bodies, introduced per checkpoint.

CP1: project settings, source, version, entry. Later checkpoints register more
kinds. Every model rejects unknown fields; decimal quantities are strings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from prometheos.domain.ids import RecordId, kind_of
from prometheos.domain.models import (
    MAX_INT,
    MAX_REFS,
    EvidenceRef,
    ExactlyOne,
    JsonObject,
    Locator,
    NonEmptyText,
    PdfLocator,
    RecordRef,
    Sha256,
    StrictModel,
    Text,
    TextLocator,
    Timestamp,
    Title,
)

Alias = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
# A05, CP18.1 (spec/03): a work-item label is 1-40 characters with no surrounding whitespace,
# no comma (the hub separates labels with commas), no C0/C1 control character, no line or
# paragraph separator and no bidirectional control (which could make a label display
# misleadingly; decision 0034).
_LABEL_BAD = r",\x00-\x1f\x7f-\x9f\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069"
WorkLabel = Annotated[
    str,
    StringConstraints(
        pattern=rf"^[^\s{_LABEL_BAD}](?:[^{_LABEL_BAD}]{{0,38}}[^\s{_LABEL_BAD}])?$",
    ),
]
MAX_WORK_LABELS = 16
MAX_WORK_DESCRIPTION = 20_000
RelPath = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


# ----- project settings ----------------------------------------------------------


class ResearchRoot(StrictModel):
    alias: Alias
    path: Annotated[str, StringConstraints(min_length=1, max_length=1024)]

    @field_validator("path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        from pathlib import Path, PureWindowsPath

        if (
            "<" in value
            or ">" in value
            or not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute())
        ):
            raise ValueError("research root must be an absolute path, never a redaction label")
        return value


class RetentionPrefs(StrictModel):
    default_retention: Literal["copy", "reference"]
    max_media_bytes: int = Field(ge=1, le=64 * 1024**3)


class ContextDefaults(StrictModel):
    budget_chars: int = Field(ge=2000, le=64000)
    reserve_chars: int = Field(ge=0, le=16000)


# Windows device names a folder cannot take (review cp20-3-a-r2, round 3).
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


class VerificationBudget(StrictModel):
    """A06.1 (CP20.3, decision 0084): the owner's budget for read-only checks an agent may start
    without a per-item question. Changed only by the owner (a settings change is protected)."""

    max_minutes_each: int = Field(default=15, ge=1, le=1440)
    max_minutes_per_day: int = Field(default=60, ge=1, le=1440)
    output_root_alias: Alias | None = None
    output_path: RelPath | None = None

    @model_validator(mode="after")
    def _folder(self) -> VerificationBudget:
        if (self.output_root_alias is None) != (self.output_path is None):
            raise ValueError("the output folder names both a research root and a path inside it")
        if self.output_path is not None:
            # Review cp20-3-a-r2 R2-2: a path inside the root, never absolute or climbing out.
            parts = self.output_path.replace("\\", "/").split("/")
            if (
                self.output_path.startswith(("/", "\\"))
                or ":" in self.output_path
                or any(part in ("", ".", "..") for part in parts)
                or any(ord(ch) < 32 for ch in self.output_path)
                or any(part.split(".")[0].upper() in _RESERVED_NAMES for part in parts)
            ):
                raise ValueError("the output folder is a relative path inside the research root")
        if self.max_minutes_each > self.max_minutes_per_day:
            raise ValueError("one check cannot exceed the daily budget")
        return self


DEFAULT_VERIFICATION_BUDGET = VerificationBudget()


class VerificationBudgetChange(StrictModel):
    """The hub's budget control (A06.1, CP20.4; gap audit #16): the whole budget the owner
    wants, and for the apply the digest of the preview they saw. The hub builds the same
    ``project_update`` as ``prometheos verify budget`` from the stored settings, so no research
    root path travels through the browser."""

    budget: VerificationBudget
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


class ProjectSettings(StrictModel):
    title: Title
    current_objective: NonEmptyText
    objective_entry_ref: RecordRef | None
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None
    allowed_roots: list[ResearchRoot] = Field(max_length=50)
    retention: RetentionPrefs
    context_defaults: ContextDefaults
    policy_version: ExactlyOne
    # A06.1, CP20.3: absent means the default budget (15 minutes each, 60 a day, no folder).
    verification_budget: VerificationBudget | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @field_validator("timezone")
    @classmethod
    def _iana(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _unique_aliases(self) -> ProjectSettings:
        aliases = [root.alias for root in self.allowed_roots]
        if len(aliases) != len(set(aliases)):
            raise ValueError("research root aliases must be unique")
        if self.objective_entry_ref and kind_of(self.objective_entry_ref.id) != "entry":
            raise ValueError("objective_entry_ref must reference an entry")
        budget = self.verification_budget
        if budget and budget.output_root_alias and budget.output_root_alias not in aliases:
            # Review cp20-3-a F4: the checks' output folder is inside an approved research root.
            raise ValueError("the verification output folder must be in an approved research root")
        return self


def default_settings(title: str, objective: str, timezone: str | None) -> ProjectSettings:
    return ProjectSettings(
        title=title,
        current_objective=objective,
        objective_entry_ref=None,
        timezone=timezone,
        allowed_roots=[],
        retention=RetentionPrefs(default_retention="reference", max_media_bytes=2 * 1024**3),
        context_defaults=ContextDefaults(budget_chars=12000, reserve_chars=2000),
        policy_version=1,
    )


# ----- sources and versions ----------------------------------------------------


class RootBinding(StrictModel):
    alias: Alias
    path: RelPath


class SourceBody(StrictModel):
    title: Title
    origin: Literal["file", "url", "typed", "recording", "agent_receipt", "adapter"]
    origin_uri: Annotated[str, StringConstraints(max_length=2048)] | None
    root_binding: RootBinding | None
    retention: Literal["copy", "reference"]
    description: Text

    @field_validator("origin_uri")
    @classmethod
    def _no_credentials(cls, value: str | None) -> str | None:
        return reject_credentials(value, "origin_uri")


CREDENTIAL_MARKERS = ("token=", "key=", "secret=", "password=", "sig=", "signature=")


def reject_credentials(value: str | None, field: str) -> str | None:
    """Refuse a URL-like value carrying user info or credential-bearing parameters."""
    if value is None:
        return None
    lowered = value.lower()
    _scheme, sep, rest = lowered.partition("://")
    if sep and "@" in rest.split("/", 1)[0]:
        raise ValueError(f"{field} must not contain credentials")
    for marker in CREDENTIAL_MARKERS:
        if marker in lowered:
            raise ValueError(f"{field} must not contain credential-bearing parameters")
    return value


class Transformation(StrictModel):
    type: Literal["extraction", "transcription", "conversion"]
    engine: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    engine_version: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    model: Annotated[str, StringConstraints(max_length=200)] | None
    language: Annotated[str, StringConstraints(max_length=35)] | None
    parameters_digest: Sha256
    input_version_refs: list[RecordId] = Field(max_length=MAX_REFS)
    run_id: RecordId | None


class VersionBody(StrictModel):
    source_id: RecordId
    sha256: Sha256
    byte_length: int = Field(ge=0)
    mime: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    captured_at: Timestamp
    storage: Literal["body", "media", "external"]
    storage_key: Annotated[str, StringConstraints(max_length=200)] | None
    availability: Literal["retained", "external", "missing"]
    extraction: Literal["none", "pending", "complete", "failed"]
    derived_from: EvidenceRef | None
    # CP11 S11.1 adds pdf_pages_v1: the per-page text extracted from a PDF version (decision 0060)
    payload_schema: Literal["text", "transcript_v1", "execution_receipt_v1", "pdf_pages_v1"] | None
    transformation: Transformation | None

    @model_validator(mode="after")
    def _consistent(self) -> VersionBody:
        if kind_of(self.source_id) != "source":
            raise ValueError("version.source_id must reference a source")
        if self.storage == "external":
            if self.storage_key is not None or self.availability == "retained":
                raise ValueError("external storage cannot claim retained bytes")
        elif self.storage_key is None or self.availability != "retained":
            raise ValueError("retained storage requires a storage key and availability=retained")
        return self


# ----- execution receipts (spec/03 "Execution receipts"; decision 0009) -------------

ShortField = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ReceiptTool(StrictModel):
    name: ShortField
    version: ShortField


class ReceiptEnvironment(StrictModel):
    os: ShortField
    tools: list[ReceiptTool] = Field(max_length=50)


class ReceiptArtifact(StrictModel):
    """One input or output, pinned to an exact stored version by its digest."""

    source_id: RecordId
    version_id: RecordId
    sha256: Sha256
    role: ShortField

    @model_validator(mode="after")
    def _kinds(self) -> ReceiptArtifact:
        if kind_of(self.source_id) != "source" or kind_of(self.version_id) != "version":
            raise ValueError("receipt artifacts reference a source and one of its versions")
        return self


class ExecutionReceiptV1(StrictModel):
    """What an external agent reports it ran. PrometheOS records it; it never upgrades a
    report to an observation, and intact bytes do not establish scientific validity."""

    schema_version: ExactlyOne
    observation: Literal["reported"]
    reporter: ShortField
    command: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    working_root: Alias | None
    environment: ReceiptEnvironment
    started_at: Timestamp | None
    ended_at: Timestamp | None
    exit_status: int | None = Field(ge=-(2**31), le=2**32 - 1)
    outcome: Literal["succeeded", "failed", "unknown"]
    inputs: list[ReceiptArtifact] = Field(max_length=MAX_REFS)
    outputs: list[ReceiptArtifact] = Field(max_length=MAX_REFS)
    limitations: list[Annotated[str, StringConstraints(min_length=1, max_length=2000)]] = Field(
        max_length=50
    )

    @model_validator(mode="after")
    def _consistent(self) -> ExecutionReceiptV1:
        if self.started_at and self.ended_at:
            # Fractional seconds vary in length, so compare instants, not strings.
            start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
            if end < start:
                raise ValueError("ended_at precedes started_at")
        if self.outcome == "succeeded" and self.exit_status not in (None, 0):
            raise ValueError("a succeeded run cannot report a nonzero exit status")
        return self


# ----- notebook entries ----------------------------------------------------------


class QuantityBinding(StrictModel):
    decimal: Annotated[str, StringConstraints(pattern=r"^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$")]
    unit: Annotated[str, StringConstraints(max_length=64)]
    source_ref: EvidenceRef
    source_pointer: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    # A06.1 (CP20.1): what the number is, so two bindings of the same quantity can be compared
    # (conflicting) and two definitions never are. Optional; a binding without them serialises
    # exactly as before.
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    definition: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )


# ``track`` (A06.1, CP20.2; decision 0083): a line of work that claims and questions draw on.
EntryCategory = Literal[
    "note", "finding", "experiment", "decision", "question", "assumption", "track"
]


class EntryBody(StrictModel):
    title: Title
    text: Text
    # ``assumption`` (A02, CP10): an attributed assumption or constraint that claims and
    # entries rely on through ``depends_on`` relations (spec/03 RP-00 mapping).
    category: EntryCategory
    evidence: list[EvidenceRef] = Field(max_length=MAX_REFS)
    claim_refs: list[RecordRef] = Field(max_length=MAX_REFS)
    author_ref: RecordRef | None
    origin: Literal["typed", "imported", "agent", "generated"]
    quantities: list[QuantityBinding] = Field(max_length=MAX_REFS)
    review: Literal["unreviewed", "reviewed"]
    # Protected (A02, CP10): set only by applying a correction whose ``supersede`` effect
    # names this assumption entry and its replacement assumption. Entries recorded before
    # schema 7 have no such key and read it as null (decision 0015).
    superseded_by: RecordRef | None = None

    @model_validator(mode="after")
    def _supersession(self) -> EntryBody:
        if self.superseded_by is not None:
            if self.category != "assumption":
                raise ValueError("only assumption entries can be superseded")
            if kind_of(self.superseded_by.id) != "entry":
                raise ValueError("an assumption is superseded by another assumption entry")
        return self


# ----- CP2: scoped claims, assessments and corrections ------------------------------

ScopeText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class ClaimScope(StrictModel):
    system: ScopeText
    operating_regime: ScopeText
    comparator: ScopeText
    model_fidelity: ScopeText
    validation_level: ScopeText
    # A02, CP10: the research objective the finding answers. Nullable and optional on input:
    # claims recorded earlier have no such key and read it as not recorded (null).
    objective: ScopeText | None = None


ClaimState = Literal[
    "proposed",
    "supported_within_conditions",
    "inconclusive",
    "contested",
    "superseded",
    "withdrawn",
]


class ClaimBody(StrictModel):
    statement: NonEmptyText
    scope: ClaimScope
    evidence: list[EvidenceRef] = Field(max_length=MAX_REFS)
    assessment_refs: list[RecordRef] = Field(max_length=MAX_REFS)
    state: ClaimState
    superseded_by: RecordRef | None
    # A06.1 (CP20.1): a one-line lead for a card, and the claim's own bound quantities. The
    # statement stays the authority; every number in the summary must be one of the bound
    # quantities (checked on write). Optional; absent values serialise exactly as before.
    summary: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    quantities: list[QuantityBinding] = Field(
        default_factory=list, max_length=MAX_REFS, exclude_if=lambda v: not v
    )

    @model_validator(mode="after")
    def _consistent(self) -> ClaimBody:
        if (self.state == "superseded") != (self.superseded_by is not None):
            raise ValueError("superseded claims (and only those) name superseded_by")
        if self.superseded_by is not None and kind_of(self.superseded_by.id) != "claim":
            raise ValueError("superseded_by must reference a claim")
        for ref in self.assessment_refs:
            if kind_of(ref.id) != "assessment":
                raise ValueError("assessment_refs must reference assessments")
        return self


CheckValue = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class AssessmentCheck(StrictModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    result: Literal["pass", "fail", "unknown"]
    evidence: list[EvidenceRef] = Field(max_length=MAX_REFS)
    # A06.1 (CP20.1): a reproduction check states what it expected, what it measured and the
    # tolerance it judged by (text; decimals stay text). Optional; absent values serialise as
    # before.
    expected: CheckValue | None = Field(default=None, exclude_if=lambda v: v is None)
    measured: CheckValue | None = Field(default=None, exclude_if=lambda v: v is None)
    tolerance: CheckValue | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _measured(self) -> AssessmentCheck:
        if self.result == "pass" and self.expected is not None and self.measured is None:
            raise ValueError("a passing check naming an expected value must state the measured one")
        return self


class AssessmentBody(StrictModel):
    claim_ref: RecordRef
    outcome: Literal["supports", "challenges", "inconclusive"]
    rationale: NonEmptyText
    checks: list[AssessmentCheck] = Field(max_length=MAX_REFS)
    author_ref: RecordRef | None
    method: Literal["researcher", "reported_agent", "deterministic_check"]
    scope_match: bool

    @model_validator(mode="after")
    def _kinds(self) -> AssessmentBody:
        if kind_of(self.claim_ref.id) != "claim":
            raise ValueError("claim_ref must reference a claim")
        return self


class CorrectionEffect(StrictModel):
    target_ref: RecordRef
    effect: Literal["withdraw", "contest", "supersede", "replace_text"]
    replacement_ref: RecordRef | None
    replacement_text: Text | None

    @model_validator(mode="after")
    def _shape(self) -> CorrectionEffect:
        target_kind = kind_of(self.target_ref.id)
        if self.effect == "supersede":
            # Claims are superseded by claims; from CP10 (A02) an assumption entry by another
            # assumption entry (the category is checked against the store, not here).
            if target_kind not in ("claim", "entry"):
                raise ValueError("only claims and assumption entries can be superseded")
            if self.replacement_ref is None or kind_of(self.replacement_ref.id) != target_kind:
                raise ValueError(
                    "supersede requires a replacement of the same kind (claim or assumption entry)"
                )
        elif self.effect == "replace_text":
            if target_kind not in ("entry", "item") or self.replacement_text is None:
                raise ValueError("replace_text needs an entry/item target and replacement_text")
        elif target_kind != "claim":
            raise ValueError(f"{self.effect} applies to claims")
        if self.effect != "supersede" and self.replacement_ref is not None:
            raise ValueError("replacement_ref is only used by supersede")
        if self.effect != "replace_text" and self.replacement_text is not None:
            raise ValueError("replacement_text is only used by replace_text")
        return self


class CorrectionBody(StrictModel):
    target_refs: list[RecordRef] = Field(min_length=1, max_length=MAX_REFS)
    replacement_refs: list[RecordRef] = Field(max_length=MAX_REFS)
    reason: NonEmptyText
    evidence: list[EvidenceRef] = Field(max_length=MAX_REFS)
    effects: list[CorrectionEffect] = Field(min_length=1, max_length=MAX_REFS)
    status: Literal["proposed", "applied", "rejected"]
    applied_seq: int | None = Field(ge=1, le=MAX_INT)

    @model_validator(mode="after")
    def _consistent(self) -> CorrectionBody:
        targets = {r.id: r.rev for r in self.target_refs}
        replacements = {r.id for r in self.replacement_refs}
        if len(targets) != len(self.target_refs):
            raise ValueError("duplicate correction target")
        if targets.keys() & replacements:
            raise ValueError("a record cannot be both target and replacement")
        effect_targets = [e.target_ref.id for e in self.effects]
        if len(set(effect_targets)) != len(effect_targets):
            raise ValueError("at most one effect per target")
        if set(effect_targets) != set(targets):
            raise ValueError("every target needs exactly one effect")
        for effect in self.effects:
            if targets[effect.target_ref.id] != effect.target_ref.rev:
                raise ValueError("effect target revision differs from target_refs")
            if effect.replacement_ref and effect.replacement_ref.id not in replacements:
                raise ValueError("effect replacement must be listed in replacement_refs")
        if (self.status == "applied") != (self.applied_seq is not None):
            raise ValueError("applied_seq is set exactly when the correction is applied")
        return self


# ----- CP2: people, meetings, items and actions ---------------------------------------


class PersonBody(StrictModel):
    display_name: Title
    affiliation: Annotated[str, StringConstraints(max_length=300)] | None
    contact: Annotated[str, StringConstraints(max_length=500)] | None
    notes: Text


class RoleBody(StrictModel):
    person_id: RecordId
    scope_id: RecordId
    role: Literal["researcher", "supervisor", "collaborator", "reviewer", "administrator", "other"]
    label: Annotated[str, StringConstraints(max_length=200)] | None
    effective_from: Timestamp | None
    effective_until: Timestamp | None

    @model_validator(mode="after")
    def _kinds(self) -> RoleBody:
        if kind_of(self.person_id) != "person":
            raise ValueError("person_id must reference a person")
        if kind_of(self.scope_id) not in ("project", "paper"):
            raise ValueError("roles are scoped to a project or paper")
        if (
            self.effective_from
            and self.effective_until
            and self.effective_until < self.effective_from
        ):
            raise ValueError("effective_until precedes effective_from")
        return self


class MeetingBody(StrictModel):
    title: Title
    held_at: Timestamp | None
    participant_refs: list[RecordRef] = Field(max_length=MAX_REFS)
    source_version_refs: list[RecordRef] = Field(max_length=MAX_REFS)
    summary_entry_ref: RecordRef | None
    processing: Literal["captured", "proposed", "reviewed"]

    @model_validator(mode="after")
    def _kinds(self) -> MeetingBody:
        if any(kind_of(r.id) != "person" for r in self.participant_refs):
            raise ValueError("participants must be people")
        if any(kind_of(r.id) != "version" for r in self.source_version_refs):
            raise ValueError("meeting sources must be source versions")
        if self.summary_entry_ref and kind_of(self.summary_entry_ref.id) != "entry":
            raise ValueError("summary_entry_ref must reference an entry")
        return self


class ItemBody(StrictModel):
    meeting_id: RecordId | None
    type: Literal["observation", "suggestion", "request", "decision", "commitment", "question"]
    text: NonEmptyText
    evidence: list[EvidenceRef] = Field(max_length=MAX_REFS)
    proposed_owner: RecordRef | None
    due_at: Timestamp | None
    status: Literal["proposed", "adopted", "dismissed", "superseded"]
    action_id: RecordId | None

    @model_validator(mode="after")
    def _kinds(self) -> ItemBody:
        if self.meeting_id and kind_of(self.meeting_id) != "meeting":
            raise ValueError("meeting_id must reference a meeting")
        if self.proposed_owner and kind_of(self.proposed_owner.id) != "person":
            raise ValueError("proposed_owner must be a person")
        if self.action_id and kind_of(self.action_id) != "action":
            raise ValueError("action_id must reference an action")
        if (self.status == "adopted") != (self.action_id is not None):
            raise ValueError("adopted items (and only those) link their action")
        return self


ActionStatus = Literal["proposed", "ready", "in_progress", "blocked", "done", "cancelled"]


VerificationKind = Literal["read_only", "rerun", "model_edit", "literature"]


class Verification(StrictModel):
    """A06.1 (CP20.3, decision 0084): the check an action is. Only ``read_only`` can be
    budget-eligible; who created the action and who adopted it are read from the record.

    A check is about a claim, or (CP20.4 6c, decision 0106) about one adoption plan step that the
    coding agent can do (a comparison, a tool extract, a rerun): exactly one of ``claim_ref`` and
    ``adoption_step``. Both are serialized only when set, so earlier bodies keep their bytes."""

    claim_ref: RecordRef | None = Field(default=None, exclude_if=lambda v: v is None)
    adoption_step: Annotated[str, StringConstraints(pattern=r"^st_[0-9a-f]{16}$")] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    kind: VerificationKind
    estimated_minutes: int | None = Field(default=None, ge=1, le=100_000)
    expected: Annotated[str, StringConstraints(max_length=2000)] | None = None

    @model_validator(mode="after")
    def _claim(self) -> Verification:
        if (self.claim_ref is None) == (self.adoption_step is None):
            raise ValueError("a verification checks one claim or one adoption plan step")
        if self.claim_ref is not None and kind_of(self.claim_ref.id) != "claim":
            raise ValueError("a verification checks a claim")
        return self


class ActionBody(StrictModel):
    title: Title
    reason: NonEmptyText
    source_refs: list[RecordRef] = Field(max_length=MAX_REFS)
    owner_ref: RecordRef | None
    due_at: Timestamp | None
    dependencies: list[RecordId] = Field(max_length=MAX_REFS)
    status: ActionStatus
    blocker: Text | None
    completion_evidence: list[EvidenceRef] = Field(max_length=MAX_REFS)
    commitment_item_id: RecordId | None
    # A05, CP18.1: an action is a work item. Both fields are optional and additive: actions
    # stored before A05 validate unchanged and gain them only when next written. The
    # description is the researcher's untrusted text (what is wanted), never instructions.
    description: Annotated[str, StringConstraints(max_length=MAX_WORK_DESCRIPTION)] | None = None
    labels: list[WorkLabel] = Field(default_factory=list, max_length=MAX_WORK_LABELS)
    # A06.1, CP20.3: a verification action (a planned check). Absent on every other action.
    verification: Verification | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _kinds(self) -> ActionBody:
        if len({label.casefold() for label in self.labels}) != len(self.labels):
            raise ValueError("duplicate label")
        if self.owner_ref and kind_of(self.owner_ref.id) != "person":
            raise ValueError("owner_ref must be a person")
        if any(kind_of(d) != "action" for d in self.dependencies):
            raise ValueError("dependencies must be actions")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("duplicate dependency")
        if self.commitment_item_id and kind_of(self.commitment_item_id) != "item":
            raise ValueError("commitment_item_id must reference an item")
        if (self.status == "blocked") != bool(self.blocker):
            raise ValueError("blocked actions (and only those) state a blocker")
        return self


class TaskBody(StrictModel):
    purpose: Literal["meeting_extract", "notebook_draft", "next_steps", "paper_check"]
    input_seq: int = Field(ge=0, le=MAX_INT)
    input_refs: list[RecordRef] = Field(max_length=2000)
    instructions_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    context_digest: Sha256
    created_at: Timestamp
    expires_at: Timestamp | None
    status: Literal["prepared", "submitted", "stale", "cancelled"]
    result_request_id: Annotated[str, StringConstraints(max_length=64)] | None


class PackageBody(StrictModel):
    """A package installed into this project's host projection (spec/03, spec/07)."""

    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)]
    version: Annotated[str, StringConstraints(min_length=5, max_length=100)]
    origin: JsonObject
    digest: Sha256
    license_expression: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None
    lock_path: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    status: Literal["available", "installed", "quarantined", "removed"]


StepRef = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class CheckResult(StrictModel):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}\.[0-9]{1,2}$")]
    result: Literal["pass", "fail", "unknown"]
    evidence: list[EvidenceRef] = Field(max_length=50)


class RunStep(StrictModel):
    id: StepRef
    status: Literal["pending", "in_progress", "blocked", "done", "skipped"]
    artifact_refs: list[RecordRef] = Field(max_length=100)
    check_results: list[CheckResult] = Field(max_length=50)
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None


class RunBody(StrictModel):
    """Host-reported progress through a recipe; PrometheOS never executes the steps."""

    package_ref: RecordRef
    package_digest: Sha256
    recipe_id: StepRef
    inputs: JsonObject
    input_digest: Sha256
    steps: list[RunStep] = Field(min_length=1, max_length=100)
    status: Literal["planned", "active", "blocked", "completed", "cancelled"]
    outputs: JsonObject | None


# ----- CP7: papers, anchors and recurring jobs ------------------------------------------

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]

PaperStatus = Literal[
    "planning",
    "drafting",
    "internal_review",
    "ready_for_submission",
    "submitted",
    "revision",
    "accepted",
    "withdrawn",
]


class Requirement(StrictModel):
    """One source-derived venue requirement row; unknown until someone checks it."""

    id: Slug
    text: ShortText
    source_ref: EvidenceRef
    applicability: Literal["unknown", "applicable", "not_applicable"]
    last_checked_at: Timestamp | None
    status: Literal["unknown", "todo", "met", "not_applicable"]
    evidence: list[EvidenceRef] = Field(max_length=50)

    @model_validator(mode="after")
    def _consistent(self) -> Requirement:
        if self.applicability == "unknown" and self.status != "unknown":
            raise ValueError("a requirement of unknown applicability has unknown status")
        if (self.status == "not_applicable") != (self.applicability == "not_applicable"):
            raise ValueError("status not_applicable goes with applicability not_applicable")
        if self.status == "met" and (self.applicability != "applicable" or not self.evidence):
            raise ValueError("a met requirement is applicable and cites evidence")
        if self.status != "unknown" and self.last_checked_at is None:
            raise ValueError("a checked requirement records last_checked_at")
        return self


class RelatedPaper(StrictModel):
    paper_id: RecordId
    relationship: Literal["shares_evidence", "extends", "potential_overlap"]
    explanation: ShortText

    @model_validator(mode="after")
    def _kind(self) -> RelatedPaper:
        if kind_of(self.paper_id) != "paper":
            raise ValueError("related_papers must reference papers")
        return self


class ReviewItem(StrictModel):
    id: Slug
    comment_source_ref: EvidenceRef
    text: NonEmptyText
    response_text: Text
    change_refs: list[RecordRef] = Field(max_length=50)
    status: Literal["open", "drafted", "resolved"]
    resolution_evidence: list[EvidenceRef] = Field(max_length=50)


class ReviewRound(StrictModel):
    id: Slug
    label: Title
    source_refs: list[RecordRef] = Field(max_length=50)
    received_at: Timestamp | None
    items: list[ReviewItem] = Field(max_length=MAX_REFS)

    @model_validator(mode="after")
    def _consistent(self) -> ReviewRound:
        if any(kind_of(r.id) != "version" for r in self.source_refs):
            raise ValueError("review round sources are source versions")
        ids = [i.id for i in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("review item ids are unique within a round")
        return self


class PaperBody(StrictModel):
    title: Title
    objective: NonEmptyText
    claim_refs: list[RecordRef] = Field(max_length=MAX_REFS)
    manuscript_source_id: RecordId | None
    venue_source_refs: list[RecordRef] = Field(max_length=50)
    status: PaperStatus
    deadline: Timestamp | None
    requirements: list[Requirement] = Field(max_length=MAX_REFS)
    contribution: Text
    related_papers: list[RelatedPaper] = Field(max_length=50)
    review_rounds: list[ReviewRound] = Field(max_length=50)

    @model_validator(mode="after")
    def _consistent(self) -> PaperBody:
        if any(kind_of(r.id) != "claim" for r in self.claim_refs):
            raise ValueError("claim_refs must reference claims")
        if self.manuscript_source_id and kind_of(self.manuscript_source_id) != "source":
            raise ValueError("manuscript_source_id must reference a source")
        if any(kind_of(r.id) != "version" for r in self.venue_source_refs):
            raise ValueError("venue sources are exact source versions")
        venue_versions = {r.id for r in self.venue_source_refs}
        ids = [r.id for r in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("requirement ids are unique within a paper")
        for row in self.requirements:
            if row.source_ref.version_id not in venue_versions:
                raise ValueError(f"requirement {row.id} must cite one of the venue sources")
        related = [r.paper_id for r in self.related_papers]
        if len(related) != len(set(related)):
            raise ValueError("a related paper is listed once")
        rounds = [r.id for r in self.review_rounds]
        if len(rounds) != len(set(rounds)):
            raise ValueError("review round ids are unique within a paper")
        return self


class AnchorBody(StrictModel):
    paper_id: RecordId
    manuscript_version_id: RecordId
    locator: Annotated[TextLocator | PdfLocator, Field(discriminator="type")]
    claim_refs: list[RecordRef] = Field(max_length=MAX_REFS)
    evidence: list[EvidenceRef] = Field(max_length=MAX_REFS)
    status: Literal["current", "needs_review", "resolved"]
    reason: Text | None

    @model_validator(mode="after")
    def _consistent(self) -> AnchorBody:
        if kind_of(self.paper_id) != "paper":
            raise ValueError("paper_id must reference a paper")
        if kind_of(self.manuscript_version_id) != "version":
            raise ValueError("manuscript_version_id must reference a source version")
        if any(kind_of(r.id) != "claim" for r in self.claim_refs):
            raise ValueError("claim_refs must reference claims")
        if self.status == "needs_review" and not (self.reason or "").strip():
            raise ValueError("an anchor needing review states why")
        return self


class SourceRefreshParams(StrictModel):
    source_ids: list[RecordId] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _kinds(self) -> SourceRefreshParams:
        if any(kind_of(s) != "source" for s in self.source_ids):
            raise ValueError("source_refresh watches sources")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("duplicate source")
        return self


class PaperCheckParams(StrictModel):
    paper_id: RecordId

    @model_validator(mode="after")
    def _kind(self) -> PaperCheckParams:
        if kind_of(self.paper_id) != "paper":
            raise ValueError("paper_check needs a paper")
        return self


class DigestParams(StrictModel):
    pass


class IntervalSchedule(StrictModel):
    type: Literal["interval"]
    minutes: int = Field(ge=15, le=10080)
    start_utc: Timestamp


Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class WeeklySchedule(StrictModel):
    type: Literal["weekly"]
    day: Weekday
    time: Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
    zone: Annotated[str, StringConstraints(min_length=1, max_length=64)]

    @field_validator("zone")
    @classmethod
    def _iana(cls, value: str) -> str:
        if value != value.strip() or ("/" not in value and value != "UTC"):
            raise ValueError("zone must be an explicit IANA name such as Europe/London")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value


class JobBudget(StrictModel):
    max_inputs: int = Field(ge=1, le=500)
    max_output_chars: int = Field(ge=100, le=64000)
    max_provider_calls: int = Field(ge=0, le=20)
    max_concurrent_runs: ExactlyOne
    spend_cap_estimate_usd: (
        Annotated[str, StringConstraints(pattern=r"^\d{1,6}(\.\d{1,2})?$")] | None
    )


JOB_PARAMS: dict[str, type[StrictModel]] = {
    "source_refresh": SourceRefreshParams,
    "paper_check": PaperCheckParams,
    "digest": DigestParams,
}


class JobBody(StrictModel):
    """A fixed-handler schedule (spec/08). Slots and leases live in their own tables."""

    handler: Literal["source_refresh", "paper_check", "digest"]
    parameters: SourceRefreshParams | PaperCheckParams | DigestParams
    schedule: Annotated[IntervalSchedule | WeeklySchedule, Field(discriminator="type")]
    enabled: bool
    budget: JobBudget
    last_slot: Timestamp | None

    @model_validator(mode="after")
    def _params(self) -> JobBody:
        if not isinstance(self.parameters, JOB_PARAMS[self.handler]):
            raise ValueError(f"parameters do not match the {self.handler} handler")
        if (
            isinstance(self.parameters, SourceRefreshParams)
            and len(self.parameters.source_ids) > self.budget.max_inputs
        ):
            raise ValueError("more watched sources than the budget's max_inputs")
        return self


# ----- CP10 (A02): observe-only external scientific executions -------------------------

ExecutionState = Literal[
    "registered",
    "reported_running",
    "reported_completed",
    "reported_failed",
    "reported_cancelled",
    "status_unknown",
]


class ExecutionBody(StrictModel):
    """A run owned by a coding agent or another tool, observed, never controlled (spec/03).

    States are what was reported (no app-controlled observer exists yet); silence or a
    timeout becomes ``status_unknown``, never failed or completed. Whether a completed run
    awaits interpretation is derived from ``cites`` relations, never stored. Registering or
    reporting never starts, stops or restarts anything."""

    title: Title
    host_label: ShortField
    reporter: ShortField
    command_summary: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None
    working_root_alias: Alias | None
    # An external job label (scheduler job ID, run name), never a credential.
    external_ref: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None
    expected_outputs: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        max_length=50
    )
    state: ExecutionState
    last_reported_at: Timestamp | None
    # Execution-receipt source versions (payload_schema execution_receipt_v1) of its outputs.
    receipt_refs: list[RecordRef] = Field(max_length=MAX_REFS)

    @field_validator("external_ref")
    @classmethod
    def _no_credentials(cls, value: str | None) -> str | None:
        return reject_credentials(value, "external_ref")

    @model_validator(mode="after")
    def _consistent(self) -> ExecutionBody:
        if self.state.startswith("reported_") and self.last_reported_at is None:
            raise ValueError("a reported state records last_reported_at")
        if any(kind_of(r.id) != "version" for r in self.receipt_refs):
            raise ValueError("receipt_refs reference execution-receipt source versions")
        if len({r.id for r in self.receipt_refs}) != len(self.receipt_refs):
            raise ValueError("duplicate receipt reference")
        return self


# ----- bibliographic works (A02, CP11 S11.1; decision 0060) -----------------------------------

WorkText = Annotated[str, StringConstraints(min_length=1, max_length=300)]
# Normalised identifiers (spec/03): lowercase DOI without a resolver prefix; an arXiv identifier
# without its version suffix; PubMed and PubMed Central numbers; an ISBN of digits (and X).
Doi = Annotated[str, StringConstraints(pattern=r"^10\.[0-9]{4,9}/[!-~]{1,300}$", max_length=320)]
Arxiv = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9]{4}\.[0-9]{4,5}|[a-z][a-z.-]{1,30}/[0-9]{7})$"),
]
Pmid = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,9}$")]
Pmcid = Annotated[str, StringConstraints(pattern=r"^PMC[1-9][0-9]{0,9}$")]
Isbn = Annotated[str, StringConstraints(pattern=r"^(?:[0-9]{9}[0-9X]|[0-9]{13})$")]


class OtherIdentifier(StrictModel):
    scheme: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    value: WorkText


class WorkIdentifiers(StrictModel):
    doi: Doi | None
    arxiv: Arxiv | None
    pmid: Pmid | None
    pmcid: Pmcid | None
    isbn: Isbn | None
    other: list[OtherIdentifier] = Field(max_length=100)


class WorkContributor(StrictModel):
    name: WorkText
    role: Literal["author", "editor"]


WorkField = Literal[
    "title", "contributors", "year", "venue", "identifiers.doi", "identifiers.arxiv",
    "identifiers.pmid", "identifiers.pmcid", "identifiers.isbn", "abstract_ref", "entry_type",
]  # fmt: skip
WorkEntryType = Literal[
    "article", "inproceedings", "book", "incollection", "phdthesis", "mastersthesis",
    "techreport", "misc", "unpublished", "online", "preprint",
]  # fmt: skip


def _absent(value: Any) -> bool:
    return value is None


class FieldProvenance(StrictModel):
    """Where one field of a work came from: a version and a locator in it. ``basis`` says how
    (decision 0061): the PDF information dictionary (a JSON pointer into the extracted
    version), a first-page text span (a PDF locator with character offsets), or the file name
    (no locator)."""

    field: WorkField
    version_ref: RecordRef
    locator: Locator | None
    basis: Literal["pdf_info", "first_page_text", "file_name", "bibliography", "zotero"]
    # CP11 S11.2 (decisions 0062, 0063): the value this source asserts (so every source's value
    # stays readable when another one takes precedence) and the entry's key in a bibliography
    # file. Both are omitted when absent, so S11.1 records serialise as before.
    value: Annotated[str, StringConstraints(max_length=2000)] | None = Field(
        default=None, exclude_if=_absent
    )
    entry: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = Field(
        default=None, exclude_if=_absent
    )

    @model_validator(mode="after")
    def _consistent(self) -> FieldProvenance:
        if kind_of(self.version_ref.id) != "version":
            raise ValueError("field provenance names a version")
        if (self.basis == "file_name") != (self.locator is None):
            raise ValueError("only a file-name basis has no locator")
        return self


class WorkBody(StrictModel):
    """A bibliographic work (spec/03, CP11). Imported bytes stay source versions; the work
    points at the extracted full text and says where each field came from. In S11.1 a work is
    registered only by the library importer."""

    title: Title
    contributors: list[WorkContributor] = Field(max_length=200)
    year: int | None = Field(ge=1000, le=2200)
    venue: WorkText | None
    identifiers: WorkIdentifiers
    abstract_ref: EvidenceRef | None
    access_state: Literal["metadata_only", "abstract_only", "full_text", "unavailable"]
    full_text_refs: list[RecordRef] = Field(max_length=20)
    field_provenance: list[FieldProvenance] = Field(max_length=500)
    merged_into: RecordRef | None
    # CP11 S11.2: the bibliographic entry type (export); omitted when absent (S11.1 works).
    entry_type: WorkEntryType | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def _consistent(self) -> WorkBody:
        if any(kind_of(r.id) != "version" for r in self.full_text_refs):
            raise ValueError("full_text_refs reference versions")
        if len({r.id for r in self.full_text_refs}) != len(self.full_text_refs):
            raise ValueError("duplicate full-text reference")
        if self.access_state == "full_text" and not self.full_text_refs:
            raise ValueError("a full_text work names its full-text version")
        if self.merged_into is not None and kind_of(self.merged_into.id) != "work":
            raise ValueError("merged_into references a work")
        return self


# ----- CP11 S11.4: the evidence matrix (decision 0064) -----------------------------------------

ColumnId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
ChoiceValue = Annotated[str, StringConstraints(min_length=1, max_length=200)]
MAX_MATRIX_COLUMNS = 50
MAX_MATRIX_WORKS = 500
MAX_CELL_EVIDENCE = 20
MAX_CELL_QUANTITIES = 20
MAX_CELL_TEXT = 4000
# An unsupported cell makes no value claim: its text is empty or says why nothing was found.
NOT_FOUND = "not found"


class MatrixColumn(StrictModel):
    """One column of the rubric. ``choices`` (an additive member, decision 0064) lists the
    allowed values of a choice column; it is omitted for the other kinds."""

    id: ColumnId
    label: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    value_kind: Literal["text", "quantity", "choice"]
    instructions: Annotated[str, StringConstraints(max_length=4000)]
    choices: list[ChoiceValue] | None = Field(default=None, max_length=50, exclude_if=_absent)

    @model_validator(mode="after")
    def _choices(self) -> MatrixColumn:
        if self.value_kind == "choice":
            if not self.choices:
                raise ValueError("a choice column lists its allowed values")
            if len(set(self.choices)) != len(self.choices):
                raise ValueError("duplicate choice value")
        elif self.choices is not None:
            raise ValueError("only a choice column lists choices")
        return self


class MatrixBody(StrictModel):
    """An evidence matrix (spec/03, CP11): a rubric of columns over a set of works. Its
    ``rubric_version`` rises whenever the columns change; rows extracted under an older rubric
    are stale."""

    title: Title
    question_ref: RecordRef | None
    columns: list[MatrixColumn] = Field(min_length=1, max_length=MAX_MATRIX_COLUMNS)
    rubric_version: int = Field(ge=1, le=MAX_INT)
    work_refs: list[RecordRef] = Field(max_length=MAX_MATRIX_WORKS)

    @model_validator(mode="after")
    def _consistent(self) -> MatrixBody:
        ids = [c.id for c in self.columns]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate column id")
        if any(kind_of(r.id) != "work" for r in self.work_refs):
            raise ValueError("work_refs reference works")
        if len({r.id for r in self.work_refs}) != len(self.work_refs):
            raise ValueError("a work is listed once")
        if self.question_ref is not None and kind_of(self.question_ref.id) != "entry":
            raise ValueError("question_ref references a question entry")
        return self


def _evidence_key(ev: EvidenceRef) -> str:
    return ev.model_dump_json()


class MatrixCell(StrictModel):
    """One cell: a value with the evidence it is quoted from, how it was found, and its review
    state. A cell without evidence can only be unsupported (C11-GROUNDING); quantities follow
    the binding rules and cite one of the cell's evidence references."""

    column_id: ColumnId
    value_text: Annotated[str, StringConstraints(max_length=MAX_CELL_TEXT)]
    quantities: list[QuantityBinding] = Field(max_length=MAX_CELL_QUANTITIES)
    evidence: list[EvidenceRef] = Field(max_length=MAX_CELL_EVIDENCE)
    basis: Literal["full_text", "abstract", "metadata"]
    state: Literal["proposed", "reviewed", "unsupported"]
    method: Literal["manual", "rule", "model"]
    transformation: Transformation | None

    @model_validator(mode="after")
    def _consistent(self) -> MatrixCell:
        if not self.evidence and self.state != "unsupported":
            raise ValueError("a cell without evidence can only be unsupported")
        if self.state == "unsupported":
            if self.quantities:
                raise ValueError("an unsupported cell binds no quantity")
            if self.value_text and not self.value_text.startswith(NOT_FOUND):
                raise ValueError(f"an unsupported cell's text is empty or starts '{NOT_FOUND}'")
        elif not self.value_text.strip():
            raise ValueError("a proposed or reviewed cell states its value")
        if self.method in ("rule", "model") and self.transformation is None:
            raise ValueError("rule and model cells name their transformation")
        if self.transformation is not None and self.transformation.type != "extraction":
            raise ValueError("a cell's transformation is an extraction")
        keys = {_evidence_key(ev) for ev in self.evidence}
        if len(keys) != len(self.evidence):
            raise ValueError("duplicate evidence reference")
        for quantity in self.quantities:
            if _evidence_key(quantity.source_ref) not in keys:
                raise ValueError("a quantity cites one of the cell's evidence references")
        return self


class ExtractionBody(StrictModel):
    """One matrix row: the cells extracted for one work under one matrix revision."""

    matrix_ref: RecordRef
    work_ref: RecordRef
    cells: list[MatrixCell] = Field(max_length=MAX_MATRIX_COLUMNS)

    @model_validator(mode="after")
    def _consistent(self) -> ExtractionBody:
        if kind_of(self.matrix_ref.id) != "matrix":
            raise ValueError("matrix_ref references a matrix")
        if kind_of(self.work_ref.id) != "work":
            raise ValueError("work_ref references a work")
        ids = [c.column_id for c in self.cells]
        if len(set(ids)) != len(ids):
            raise ValueError("one cell per column")
        return self


BODY_MODELS: dict[str, type[BaseModel]] = {
    "project": ProjectSettings,
    "source": SourceBody,
    "version": VersionBody,
    "entry": EntryBody,
    "claim": ClaimBody,
    "assessment": AssessmentBody,
    "correction": CorrectionBody,
    "person": PersonBody,
    "role": RoleBody,
    "meeting": MeetingBody,
    "item": ItemBody,
    "action": ActionBody,
    "task": TaskBody,
    "package": PackageBody,
    "run": RunBody,
    "paper": PaperBody,
    "anchor": AnchorBody,
    "job": JobBody,
    "execution": ExecutionBody,
    "work": WorkBody,
    "matrix": MatrixBody,
    "extraction": ExtractionBody,
}


def normalise_stored_body(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """Read-side view of a stored body: fields added after it was written read as null.

    Stored bodies are never rewritten (A02 alters no historical evidence); readers get the
    current shape. CP10 (decision 0015): claim ``scope.objective`` and entry
    ``superseded_by``. Unknown or purged bodies are returned unchanged."""
    if kind == "claim" and isinstance(body.get("scope"), dict) and "objective" not in body["scope"]:
        return {**body, "scope": {**body["scope"], "objective": None}}
    if kind == "entry" and "title" in body and "superseded_by" not in body:
        return {**body, "superseded_by": None}
    return body
