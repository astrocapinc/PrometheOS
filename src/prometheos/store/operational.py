"""CP10 operational tables (A02 migrations (a), (b), (c), (e); decision 0015).

These rows are operational state, not research records: operation runs, approval
requests and the backup inventory live in the project store (schema 7); maintenance
approvals and quarantine records live in the per-user registry (registry schema 2,
``store.home``). Each table has a closed row model below, exported as a public contract
(``prometheos.schemas``); a test pins every model's fields to its table's columns.

Only minimal accessors live here (insert, get, list, log append). The behaviour that
creates, resumes, grants, expires or recovers belongs to CP10.2-CP10.5. Status writes to
these tables never advance the project sequence; research-visible results commit through
the transaction service.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from prometheos.domain.bodies import reject_credentials
from prometheos.domain.canonical import canonical_sha256, dumps_canonical
from prometheos.domain.ids import (
    ApprovalRequestId,
    MaintenanceApprovalId,
    OperationRunId,
    ProjectId,
    QuarantineId,
    RecordId,
    RequestId,
)
from prometheos.domain.models import (
    MAX_INT,
    CapabilityName,
    ErrorInfo,
    JsonObject,
    PrometheosError,
    Sha256,
    StrictModel,
    Timestamp,
)

OperationName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Actor = Annotated[str, StringConstraints(min_length=1, max_length=100)]
Rank = Annotated[int, Field(ge=0, le=2)]
Count = Annotated[int, Field(ge=0, le=MAX_INT)]
Seq = Annotated[int, Field(ge=0, le=MAX_INT)]
ShortLabel = Annotated[str, StringConstraints(min_length=1, max_length=200)]
LocalPath = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Note = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
CapabilityId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]

# ----- (a) operation runs ---------------------------------------------------------------

RunState = Literal[
    "queued",
    "running",
    "waiting_for_input",
    "paused_budget",
    "completed",
    "failed",
    "cancelled",
    "unknown_external",
]
RUN_STATES: tuple[str, ...] = (
    "queued",
    "running",
    "waiting_for_input",
    "paused_budget",
    "completed",
    "failed",
    "cancelled",
    "unknown_external",
)
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})
ExternalEffect = Literal["none", "started", "confirmed", "uncertain"]


class InputDigest(StrictModel):
    name: ShortLabel
    sha256: Sha256


class BudgetReservation(StrictModel):
    unit: Literal["usd_estimate", "provider_calls", "seconds"]
    amount: Annotated[str, StringConstraints(pattern=r"^\d{1,9}(\.\d{1,6})?$")]


class RunProgress(StrictModel):
    done: Count
    total: Count | None
    unit: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class RunArtifact(StrictModel):
    id: RecordId
    rev: int = Field(ge=1, le=MAX_INT)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class ApprovalWait(StrictModel):
    """The run waits for the owner's decision on an approval request (CP10.2)."""

    kind: Literal["approval"]
    approval_request_id: ApprovalRequestId


class InputWait(StrictModel):
    """The run waits for input from the caller (a file, a choice, text or a confirmation)."""

    kind: Literal["input"]
    input_kind: Literal["file", "choice", "text", "confirmation"]
    description: Note


WaitingOn = Annotated[ApprovalWait | InputWait, Field(discriminator="kind")]


class ExternalCall(StrictModel):
    """The external request a run started (a provider call, a remote push, a download):
    enough to show what is uncertain and to never send it again blindly. Never a credential:
    ``external_ref`` is the provider's request or job label."""

    provider: ShortLabel
    external_ref: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None
    request_sha256: Sha256 | None
    started_at: Timestamp

    @field_validator("external_ref")
    @classmethod
    def _no_credentials(cls, value: str | None) -> str | None:
        return reject_credentials(value, "external_ref")


