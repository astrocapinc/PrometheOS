"""The research event catalogue (A05, CP18.2; spec/04 "Work items, events and the agent-visible
queue"; decision 0035).

An event says that something needs attention: its ``type`` fixes the kind of its subject, a
closed ``data`` object and the code that emits it. Event data never carries record text or a
local path (no title, description, reason, note or summary line has a field here); readers
fetch details through the normal operations. ``Event`` is the contract schema ``event``
checked by ``tools/check_contracts.py``; the per-type schemas are pinned append-only by
``tests/test_cp18_events.py``: a type is added, never changed or removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, get_args

from pydantic import ConfigDict, Field, RootModel, StringConstraints, model_validator

from prometheos.domain.bodies import ActionStatus
from prometheos.domain.ids import (
    UUID4_PATTERN,
    ApprovalRequestId,
    OperationRunId,
    ProjectId,
    kind_id_pattern,
    kind_of,
)
from prometheos.domain.models import MAX_INT, CapabilityName, StrictModel, Timestamp

EventId = Annotated[str, StringConstraints(pattern=rf"^evt_{UUID4_PATTERN}$")]
# The record kinds as they were when the event catalogue was defined (CP18.2). Event types are
# never redefined in place (spec/04), so their schemas keep this alternation when a later
# checkpoint adds a kind (CP11 adds work: a work is neither an awaited result nor a dependent of a
# correction target; decision 0061 section 12).
DependentKind = Literal[
    "project", "source", "version", "entry", "claim", "assessment", "correction", "person",
    "role", "meeting", "item", "action", "paper", "anchor", "task", "job", "package", "run",
    "execution",
]  # fmt: skip
EventRecordId = Annotated[
    str,
    StringConstraints(pattern=rf"^(?:{'|'.join(get_args(DependentKind))})_{UUID4_PATTERN}$"),
]
# The same shapes as the operation-run row contract (store.operational), restated here so the
# domain layer does not import the store.
OperationName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
StageName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_:.-]{0,99}$")]
Actor = Annotated[str, StringConstraints(min_length=1, max_length=100)]
Note = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
Seq = Annotated[int, Field(ge=0, le=MAX_INT)]
Positive = Annotated[int, Field(ge=1, le=MAX_INT)]

ActionField = Literal[
    "title",
    "reason",
    "source_refs",
    "owner_ref",
    "due_at",
    "dependencies",
    "status",
    "blocker",
    "completion_evidence",
    "commitment_item_id",
    "description",
    "labels",
]
"""The fields of ``ActionBody`` (a test pins the two together)."""

SELF_ACKNOWLEDGED_TYPES = frozenset({"work_item.created", "work_item.changed"})
"""Types written already acknowledged when an owner-scope caller caused them (decision 0035,
item 4): the owner made the change; the item itself stays listed in the queue."""

OWN_CHANGE_NOTE = "own change"


ActionId = Annotated[str, StringConstraints(pattern=kind_id_pattern("action"))]
ClaimId = Annotated[str, StringConstraints(pattern=kind_id_pattern("claim"))]
CorrectionId = Annotated[str, StringConstraints(pattern=kind_id_pattern("correction"))]
SourceId = Annotated[str, StringConstraints(pattern=kind_id_pattern("source"))]


# ----- subjects ------------------------------------------------------------------------------


class ActionSubject(StrictModel):
    kind: Literal["action"]
    id: ActionId


class ApprovalSubject(StrictModel):
    kind: Literal["approval_request"]
    id: ApprovalRequestId


class ResultSubject(StrictModel):
    """A registered execution, or the receipt version when no execution was registered."""

    kind: Literal["execution", "version"]
    id: EventRecordId

    @model_validator(mode="after")
    def _matches(self) -> ResultSubject:
        if kind_of(self.id) != self.kind:
            raise ValueError("the subject id must be of the subject kind")
        return self


class CorrectionSubject(StrictModel):
    kind: Literal["correction"]
    id: CorrectionId


class ProjectSubject(StrictModel):
    kind: Literal["project"]
    id: ProjectId


class SourceSubject(StrictModel):
    kind: Literal["source"]
    id: SourceId


class RunSubject(StrictModel):
    kind: Literal["operation_run"]
    id: OperationRunId


class ClaimSubject(StrictModel):
    kind: Literal["claim"]
    id: ClaimId


# ----- closed data objects -------------------------------------------------------------------


class WorkItemCreatedData(StrictModel):
    status: ActionStatus
    due_at: Timestamp | None


class WorkItemChangedData(StrictModel):
    changed: list[ActionField] = Field(max_length=12)
    status: ActionStatus
    due_at: Timestamp | None

    @model_validator(mode="after")
    def _unique(self) -> WorkItemChangedData:
        if len(set(self.changed)) != len(self.changed):
            raise ValueError("duplicate field name")
        return self


class DeadlineData(StrictModel):
    due_at: Timestamp
    days_before: Literal[14, 7, 2]


class ApprovalRequestedData(StrictModel):
    operation: OperationName
    scope: CapabilityName


class ApprovalDecidedData(StrictModel):
    decision: Literal["granted", "denied"]


class AwaitingData(StrictModel):
    state: Literal["completed", "unregistered_receipt"]


class FlaggedDependentsData(StrictModel):
    dependents: dict[DependentKind, Positive] = Field(min_length=1)
    total: Positive

    @model_validator(mode="after")
    def _sum(self) -> FlaggedDependentsData:
        if sum(self.dependents.values()) != self.total:
            raise ValueError("total must equal the sum of the counts by kind")
        return self


class BackupGapData(StrictModel):
    uncovered_versions: Positive


class SourceUnavailableData(StrictModel):
    availability: Literal["unavailable"]
    versions: Positive


class RunOutcomeUnknownData(StrictModel):
    operation: OperationName
    stage: StageName


# ----- events --------------------------------------------------------------------------------


class ClaimNeedsReviewData(StrictModel):
    """The derived status the claim entered and its first cause's code (decision 0108)."""

    status: Literal["stale", "conflicting"]
    cause: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,39}$")]