class OperationRun(StrictModel):
    """One row of ``operation_runs``, the single run of record for app-owned long operations.

    ``params`` are the run's re-executable parameters (``params_digest`` is their canonical
    digest) and ``input_digest`` the digest over ``input_digests``. Resume is allowed only
    when ``params_digest``, ``input_digest``, ``config_digest``, ``model`` and
    ``workflow_version`` are unchanged (spec/04 "Operation runs"). ``waiting_on`` says what a
    ``waiting_for_input`` run awaits; ``external_call`` describes a started external request
    (uncertain when ``external_effect`` is ``uncertain``)."""

    id: OperationRunId
    operation: OperationName
    state: RunState
    caller_rank: Rank
    actor: Actor
    start_request_id: RequestId | None
    start_request_sha256: Sha256 | None
    params: JsonObject
    params_digest: Sha256
    input_digests: list[InputDigest] = Field(max_length=200)
    input_digest: Sha256
    config_digest: Sha256
    workflow_version: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None
    model: ShortLabel | None
    budget_reservation: BudgetReservation | None
    external_effect: ExternalEffect
    external_call: ExternalCall | None
    waiting_on: WaitingOn | None
    stage: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None
    progress: RunProgress | None
    cancel_requested_at: Timestamp | None
    lease_owner: ShortLabel | None
    lease_expires_at: Timestamp | None
    heartbeat_at: Timestamp | None
    attempts: Count
    error: ErrorInfo | None
    limitations: list[Note] = Field(max_length=50)
    artifacts: list[RunArtifact] = Field(max_length=200)
    result_seq: Seq | None
    job_id: RecordId | None
    job_due_utc: Timestamp | None
    created_at: Timestamp
    updated_at: Timestamp
    finished_at: Timestamp | None

    @model_validator(mode="after")
    def _consistent(self) -> OperationRun:
        if self.params_digest != canonical_sha256(self.params):
            raise ValueError("params_digest must be the canonical digest of params")
        if self.input_digest != canonical_sha256(
            [d.model_dump(mode="json") for d in self.input_digests]
        ):
            raise ValueError("input_digest must be the digest of input_digests")
        if (self.state == "waiting_for_input") != (self.waiting_on is not None):
            raise ValueError("waiting_on is set exactly for a waiting_for_input run")
        if (self.state in TERMINAL_RUN_STATES) != (self.finished_at is not None):
            raise ValueError("finished_at is set exactly for completed, failed and cancelled runs")
        if self.state == "failed" and self.error is None:
            raise ValueError("a failed run records its error")
        if self.state == "unknown_external" and self.external_effect != "uncertain":
            raise ValueError("unknown_external records an uncertain external effect")
        if (self.external_effect != "none") != (self.external_call is not None):
            raise ValueError("external_call is set exactly when an external effect began")
        if (self.start_request_id is None) != (self.start_request_sha256 is None):
            raise ValueError("a start request id comes with its rank-bound key")
        if (self.job_id is None) != (self.job_due_utc is None):
            raise ValueError("a job slot is named by job_id and job_due_utc together")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("a lease has an owner and an expiry")
        return self


class OperationRunLog(StrictModel):
    run_id: OperationRunId
    idx: Count
    at: Timestamp
    level: Literal["info", "warning", "error"]
    message: Note


MAX_RUN_LOG_ENTRIES = 500

StageName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_:.-]{0,99}$")]
Amount = Annotated[str, StringConstraints(pattern=r"^\d{1,9}(\.\d{1,6})?$")]


class OperationRunStage(StrictModel):
    """One completed stage of a run (store schema 8, decision 0017): the checkpoint a resume
    continues from. ``output`` is what later stages need (bounded); ``charged`` is what the
    stage cost in the run's budget unit, so a resumed run never charges it again. The stage
    named ``result`` holds the operation's result document."""

    run_id: OperationRunId
    idx: Count
    name: StageName
    output: JsonObject
    charged: Amount | None
    completed_at: Timestamp


class RunLogLine(StrictModel):
    idx: Count
    at: Timestamp
    level: Literal["info", "warning", "error"]
    message: Note


class RunStageView(StrictModel):
    name: StageName
    completed_at: Timestamp
    charged: Amount | None


class RunBudget(StrictModel):
    reservation: BudgetReservation | None
    charged: Amount


class RunView(StrictModel):
    """What every transport (hub, CLI, MCP, HTTP) shows of one operation run: the same
    document everywhere (decision 0017). ``cancel_confirmed`` is true only once the run is
    ``cancelled``: a recorded ``cancel_requested_at`` is a request, not proof of a stop.
    ``result`` is the operation's result document once it completed. The latest 50 log lines
    and stages are included; the lease owner (host and process) is not."""

    run_id: OperationRunId
    operation: OperationName
    state: RunState
    stage: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None
    progress: RunProgress | None
    actor: Actor
    caller_rank: Rank
    params: JsonObject
    params_digest: Sha256
    input_digests: list[InputDigest] = Field(max_length=200)
    input_digest: Sha256
    config_digest: Sha256
    workflow_version: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None
    model: ShortLabel | None
    budget: RunBudget
    external_effect: ExternalEffect
    external_call: ExternalCall | None
    waiting_on: WaitingOn | None
    cancel_requested_at: Timestamp | None
    cancel_confirmed: bool
    heartbeat_at: Timestamp | None
    lease_expires_at: Timestamp | None
    attempts: Count
    error: ErrorInfo | None
    limitations: list[Note] = Field(max_length=50)
    artifacts: list[RunArtifact] = Field(max_length=200)
    result_seq: Seq | None
    result: JsonObject | None
    resumable: bool
    job_id: RecordId | None
    job_due_utc: Timestamp | None
    stages_completed: Count
    stages: list[RunStageView] = Field(max_length=50)
    logs: list[RunLogLine] = Field(max_length=50)
    created_at: Timestamp
    updated_at: Timestamp
    finished_at: Timestamp | None

    @model_validator(mode="after")
    def _consistent(self) -> RunView:
        if self.cancel_confirmed != (self.state == "cancelled"):
            raise ValueError("a cancellation is confirmed exactly when the run is cancelled")
        if (self.state in TERMINAL_RUN_STATES) != (self.finished_at is not None):
            raise ValueError("finished_at is set exactly for completed, failed and cancelled runs")
        if self.result is not None and self.state != "completed":
            raise ValueError("a result belongs to a completed run")
        if self.state == "unknown_external" and self.external_effect != "uncertain":
            raise ValueError("unknown_external records an uncertain external effect")
        if self.resumable and self.state not in ("queued", "failed", "paused_budget"):
            raise ValueError("only queued, failed and paused_budget runs are resumable")
        return self


# ----- (b) approval requests ------------------------------------------------------------

ApprovalState = Literal["pending", "granted", "denied", "withdrawn", "expired", "revoked", "used"]
APPROVAL_STATES: tuple[str, ...] = (
    "pending",
    "granted",
    "denied",
    "withdrawn",
    "expired",
    "revoked",
    "used",
)
OPEN_APPROVAL_STATES = frozenset({"pending", "granted"})


def check_approval_lifecycle(
    state: str,
    decided_at: str | None,
    decided_by: str | None,
    grant: object | None,
    used_at: str | None,
    closed_at: str | None,
) -> None:
    """The approval lifecycle (decision 0015 round 2, R2), shared by project approval
    requests (``grant`` = capability_id) and maintenance approvals (``grant`` =
    grant_expires_at).

    pending -> granted | denied | withdrawn | expired; granted -> used | withdrawn |
    expired | revoked. A decision, once made, is never erased: a granted request that later
    ends keeps who decided, when, and which grant was issued; the state records why it
    ended and ``closed_at`` when (the grant is then unusable). ``closed_at`` is set exactly
    for ended requests."""
    if (decided_at is None) != (decided_by is None):
        raise ValueError("a decision records who decided")
    if (state in OPEN_APPROVAL_STATES) != (closed_at is None):
        raise ValueError("closed_at is set exactly when the request has ended")
    if (state == "used") != (used_at is not None):
        raise ValueError("used_at is set exactly for a used request")
    if state == "pending" and (decided_at is not None or grant is not None):
        raise ValueError("a pending request has no decision and no grant")
    if state == "denied" and (decided_at is None or grant is not None):
        raise ValueError("a denied request records the decision and no grant")
    if state in ("granted", "used", "revoked") and (decided_at is None or grant is None):
        raise ValueError("a granted, used or revoked request keeps its decision and grant")
    if state in ("withdrawn", "expired") and (decided_at is None) != (grant is None):
        raise ValueError("a request that ended after a grant keeps its decision and grant")