class _EventBase(StrictModel):
    event_id: EventId
    occurred_at: Timestamp
    project_seq: Seq
    acknowledged_at: Timestamp | None
    acknowledged_by: Actor | None
    acknowledged_note: Note | None

    @model_validator(mode="after")
    def _acknowledgement(self) -> _EventBase:
        if (self.acknowledged_at is None) != (self.acknowledged_by is None):
            raise ValueError("an acknowledgement has both a time and an actor")
        if self.acknowledged_note is not None and self.acknowledged_at is None:
            raise ValueError("only an acknowledged event has an acknowledgement note")
        return self


def _emitter(text: str) -> ConfigDict:
    return ConfigDict(json_schema_extra={"x-emitter": text})


class WorkItemCreated(_EventBase):
    """A work item (an action) was created."""

    model_config = _emitter("every action create (apply create of kind action)")
    type: Literal["work_item.created"]
    subject_ref: ActionSubject
    data: WorkItemCreatedData


class WorkItemChanged(_EventBase):
    """A work item was revised or moved to another status."""

    model_config = _emitter("action revise or transition (apply)")
    type: Literal["work_item.changed"]
    subject_ref: ActionSubject
    data: WorkItemChangedData


class DeadlineApproaching(_EventBase):
    """An open work item's deadline is within 14, 7 or 2 days."""

    model_config = _emitter("deadline detector, once per deadline and threshold")
    type: Literal["deadline.approaching"]
    subject_ref: ActionSubject
    data: DeadlineData


class ApprovalRequested(_EventBase):
    """An operation answered approval_required and stored its exact plan."""

    model_config = _emitter("storing a new approval request (approval_required)")
    type: Literal["approval.requested"]
    subject_ref: ApprovalSubject
    data: ApprovalRequestedData


class ApprovalDecided(_EventBase):
    """The owner granted or denied an approval request."""

    model_config = _emitter("the owner's grant or deny of an approval request")
    type: Literal["approval.decided"]
    subject_ref: ApprovalSubject
    data: ApprovalDecidedData