class ApprovalRequest(StrictModel):
    """One row of ``approval_requests``: an agent's exact protected plan awaiting the owner.

    The grant is an exact-plan capability held by the server (``capability_id`` names a row
    of ``capabilities``; its secret is never returned to the agent) and used once, in the
    transaction that executes the stored plan (spec/04 "Pending approvals")."""

    id: ApprovalRequestId
    project_id: ProjectId
    operation: OperationName
    caller_rank: Rank
    actor: Actor
    request_id: RequestId | None
    plan: JsonObject
    plan_sha256: Sha256
    required_scope: CapabilityName
    owner_route_hub: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9/_.:-]{0,299}$")]
    owner_route_cli: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    summary: list[Annotated[str, StringConstraints(min_length=1, max_length=300)]] = Field(
        min_length=1, max_length=50
    )
    state: ApprovalState
    created_at: Timestamp
    expires_at: Timestamp
    decided_at: Timestamp | None
    decided_by: Actor | None
    decision_note: Note | None
    capability_id: CapabilityId | None
    used_at: Timestamp | None
    used_seq: Seq | None
    result: JsonObject | None
    closed_at: Timestamp | None

    @model_validator(mode="after")
    def _consistent(self) -> ApprovalRequest:
        if self.plan_sha256 != canonical_sha256(self.plan):
            raise ValueError("plan_sha256 must be the canonical digest of plan")
        check_approval_lifecycle(
            self.state,
            self.decided_at,
            self.decided_by,
            self.capability_id,
            self.used_at,
            self.closed_at,
        )
        if self.used_seq is not None and self.state != "used":
            raise ValueError("used_seq belongs to a used request")
        return self


# ----- (c) backup inventory (metadata only) ---------------------------------------------


class BackupInventoryEntry(StrictModel):
    """One verified backup known to this store. Metadata only: never backup bytes.

    ``location`` is the owner's local path, used to verify or restore; readers outside the
    owner scope get ``label`` only. Covered media digests are in ``backup_inventory_media``;
    the database (and so every retained payload) is covered up to ``project_seq``;
    reference-only bytes are never covered."""

    id: Annotated[int, Field(ge=1, le=MAX_INT)]
    backup_sha256: Sha256
    manifest_sha256: Sha256
    location: LocalPath
    label: ShortLabel
    purpose: Literal["manual", "pre_migration", "pre_restore"]
    project_seq: Seq
    schema_version: Annotated[int, Field(ge=1, le=10000)]
    software_version: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    created_at: Timestamp
    database_sha256: Sha256
    media_count: Count
    media_bytes: Count
    reference_only_count: Count
    verification: Literal["verified", "failed", "unverified"]
    verified_at: Timestamp | None
    last_checked_at: Timestamp | None
    present: bool | None
    recorded_at: Timestamp

    @model_validator(mode="after")
    def _consistent(self) -> BackupInventoryEntry:
        if (self.verification == "unverified") != (self.verified_at is None):
            raise ValueError("verified_at is set exactly when a verification ran")
        if (self.present is None) != (self.last_checked_at is None):
            raise ValueError("presence is known exactly when the archive was checked")
        return self


# ----- (e) per-user registry: maintenance approvals and quarantine records ---------------

# An unreadable database cannot provide the exact digest a grant requires (decision 0021).
DamagedDigest = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{64}|missing)$")]


MAINTENANCE_ACTIONS: tuple[str, ...] = ("registry_switch", "migrate")
"""What a maintenance approval asks for (``plan["action"]``; decision 0021): a registry
switch to a verified restored store, or (decision 0016) a schema migration of a store that
cannot hold a project approval request because it does not open at the current schema."""


class MaintenanceApproval(StrictModel):
    """One row of the registry's ``maintenance_approvals``: a registry switch an agent
    requested while no project database was open (spec/04 "Maintenance service"). The grant
    is held in the registry (state ``granted`` until ``grant_expires_at``) and used once in
    the registry transaction that performs the switch.

    Decision 0021: a ``migrate`` request (``plan["action"]``) binds the store to migrate
    (``damaged_store_path``, the store whose database cannot be opened at the current schema)
    and its database digest; it names no backup manifest and no target store (the migration
    writes its own verified pre-migration backup and migrates in place)."""

    id: MaintenanceApprovalId
    project_id: ProjectId
    damaged_store_path: LocalPath
    damaged_db_sha256: DamagedDigest
    backup_manifest_sha256: Sha256 | None
    target_store_path: LocalPath | None
    caller_rank: Rank
    actor: Actor
    request_id: RequestId | None
    plan: JsonObject
    plan_sha256: Sha256
    state: ApprovalState
    created_at: Timestamp
    expires_at: Timestamp
    decided_at: Timestamp | None
    decided_by: Actor | None
    grant_expires_at: Timestamp | None
    used_at: Timestamp | None
    closed_at: Timestamp | None

    @model_validator(mode="after")
    def _consistent(self) -> MaintenanceApproval:
        if self.plan_sha256 != canonical_sha256(self.plan):
            raise ValueError("plan_sha256 must be the canonical digest of plan")
        action = self.plan.get("action")
        if action not in MAINTENANCE_ACTIONS:
            raise ValueError("the plan's action is registry_switch or migrate")
        switch = action == "registry_switch"
        if switch != (self.backup_manifest_sha256 is not None) or switch != (
            self.target_store_path is not None
        ):
            raise ValueError("a registry switch names a backup and a target; a migration neither")
        if switch and self.target_store_path == self.damaged_store_path:
            raise ValueError("the target is a new store, never the damaged one")
        bound = {
            "project_id": self.project_id,
            "damaged_store_path": self.damaged_store_path,
            "damaged_db_sha256": self.damaged_db_sha256,
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "target_store_path": self.target_store_path,
        }
        if any(self.plan.get(key) != value for key, value in bound.items()):
            raise ValueError("the plan binds the project, damaged store, backup and target")
        check_approval_lifecycle(
            self.state,
            self.decided_at,
            self.decided_by,
            self.grant_expires_at,
            self.used_at,
            self.closed_at,
        )
        return self


class StoreQuarantine(StrictModel):
    """One row of the registry's ``store_quarantine``: a damaged store kept for inspection
    (never overwritten) and the verified replacement the registry switched to."""

    id: QuarantineId
    project_id: ProjectId
    damaged_store_path: LocalPath
    damaged_db_sha256: DamagedDigest
    damaged_schema_version: Annotated[int, Field(ge=0, le=10000)] | None
    damaged_integrity: Literal["ok", "corrupt", "unreadable", "missing"]
    replacement_store_path: LocalPath
    backup_sha256: Sha256
    backup_manifest_sha256: Sha256
    maintenance_approval_id: MaintenanceApprovalId | None
    switched_at: Timestamp
    switched_by: Actor
    reason: Note

    @model_validator(mode="after")
    def _consistent(self) -> StoreQuarantine:
        if (self.damaged_db_sha256 == "missing") != (self.damaged_integrity == "missing"):
            raise ValueError("a missing database has no digest, and only a missing one")
        if self.damaged_store_path == self.replacement_store_path:
            raise ValueError("the replacement is a new store, never the damaged one")
        return self