class ResultAwaitingInterpretation(_EventBase):
    """A completed execution, or a receipt without one, waits for an interpretation."""

    model_config = _emitter("awaiting-interpretation detector, once per queue entry")
    type: Literal["result.awaiting_interpretation"]
    subject_ref: ResultSubject
    data: AwaitingData


class CorrectionFlaggedDependents(_EventBase):
    """An applied correction flagged dependent records for review."""

    model_config = _emitter("correction apply, when it flags at least one dependent")
    type: Literal["correction.flagged_dependents"]
    subject_ref: CorrectionSubject
    data: FlaggedDependentsData


class BackupCoverageGap(_EventBase):
    """Retained source versions have no verified backup."""

    model_config = _emitter("backup coverage detector, when the uncovered count grows")
    type: Literal["backup.coverage_gap"]
    subject_ref: ProjectSubject
    data: BackupGapData


class SourceUnavailable(_EventBase):
    """Versions of a source are unavailable (bytes missing or corrupt, or origin gone)."""

    model_config = _emitter("artifact availability detector, once until available again")
    type: Literal["source.unavailable"]
    subject_ref: SourceSubject
    data: SourceUnavailableData


class RunOutcomeUnknown(_EventBase):
    """An operation run's external call has an unknown outcome; it is never repeated."""

    model_config = _emitter(
        "an operation run entering unknown_external (run recovery or the worker's finish)"
    )
    type: Literal["run.outcome_unknown"]
    subject_ref: RunSubject
    data: RunOutcomeUnknownData


class ClaimNeedsReview(_EventBase):
    """A claim's derived status became stale or conflicting (A06.1; decision 0108): do not
    quote it until it is rerun, corrected or decided. Once per entry into either status."""

    model_config = _emitter("claim status detector, once per entry into stale or conflicting")
    type: Literal["claim.needs_review"]
    subject_ref: ClaimSubject
    data: ClaimNeedsReviewData


EVENT_MODELS: tuple[type[_EventBase], ...] = (
    WorkItemCreated,
    WorkItemChanged,
    DeadlineApproaching,
    ApprovalRequested,
    ApprovalDecided,
    ResultAwaitingInterpretation,
    CorrectionFlaggedDependents,
    BackupCoverageGap,
    SourceUnavailable,
    RunOutcomeUnknown,
    ClaimNeedsReview,
)
"""The catalogue in registration order. Append new types; never change or remove one."""

AnyEvent = Annotated[
    WorkItemCreated
    | WorkItemChanged
    | DeadlineApproaching
    | ApprovalRequested
    | ApprovalDecided
    | ResultAwaitingInterpretation
    | CorrectionFlaggedDependents
    | BackupCoverageGap
    | SourceUnavailable
    | RunOutcomeUnknown
    | ClaimNeedsReview,
    Field(discriminator="type"),
]


class Event(RootModel[AnyEvent]):
    """One research event (the ``events`` outbox row, store schema 9)."""

    model_config = ConfigDict(strict=True, frozen=True)


@dataclass(frozen=True)
class EventType:
    name: str
    model: type[_EventBase]
    subject_kinds: tuple[str, ...]
    emitter: str
    in_transaction: bool
    """Written by the operation's own transaction (else by a detector pass)."""


def _catalogue() -> dict[str, EventType]:
    from typing import get_args

    found: dict[str, EventType] = {}
    for model in EVENT_MODELS:
        (name,) = get_args(model.model_fields["type"].annotation)
        subject = model.model_fields["subject_ref"].annotation
        assert subject is not None
        kinds = get_args(subject.model_fields["kind"].annotation)
        extra = model.model_config.get("json_schema_extra")
        assert isinstance(extra, dict)
        emitter = str(extra["x-emitter"])
        found[name] = EventType(name, model, kinds, emitter, "detector" not in emitter)
    return found


CATALOGUE: dict[str, EventType] = _catalogue()
EVENT_TYPES: tuple[str, ...] = tuple(CATALOGUE)