StoreLabel = Annotated[str, StringConstraints(min_length=1, max_length=600)]


class MaintenanceApprovalView(StrictModel):
    """A maintenance approval as its requester or the owner is shown (CP10.5; decision 0021).

    The row plus what a reader needs without local paths: every store location also has a
    label (``<PrometheOS home>/...`` or the folder name under ``<local path>``), which is what
    the hub shows (it removes the absolute paths), the owner routes, and summary lines. No
    grant secret exists: the grant is the row's ``granted`` state, held in the registry."""

    approval_request_id: MaintenanceApprovalId
    operation: Literal["maintenance_switch", "maintenance_migrate"]
    state: Literal["pending", "granted", "denied", "withdrawn", "expired", "revoked", "used"]
    project_id: ProjectId
    caller_rank: Rank
    actor: Actor
    request_id: RequestId | None
    plan: JsonObject
    plan_sha256: Sha256
    summary: list[Annotated[str, StringConstraints(min_length=1, max_length=600)]] = Field(
        min_length=1, max_length=20
    )
    owner_route: dict[Literal["hub", "cli"], Annotated[str, StringConstraints(max_length=300)]]
    damaged_store_path: LocalPath
    damaged_store_label: StoreLabel
    damaged_db_sha256: DamagedDigest
    backup_manifest_sha256: Sha256 | None
    target_store_path: LocalPath | None
    target_store_label: StoreLabel | None
    created_at: Timestamp
    expires_at: Timestamp
    decided_at: Timestamp | None
    decided_by: Actor | None
    grant_expires_at: Timestamp | None
    used_at: Timestamp | None
    closed_at: Timestamp | None

    @model_validator(mode="after")
    def _consistent(self) -> MaintenanceApprovalView:
        if (self.target_store_path is None) != (self.target_store_label is None):
            raise ValueError("a target store has a label, and only a target store does")
        switch = self.operation == "maintenance_switch"
        if switch != (self.target_store_path is not None):
            raise ValueError("a registry switch names its target store; a migration does not")
        check_approval_lifecycle(
            self.state,
            self.decided_at,
            self.decided_by,
            self.grant_expires_at,
            self.used_at,
            self.closed_at,
        )
        return self


# ----- row encoding ---------------------------------------------------------------------

_JSON_COLUMNS: dict[type[StrictModel], frozenset[str]] = {
    OperationRun: frozenset(
        {
            "params",
            "input_digests",
            "budget_reservation",
            "external_call",
            "waiting_on",
            "progress",
            "error",
            "limitations",
            "artifacts",
        }
    ),
    ApprovalRequest: frozenset({"plan", "summary", "result"}),
    MaintenanceApproval: frozenset({"plan"}),
    OperationRunStage: frozenset({"output"}),
}
_BOOL_COLUMNS: dict[type[StrictModel], frozenset[str]] = {
    BackupInventoryEntry: frozenset({"present"}),
}


def to_row(model: StrictModel) -> dict[str, Any]:
    """Column values for ``model`` (JSON columns canonical, booleans as 0/1)."""
    json_cols = _JSON_COLUMNS.get(type(model), frozenset())
    bool_cols = _BOOL_COLUMNS.get(type(model), frozenset())
    row: dict[str, Any] = {}
    for name, value in model.model_dump(mode="json").items():
        if name in json_cols and value is not None:
            row[name] = dumps_canonical(value).decode("utf-8")
        elif name in bool_cols and value is not None:
            row[name] = int(value)
        else:
            row[name] = value
    return row


def from_row[M: StrictModel](model: type[M], row: sqlite3.Row | dict[str, Any]) -> M:
    json_cols = _JSON_COLUMNS.get(model, frozenset())
    bool_cols = _BOOL_COLUMNS.get(model, frozenset())
    values: dict[str, Any] = {}
    for name in model.model_fields:
        value = row[name]
        if name in json_cols and value is not None:
            value = json.loads(value)
        elif name in bool_cols and value is not None:
            value = bool(value)
        values[name] = value
    return model.model_validate(values, strict=True)


def insert(conn: sqlite3.Connection, table: str, model: StrictModel) -> None:
    row = to_row(model)
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})",  # noqa: S608 - fixed table names
        tuple(row.values()),
    )


TABLES: dict[str, type[StrictModel]] = {
    "operation_runs": OperationRun,
    "operation_run_logs": OperationRunLog,
    "operation_run_stages": OperationRunStage,
    "approval_requests": ApprovalRequest,
    "backup_inventory": BackupInventoryEntry,
}
"""Project-store tables (schema 7; stages since schema 8) and their row contracts."""

REGISTRY_TABLES: dict[str, type[StrictModel]] = {
    "maintenance_approvals": MaintenanceApproval,
    "store_quarantine": StoreQuarantine,
}
"""Per-user registry tables (registry schema 2) and their row contracts."""


def _get[M: StrictModel](
    conn: sqlite3.Connection, table: str, model: type[M], row_id: str | int
) -> M | None:
    row = conn.execute(
        f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608 - fixed table names
        (row_id,),
    ).fetchone()
    return None if row is None else from_row(model, row)


# ----- minimal project-store accessors --------------------------------------------------


def insert_operation_run(conn: sqlite3.Connection, run: OperationRun) -> None:
    insert(conn, "operation_runs", run)


def get_operation_run(conn: sqlite3.Connection, run_id: str) -> OperationRun | None:
    return _get(conn, "operation_runs", OperationRun, run_id)


def list_operation_runs(
    conn: sqlite3.Connection, states: tuple[str, ...] | None = None, limit: int = 100
) -> list[OperationRun]:
    if states is not None and not set(states) <= set(RUN_STATES):
        raise PrometheosError("validation_error", "unknown run state", {"states": list(states)})
    if states:
        marks = ",".join("?" for _ in states)
        rows = conn.execute(
            f"SELECT * FROM operation_runs WHERE state IN ({marks}) "  # noqa: S608
            "ORDER BY created_at, id LIMIT ?",
            (*states, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM operation_runs ORDER BY created_at, id LIMIT ?", (limit,)
        ).fetchall()
    return [from_row(OperationRun, r) for r in rows]


def append_run_log(
    conn: sqlite3.Connection, run_id: str, at: str, level: str, message: str
) -> OperationRunLog:
    """Append one bounded log line; the oldest lines beyond the bound are dropped."""
    row = conn.execute(
        "SELECT COALESCE(MAX(idx), -1) FROM operation_run_logs WHERE run_id = ?", (run_id,)
    ).fetchone()
    entry = OperationRunLog(
        run_id=run_id,
        idx=int(row[0]) + 1,
        at=at,
        level=level,  # type: ignore[arg-type]
        message=message[:2000],
    )
    insert(conn, "operation_run_logs", entry)
    conn.execute(
        "DELETE FROM operation_run_logs WHERE run_id = ? AND idx <= ?",
        (run_id, entry.idx - MAX_RUN_LOG_ENTRIES),
    )
    return entry


def run_logs(conn: sqlite3.Connection, run_id: str) -> list[OperationRunLog]:
    rows = conn.execute(
        "SELECT * FROM operation_run_logs WHERE run_id = ? ORDER BY idx", (run_id,)
    ).fetchall()
    return [from_row(OperationRunLog, r) for r in rows]


def _same_request(existing: Any, new: Any) -> bool:
    return bool(
        existing.caller_rank == new.caller_rank
        and existing.plan_sha256 == new.plan_sha256
        and getattr(existing, "operation", None) == getattr(new, "operation", None)
    )


def insert_approval_request(conn: sqlite3.Connection, request: ApprovalRequest) -> ApprovalRequest:
    """Insert a request, or return the stored one for an identical retry.

    ``request_id`` is one namespace across ranks (decision 0010): the same ID resent at
    another rank, or for another plan or operation, is ``request_key_conflict`` and reveals
    nothing about the stored request."""
    if request.request_id is not None:
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE request_id = ?", (request.request_id,)
        ).fetchone()
        if row is not None:
            existing = from_row(ApprovalRequest, row)
            if not _same_request(existing, request):
                raise PrometheosError(
                    "request_key_conflict",
                    "request_id was already used for a different request",
                    {"request_id": request.request_id},
                )
            return existing
    insert(conn, "approval_requests", request)
    return request


def get_approval_request(conn: sqlite3.Connection, request_id: str) -> ApprovalRequest | None:
    return _get(conn, "approval_requests", ApprovalRequest, request_id)


def list_approval_requests(
    conn: sqlite3.Connection, state: str | None = None, limit: int = 100
) -> list[ApprovalRequest]:
    if state is not None and state not in APPROVAL_STATES:
        raise PrometheosError("validation_error", "unknown approval state", {"state": state})
    if state is None:
        rows = conn.execute(
            "SELECT * FROM approval_requests ORDER BY created_at, id LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM approval_requests WHERE state = ? ORDER BY created_at, id LIMIT ?",
            (state, limit),
        ).fetchall()
    return [from_row(ApprovalRequest, r) for r in rows]


def record_backup(
    conn: sqlite3.Connection,
    entry: BackupInventoryEntry,
    media_sha256: list[str],
) -> int:
    """Insert one inventory row and its covered media digests; return the row id.

    ``entry.id`` is ignored (the database assigns it). An archive already listed (same
    ``backup_sha256``) is not duplicated: its existing id is returned."""
    existing = conn.execute(
        "SELECT id FROM backup_inventory WHERE backup_sha256 = ?", (entry.backup_sha256,)
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    row = to_row(entry)
    row.pop("id")
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(
        f"INSERT INTO backup_inventory ({columns}) VALUES ({marks})",  # noqa: S608
        tuple(row.values()),
    )
    backup_id = int(cur.lastrowid or 0)
    conn.executemany(
        "INSERT OR IGNORE INTO backup_inventory_media (backup_id, sha256) VALUES (?, ?)",
        [(backup_id, sha) for sha in media_sha256],
    )
    return backup_id


def list_backups(conn: sqlite3.Connection) -> list[BackupInventoryEntry]:
    rows = conn.execute("SELECT * FROM backup_inventory ORDER BY project_seq, id").fetchall()
    return [from_row(BackupInventoryEntry, r) for r in rows]


def backups_covering_media(conn: sqlite3.Connection, sha256: str) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT backup_id FROM backup_inventory_media WHERE sha256 = ? ORDER BY backup_id",
            (sha256,),
        )
    ]


def entry_from_manifest(
    manifest: dict[str, Any],
    *,
    backup_sha256: str,
    location: str,
    label: str,
    purpose: str,
    verified_at: str | None,
    recorded_at: str,
) -> tuple[BackupInventoryEntry, list[str]]:
    """The inventory row and covered media digests for a verified backup manifest."""
    media = list(manifest["media"])
    entry = BackupInventoryEntry(
        id=1,  # placeholder; record_backup lets the database assign it
        backup_sha256=backup_sha256,
        manifest_sha256=canonical_sha256(manifest),
        location=location,
        label=label,
        purpose=purpose,  # type: ignore[arg-type]
        project_seq=manifest["project_seq"],
        schema_version=manifest["schema_version"],
        software_version=manifest["software_version"],
        created_at=manifest["created_at"],
        database_sha256=manifest["database"]["sha256"],
        media_count=len(media),
        media_bytes=sum(int(m["bytes"]) for m in media),
        reference_only_count=len(manifest["reference_only_not_included"]),
        verification="verified" if verified_at else "unverified",
        verified_at=verified_at,
        last_checked_at=None,
        present=None,
        recorded_at=recorded_at,
    )
    return entry, [str(m["sha256"]) for m in media]
