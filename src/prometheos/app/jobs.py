"""Fixed recurring jobs: due slots, leases, bounded handlers and the inbox (spec/08).

A job record (``kind=job``) names one of three fixed local handlers and a schedule.
``tick`` is one bounded pass: it takes the project's single scheduler lease, records
due slots in UTC (coalescing a backlog into one current run), recovers expired
leases, and runs at most ``MAX_SLOTS_PER_TICK`` slots one at a time. Slot and lease
bookkeeping never advances the project sequence; a *finding* -- a notification,
saved source observations and the job's ``last_slot`` -- is one ordinary commit with
a request ID derived from the slot, so a retried slot cannot record it twice.

Handlers that would transmit anything must call ``before_external`` first; a slot
whose lease expires after that mark is ``unknown`` and is never repeated
automatically. The v1 handlers are local and never transmit.

The durable scheduler and slot leases are the scheduler's authority, never a live store
connection (decision 0027): every admitted connection here is short, and a handler runs with
none open, so a cancellation, a status read or a nested service call is admitted meanwhile.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from prometheos.app import papers, reads
from prometheos.app.reads import Caller, current_record, project_seq
from prometheos.domain.bodies import JobBody, ProjectSettings
from prometheos.domain.canonical import canonical_sha256, dumps_canonical, loads_internal
from prometheos.domain.ids import KINDS, UUID4_PATTERN, derived_request_id, new_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError, utc_now
from prometheos.faults import fault_point
from prometheos.store import capabilities, db
from prometheos.store.project import ProjectStore, Txn

LEASE_SECONDS = 120
MAX_SLOTS_PER_TICK = 10
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (30, 60, 120)
CHECKPOINT_SECONDS = 10.0
"""Longest a built-in handler may work between ``ctx.checkpoint()`` renewals and cancellation
checks; well under ``LEASE_SECONDS`` so a slow handler keeps its leases."""
RESOLUTIONS = {"succeeded": "succeeded", "failed": "failed", "abandoned": "skipped"}
"""Owner resolutions of an ``unknown`` slot and the slot state each records; the slot-state
set is fixed by the spec, so ``abandoned`` is a ``skipped`` slot whose reason says so."""
MAX_SKIPPED_ROWS = 96
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SIDE_EFFECTS = {
    "source_refresh": "local: reads registered files under approved roots",
    "paper_check": "local: pure readiness analysis of the record",
    "digest": "local: summarises recorded changes",
}
OWNER = Caller(actor="scheduler", scope="owner")
USAGE = {"provider_calls": 0, "estimated_cost": None}


# ----- time --------------------------------------------------------------------------------


def fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_precise(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _weekly_occurrence(day: date, time_text: str, zone: ZoneInfo) -> datetime | None:
    """UTC instant of a local wall time; ``None`` when it does not exist (spring forward).

    A repeated local time (fall back) resolves to its first occurrence (``fold=0``).
    """
    hour, minute = (int(p) for p in time_text.split(":"))
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
    instant = local.astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        return None
    return instant


def due_between(
    schedule: dict[str, Any], after: datetime, until: datetime, keep: int = MAX_SKIPPED_ROWS + 1
) -> tuple[list[datetime], int]:
    """Due instants in ``(after, until]``: the last ``keep`` of them and the total count."""
    if until <= after:
        return [], 0
    if schedule["type"] == "interval":
        start = parse(schedule["start_utc"])
        step = timedelta(minutes=schedule["minutes"])
        first = 0 if after < start else (after - start) // step + 1
        last = (until - start) // step
        if until < start or last < first:
            return [], 0
        total = last - first + 1
        begin = max(first, last - keep + 1)
        return [start + n * step for n in range(begin, last + 1)], total
    zone = ZoneInfo(schedule["zone"])
    weekday = DAYS.index(schedule["day"])
    day = after.astimezone(zone).date() - timedelta(days=1)
    end = until.astimezone(zone).date() + timedelta(days=1)
    found: list[datetime] = []
    total = 0
    while day <= end:
        if day.weekday() == weekday:
            instant = _weekly_occurrence(day, schedule["time"], zone)
            if instant is not None and after < instant <= until:
                total += 1
                found.append(instant)
                if len(found) > keep:
                    found.pop(0)
            day += timedelta(days=7)
        else:
            day += timedelta(days=(weekday - day.weekday()) % 7)
    return found, total


def upcoming(schedule: dict[str, Any], after: datetime, count: int = 5) -> list[dict[str, Any]]:
    """Next due slots for a plan preview, with skipped nonexistent weekly times named."""
    out: list[dict[str, Any]] = []
    if schedule["type"] == "interval":
        start = parse(schedule["start_utc"])
        step = timedelta(minutes=schedule["minutes"])
        first = 0 if after < start else (after - start) // step + 1
        return [{"due_utc": fmt(start + n * step)} for n in range(first, first + count)]
    zone = ZoneInfo(schedule["zone"])
    weekday = DAYS.index(schedule["day"])
    day = after.astimezone(zone).date()
    day += timedelta(days=(weekday - day.weekday()) % 7)
    for _ in range(count * 3):
        instant = _weekly_occurrence(day, schedule["time"], zone)
        if instant is None:
            out.append(
                {
                    "local_date": day.isoformat(),
                    "skipped": "this local time does not exist (daylight saving change)",
                }
            )
        elif instant > after:
            local = instant.astimezone(zone)
            entry: dict[str, Any] = {"due_utc": fmt(instant), "local": local.isoformat()}
            later = local.replace(fold=1)
            if later.astimezone(UTC) != instant:
                entry["note"] = "local time repeats this day; runs at the first occurrence only"
            out.append(entry)
        if sum(1 for o in out if "due_utc" in o) >= count:
            break
        day += timedelta(days=7)
    return out


# ----- job records ---------------------------------------------------------------------------


def validate_job(body: Any) -> JobBody:
    try:
        return JobBody.model_validate_json(json.dumps(body), strict=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise PrometheosError(
            "validation_error", "job does not match the job schema", {"errors": str(exc)[:1500]}
        ) from exc


def _job_rank(conn: sqlite3.Connection, caller: Caller, job: JobBody) -> int:
    """Referenced records must exist and be visible; return their highest sensitivity."""
    params = job.parameters.model_dump(mode="json")
    ids = params.get("source_ids", []) + ([params["paper_id"]] if "paper_id" in params else [])
    rank = 0
    for record_id in ids:
        record = current_record(conn, record_id)
        if record is None or not caller.may_see(record["sensitivity"]):
            raise PrometheosError("not_found", "job input not found", {"id": record_id})
        if record["lifecycle"] != "active":
            raise PrometheosError(
                "validation_error", "job inputs must be active", {"id": record_id}
            )
        rank = max(rank, SENSITIVITY_ORDER[record["sensitivity"]])
    return rank


def plan(
    store: ProjectStore, caller: Caller, body: Any, now: datetime | None = None
) -> dict[str, Any]:
    """Pure preview of a job: validated body, next due slots, side effects and budget."""
    job = validate_job(body)
    if job.last_slot is not None or not job.enabled:
        raise PrometheosError("validation_error", "new jobs start enabled with last_slot null", {})
    conn = store.connect(readonly=True)
    try:
        with db.read_snapshot(conn):
            rank = _job_rank(conn, caller, job)
    finally:
        conn.close()
    moment = now or datetime.now(UTC)
    schedule = job.schedule.model_dump(mode="json")
    return {
        "job": job.model_dump(mode="json"),
        "sensitivity": ("normal", "restricted", "private")[rank],
        "next_slots": upcoming(schedule, moment),
        "side_effects": SIDE_EFFECTS[job.handler],
        "transmits_data": False,
        "runs_only_when": "prometheos jobs tick runs (manually, a scheduled task you install, or "
        "a process you start); nothing runs while the computer is off",
        "coalescing": "missed slots are recorded as skipped and one current run is made",
        "dst": "weekly times that do not exist are skipped; repeated times run once (first)",
        "digest": canonical_sha256(job.model_dump(mode="json")),
    }


def add(
    store: ProjectStore, caller: Caller, body: Any, request_id: str, actor: str
) -> dict[str, Any]:
    job = validate_job(body)
    if job.last_slot is not None or not job.enabled:
        raise PrometheosError("validation_error", "new jobs start enabled with last_slot null", {})
    clean = job.model_dump(mode="json")
    job_id = new_id("job")
    max_rank = 2 if caller.scope == "owner" else 0

    def handler(txn: Txn) -> None:
        rank = _job_rank(txn.conn, caller, job)
        body_id = txn.insert_body(clean)
        sensitivity = ("normal", "restricted", "private")[rank]
        txn.write_revision(
            job_id, "job", 1, body_id, sensitivity, "active", None, "job_add", body=clean
        )
        txn.result(job_id, 1)

    result = store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256({"op": "job_add", "job": clean}),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=max_rank,
    )
    return {"job_id": result["results"][0]["id"], "commit": result}


def set_enabled(
    store: ProjectStore,
    caller: Caller,
    job_id: str,
    base_rev: int,
    enabled: bool,
    request_id: str,
    actor: str,
) -> dict[str, Any]:
    def handler(txn: Txn) -> None:
        state = txn.get(job_id)
        if state is None or state.kind != "job":
            raise PrometheosError("not_found", "job not found", {"id": job_id})
        if state.rev != base_rev:
            raise PrometheosError(
                "revision_conflict", "job changed; read it again", {"id": job_id, "rev": state.rev}
            )
        if state.body["enabled"] == enabled:
            raise PrometheosError(
                "validation_error", f"job is already {'enabled' if enabled else 'disabled'}", {}
            )
        body = validate_job(dict(state.body, enabled=enabled)).model_dump(mode="json")
        body_id = txn.insert_body(body)
        txn.write_revision(
            job_id,
            "job",
            state.rev + 1,
            body_id,
            state.sensitivity,
            state.lifecycle,
            None,
            "job_enable" if enabled else "job_disable",
            body=body,
        )
        if not enabled:
            txn.conn.execute(
                "UPDATE job_slots SET state = 'skipped', reason = 'job disabled', updated_at = ? "
                "WHERE job_id = ? AND state = 'pending'",
                (txn.recorded_at, job_id),
            )
        txn.result(job_id, state.rev + 1)

    return store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256(
            {"op": "job_enable", "job": job_id, "base_rev": base_rev, "enabled": enabled}
        ),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=2 if caller.scope == "owner" else 0,
    )


def cancel(store: ProjectStore, caller: Caller, job_id: str, due_utc: str) -> dict[str, Any]:
    """Cancel one slot: pending becomes skipped; running gets a cancellation request."""
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            job = current_record(conn, job_id)
            if job is None or job["kind"] != "job" or not caller.may_see(job["sensitivity"]):
                raise PrometheosError("not_found", "job not found", {"id": job_id})
            row = conn.execute(
                "SELECT state FROM job_slots WHERE job_id = ? AND due_utc = ?", (job_id, due_utc)
            ).fetchone()
            if row is None:
                raise PrometheosError("not_found", "slot not found", {"due_utc": due_utc})
            now = utc_now()
            if row["state"] == "pending":
                conn.execute(
                    "UPDATE job_slots SET state = 'skipped', reason = 'cancelled', updated_at = ? "
                    "WHERE job_id = ? AND due_utc = ?",
                    (now, job_id, due_utc),
                )
                outcome = "skipped"
            elif row["state"] == "running":
                conn.execute(
                    "UPDATE job_slots SET cancel_requested = 1, updated_at = ? "
                    "WHERE job_id = ? AND due_utc = ?",
                    (now, job_id, due_utc),
                )
                outcome = "cancel_requested"
            else:
                raise PrometheosError(
                    "validation_error", f"slot is already {row['state']}", {"due_utc": due_utc}
                )
    finally:
        conn.close()
    return {"job_id": job_id, "due_utc": due_utc, "outcome": outcome}


def resolve_digest(project_id: str, job_id: str, due_utc: str, resolution: str, reason: str) -> str:
    """The digest an owner decision capability must bind to resolve exactly this slot this way.
    Raises ``validation_error`` for an unknown resolution or a missing or over-long reason."""
    if resolution not in RESOLUTIONS:
        raise PrometheosError(
            "validation_error", f"resolution must be one of {sorted(RESOLUTIONS)}", {}
        )
    if not 1 <= len(reason.strip()) <= 500:
        raise PrometheosError("validation_error", "a reason of 1-500 characters is required", {})
    return canonical_sha256(
        {
            "op": "job_resolve_slot",
            "project_id": project_id,
            "job_id": job_id,
            "due_utc": due_utc,
            "resolution": resolution,
            "reason": reason.strip(),
        }
    )


def resolve_slot(
    store: ProjectStore,
    caller: Caller,
    job_id: str,
    due_utc: str,
    resolution: str,
    reason: str,
    capability: capabilities.Secret = None,
) -> dict[str, Any]:
    """The owner's explicit decision about a slot whose outcome is ``unknown`` (an external
    request began and its result was never seen). Never automatic.

    It is a write, so it needs an exact-plan owner decision capability (operation
    ``exact_request``) bound to :func:`resolve_digest`; it is counted in the same transaction
    as the change, exactly as for a protected ``apply``. The capability, not the caller's
    scope, is the authority.

    ``succeeded`` and ``failed`` record what the owner established outside PrometheOS
    (for example by looking at the provider's own records); ``abandoned`` records that the
    owner will not establish it (state ``skipped``). The reason is required and kept with the
    slot, and ``external_started_at`` is left as the evidence it is. Nothing is repeated,
    no finding is produced, and the project sequence does not advance (slot bookkeeping)."""
    digest = resolve_digest(store.project_id, job_id, due_utc, resolution, reason)
    reason = reason.strip()
    if not capability:
        raise PrometheosError(
            "policy_denied",
            "resolving an unknown slot is an owner decision: it needs an exact-plan owner "
            "decision capability bound to this digest",
            {"operation": "exact_request", "digest": digest},
        )
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            capabilities.consume(
                conn,
                secret=capability,
                project_id=store.project_id,
                operation="exact_request",
                digest=digest,
            )
            job = current_record(conn, job_id)
            if job is None or job["kind"] != "job":
                raise PrometheosError("not_found", "job not found", {"id": job_id})
            row = conn.execute(
                "SELECT state, result FROM job_slots WHERE job_id = ? AND due_utc = ?",
                (job_id, due_utc),
            ).fetchone()
            if row is None:
                raise PrometheosError("not_found", "slot not found", {"due_utc": due_utc})
            if row["state"] != "unknown":
                raise PrometheosError(
                    "validation_error",
                    f"only an unknown slot can be resolved; this one is {row['state']}",
                    {"due_utc": due_utc},
                )
            now = utc_now()
            result = loads_internal(row["result"]) if row["result"] else {}
            result["manual_resolution"] = {
                "from": "unknown",
                "as": resolution,
                "reason": reason,
                "by": caller.actor,
                "at": now,
            }
            state = RESOLUTIONS[resolution]
            conn.execute(
                "UPDATE job_slots SET state = ?, reason = ?, result = ?, updated_at = ? "
                "WHERE job_id = ? AND due_utc = ? AND state = 'unknown'",
                (
                    state,
                    f"resolved by the owner as {resolution}: {reason}",
                    dumps_canonical(result).decode(),
                    now,
                    job_id,
                    due_utc,
                ),
            )
    finally:
        conn.close()
    return {"job_id": job_id, "due_utc": due_utc, "resolution": resolution, "state": state}


def _manual_resolution(result: dict[str, Any] | None) -> dict[str, Any] | None:
    found = (result or {}).get("manual_resolution")
    return found if isinstance(found, dict) else None


def _slot_out(row: sqlite3.Row) -> dict[str, Any]:
    """One slot as every reader (CLI, service, hub) reports it. ``outcome`` is the state, except
    that a slot the owner resolved as ``abandoned`` (stored as ``skipped``, because the slot
    states are fixed) reports ``abandoned``: it is not a slot where nothing ran."""
    result = loads_internal(row["result"]) if row["result"] else None
    manual = _manual_resolution(result)
    return {
        "due_utc": row["due_utc"],
        "state": row["state"],
        "outcome": manual["as"] if manual else row["state"],
        "attempts": row["attempts"],
        "reason": row["reason"],
        "external_started_at": row["external_started_at"],
        "manual_resolution": manual,
        "result": result,
        "updated_at": row["updated_at"],
    }


def listing(
    conn: sqlite3.Connection, caller: Caller, now: datetime | None = None
) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    jobs = []
    for row in conn.execute("SELECT id FROM records WHERE kind = 'job' ORDER BY id").fetchall():
        job = current_record(conn, row["id"])
        if job is None or not caller.may_see(job["sensitivity"]):
            continue
        slots = conn.execute(
            "SELECT * FROM job_slots WHERE job_id = ? ORDER BY due_utc DESC LIMIT 10", (job["id"],)
        ).fetchall()
        counts: dict[str, int] = {}
        for r in conn.execute("SELECT state, result FROM job_slots WHERE job_id = ?", (job["id"],)):
            manual = _manual_resolution(loads_internal(r["result"]) if r["result"] else None)
            key = "abandoned" if manual and manual["as"] == "abandoned" else r["state"]
            counts[key] = counts.get(key, 0) + 1
        schedule = job["body"]["schedule"]
        jobs.append(
            {
                "job": job,
                "recent_slots": [_slot_out(s) for s in slots],
                "slot_counts": counts,
                "next_slots": upcoming(schedule, moment, 3) if job["body"]["enabled"] else [],
            }
        )
    return {"project_seq": project_seq(conn), "jobs": jobs}


def inbox(
    conn: sqlite3.Connection, caller: Caller, include_dismissed: bool = False, limit: int = 100
) -> dict[str, Any]:
    items = []
    for row in conn.execute(
        "SELECT * FROM job_notifications WHERE (? OR dismissed_at IS NULL) "
        "ORDER BY commit_seq DESC, id DESC LIMIT ?",
        (1 if include_dismissed else 0, limit),
    ):
        job = current_record(conn, row["job_id"])
        if job is None or not caller.may_see(job["sensitivity"]):
            continue
        items.append(
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "due_utc": row["due_utc"],
                "commit_seq": row["commit_seq"],
                "category": row["category"],
                "title": row["title"],
                "summary": row["summary"],
                "record_ids": loads_internal(row["record_ids"]),
                "created_at": row["created_at"],
                "dismissed_at": row["dismissed_at"],
            }
        )
    return {"project_seq": project_seq(conn), "items": items}


def dismiss(store: ProjectStore, caller: Caller, notification_id: int) -> dict[str, Any]:
    """Inbox state only: dismissal does not change research content or the sequence."""
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            row = conn.execute(
                "SELECT job_id FROM job_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            job = current_record(conn, row["job_id"]) if row else None
            if job is None or not caller.may_see(job["sensitivity"]):
                raise PrometheosError("not_found", "notification not found", {})
            conn.execute(
                "UPDATE job_notifications SET dismissed_at = ? "
                "WHERE id = ? AND dismissed_at IS NULL",
                (utc_now(), notification_id),
            )
    finally:
        conn.close()
    return {"id": notification_id, "dismissed": True}


# ----- handlers -----------------------------------------------------------------------------


@dataclass
class Finding:
    """A meaningful result: recorded as one commit with an inbox notification."""

    title: str
    summary: str
    record_ids: list[str]
    category: str = "finding"
    extra_writes: Callable[[Txn], None] | None = None


@dataclass
class Outcome:
    result: dict[str, Any]
    input_digest: str
    finding: Finding | None = None


@dataclass
class JobContext:
    store: ProjectStore
    job: dict[str, Any]
    due_utc: str
    owner: str
    now: datetime
    previous: dict[str, Any] | None = field(default=None)
    started: float = field(default_factory=time.monotonic)
    _last_checkpoint: float = field(default_factory=time.monotonic, init=False, repr=False)

    @property
    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = self.job["body"]
        return body

    def _clock(self) -> datetime:
        return self.now + timedelta(seconds=time.monotonic() - self.started)

    def _renew(self, conn: sqlite3.Connection, now: datetime) -> None:
        """Extend this slot's and this scheduler's leases, or raise ``LeaseLost``.

        Both must still be held and unexpired: after a pause longer than the lease (for
        example a sleeping laptop) another tick may have recovered the slot, and this
        handler must stop before doing anything that could repeat it."""
        until = fmt_precise(now + timedelta(seconds=LEASE_SECONDS))
        # Each lease is checked on its own so ``LeaseLost.lost`` says which one is gone. The
        # callers run this inside one write transaction, so a failure rolls back the other
        # renewal too.
        slot = conn.execute(
            "UPDATE job_slots SET lease_expires_at = ? WHERE job_id = ? AND due_utc = ? "
            "AND state = 'running' AND lease_owner = ? AND lease_expires_at >= ?",
            (until, self.job["id"], self.due_utc, self.owner, fmt_precise(now)),
        ).rowcount
        if slot != 1:
            raise LeaseLost("slot")
        scheduler = conn.execute(
            "UPDATE scheduler_lease SET expires_at = ?, heartbeat_at = ? "
            "WHERE owner = ? AND expires_at >= ?",
            (until, fmt_precise(now), self.owner, fmt_precise(now)),
        ).rowcount
        if scheduler != 1:
            raise LeaseLost("scheduler")

    def cancelled(self) -> bool:
        """Between units of work: renew the leases (``LeaseLost`` if gone) and report a
        cancellation request."""
        conn = self.store.connect()
        try:
            with db.write_transaction(conn):
                self._renew(conn, self._clock())
                row = conn.execute(
                    "SELECT cancel_requested FROM job_slots WHERE job_id = ? AND due_utc = ?",
                    (self.job["id"], self.due_utc),
                ).fetchone()
        finally:
            conn.close()
        return bool(row and row["cancel_requested"])

    def checkpoint(self) -> None:
        """Cheap call between small units of work. At most every ``CHECKPOINT_SECONDS`` it
        renews the leases and raises ``Cancelled`` once cancellation was requested (and
        ``LeaseLost`` if a lease is gone). Built-in handlers call it per unit of work, so a
        long run is cancellable and keeps its leases within a bounded interval."""
        if time.monotonic() - self._last_checkpoint < CHECKPOINT_SECONDS:
            return
        self._last_checkpoint = time.monotonic()
        if self.cancelled():
            raise Cancelled()

    def start_run(
        self, service: Any, operation: str, params: dict[str, Any], caller: Caller
    ) -> dict[str, Any]:
        """Start an operation run that names this slot (spec/03 RP-00: "a slot may start an
        operation run and links to it"; decision 0017). The run is queued, not executed: the
        slot keeps its own lease and outcome, and the run its own state and recovery. No v1
        handler does this yet (the handler set is closed, decision 0013 G1)."""
        view: dict[str, Any] = service.run_start(
            self.store.project_id,
            caller,
            operation,
            params,
            job=(str(self.job["id"]), self.due_utc),
            mode="none",
        )
        return view

    def before_external(self, description: str) -> None:
        """Commit the side-effect mark before anything leaves this computer.

        The mark is written only while both leases are still held; otherwise
        ``LeaseLost`` is raised and nothing may be sent (at most once per slot)."""
        conn = self.store.connect()
        try:
            with db.write_transaction(conn):
                now = self._clock()
                self._renew(conn, now)
                conn.execute(
                    "UPDATE job_slots SET external_started_at = ?, reason = ?, updated_at = ? "
                    "WHERE job_id = ? AND due_utc = ? AND state = 'running' AND lease_owner = ?",
                    (
                        fmt_precise(now),
                        _clip(f"external request started: {description}", 500),
                        fmt_precise(now),
                        self.job["id"],
                        self.due_utc,
                        self.owner,
                    ),
                )
        finally:
            conn.close()
        fault_point("job_after_external_mark")


class Cancelled(Exception):
    pass


class LeaseLost(Exception):
    """This tick no longer holds the slot (or scheduler) lease: stop, record nothing.
    ``lost`` names the lease: ``"slot"`` or ``"scheduler"``."""

    def __init__(self, lost: str = "slot") -> None:
        super().__init__(f"{lost} lease lost")
        self.lost = lost


Handler = Callable[[JobContext], Outcome]


def _finding_key(finding: dict[str, Any]) -> str:
    """Identity of a readiness finding: code, records and the row it is about."""
    detail = finding["detail"]
    row = detail.get("requirement") or (
        f"{detail['round']}/{detail['item']}" if "item" in detail else None
    )
    parts = [finding["code"], *finding["record_ids"], *([row] if row else [])]
    return " ".join(parts)


def _job_caller(ctx: JobContext) -> Caller:
    """Handlers see what the job's readers may see: a normal job reads as an agent, so its
    notification and slot result never describe restricted or private records."""
    if ctx.job["sensitivity"] == "normal":
        return Caller(actor="scheduler")
    return OWNER


def _paper_check(ctx: JobContext) -> Outcome:
    paper_id = ctx.body["parameters"]["paper_id"]
    if ctx.cancelled():  # also renews the slot lease
        raise Cancelled()
    # The retained media the check verifies are hashed first, with no store connection open
    # and a checkpoint before each; the snapshot below then finds them in the verification
    # cache and stays a database-only read (decision 0027: a cold multi-GiB check could
    # otherwise hold the project's admission for seconds).
    with closing(ctx.store.connect(readonly=True)) as conn, db.read_snapshot(conn):
        media = papers.retained_media(conn, _job_caller(ctx), paper_id)
    for storage_key, sha256, byte_length in media:
        ctx.checkpoint()
        reads._media_verified(ctx.store.directory, storage_key, sha256, byte_length)
    conn = ctx.store.connect(readonly=True)
    try:
        with db.read_snapshot(conn):
            # No checkpoint inside the snapshot: the store uses journal_mode=DELETE, so the
            # write that renews a lease or records a cancellation cannot commit while this
            # connection holds its read lock (it would wait out busy_timeout and fail). The
            # cancellation check above and the one after the handler bracket the snapshot.
            report = papers.check(conn, ctx.store.directory, _job_caller(ctx), paper_id)
    finally:
        conn.close()
    keys = sorted({_finding_key(f) for f in report["findings"]})
    previous = set((ctx.previous or {}).get("keys", []))
    result = {
        "findings_digest": report["findings_digest"],
        "counts": report["counts"],
        "keys": keys[:200],
        "project_seq": report["project_seq"],
        "usage": USAGE,
    }
    if ctx.previous is None:
        changed = bool(keys)  # the first run reports existing findings once
    else:
        changed = report["findings_digest"] != ctx.previous.get("findings_digest")
    if not changed:
        return Outcome(result, report["findings_digest"])
    added = sorted(set(keys) - previous)
    resolved = sorted(previous - set(keys))
    lines = [f"new: {k}" for k in added[:20]] + [f"resolved: {k}" for k in resolved[:20]]
    summary = "\n".join(lines) or "readiness findings changed"
    return Outcome(
        result,
        report["findings_digest"],
        Finding(
            title=f"Paper readiness changed ({len(added)} new, {len(resolved)} resolved)",
            summary=_clip(summary, ctx.body["budget"]["max_output_chars"]),
            record_ids=[paper_id],
        ),
    )


def _observation_state(obs: dict[str, Any], sha256: str) -> str:
    if obs["retained_digest_ok"] is False:
        return "retained bytes corrupt"
    if not obs["origin_reachable"]:
        return "origin unreachable"
    if obs["origin_current_digest"] != sha256:
        return "origin changed since this version"
    return "intact"


def _source_refresh(ctx: JobContext) -> Outcome:
    from prometheos.app import sources

    source_ids = ctx.body["parameters"]["source_ids"][: ctx.body["budget"]["max_inputs"]]
    changes: list[tuple[dict[str, Any], str]] = []
    states: dict[str, str] = {}
    for source_id in source_ids:
        if ctx.cancelled():
            raise Cancelled()
        # One short read per source; the retained bytes and the origin file are hashed
        # afterwards with no store connection open (decision 0027), so a long hash never
        # holds the project's admission against a cancellation or another reader.
        with closing(ctx.store.connect(readonly=True)) as conn, db.read_snapshot(conn):
            settings_record = current_record(conn, ctx.store.project_id)
            assert settings_record is not None
            settings = ProjectSettings.model_validate(settings_record["body"])
            latest = papers.latest_version(conn, _job_caller(ctx), source_id)
            target = None if latest is None else sources._observation_target(conn, latest["id"])
            saved = None if latest is None else conn.execute(
                "SELECT retained_digest_ok, origin_current_digest, origin_reachable "
                "FROM source_checks WHERE version_id = ? ORDER BY commit_seq DESC, id DESC LIMIT 1",
                (latest["id"],),
            ).fetchone()  # fmt: skip
        if latest is None or target is None:
            states[source_id] = "no version"
            continue
        obs = sources._observe(
            ctx.store, settings, target, progress=ctx.checkpoint
        )  # progress: once per hashed chunk of the origin file
        state = _observation_state(obs, latest["body"]["sha256"])
        states[source_id] = state
        if saved is None:
            if state != "intact":
                changes.append((obs, state))
            continue
        before = (
            None if saved["retained_digest_ok"] is None else bool(saved["retained_digest_ok"]),
            saved["origin_current_digest"],
            bool(saved["origin_reachable"]),
        )
        now_seen = (
            obs["retained_digest_ok"],
            obs["origin_current_digest"],
            obs["origin_reachable"],
        )
        if before != now_seen:
            changes.append((obs, state))
    digest = canonical_sha256(states)
    result = {"states": states, "changed": [o["version_id"] for o, _ in changes], "usage": USAGE}
    if not changes:
        return Outcome(result, digest)

    def write(txn: Txn) -> None:
        # A version purged after its short read is never observed in the record (purge deletes
        # its source checks), whatever the rendered summary shows (decision 0027 r3).
        purged = _purged_named(txn.conn, " ".join(o["version_id"] for o, _ in changes))
        observed = [o for o, _ in changes if o["version_id"] not in purged]
        if not observed:
            raise _Withheld(WITHHELD_OBSERVATIONS)
        for obs in observed:
            txn.conn.execute(
                "INSERT INTO source_checks (version_id, checked_at, retained_digest_ok, "
                "origin_current_digest, origin_reachable, error, commit_seq) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    obs["version_id"],
                    obs["checked_at"],
                    obs["retained_digest_ok"],
                    obs["origin_current_digest"],
                    obs["origin_reachable"],
                    obs["error"],
                    txn.seq,
                ),
            )
            txn.audit("source_check", obs["version_id"], None)

    summary = "\n".join(f"{o['source_id']} {o['version_id']}: {s}" for o, s in changes)
    return Outcome(
        result,
        digest,
        Finding(
            title=f"Source check: {len(changes)} source(s) changed state",
            summary=_clip(summary, ctx.body["budget"]["max_output_chars"]),
            record_ids=sorted({o["source_id"] for o, _ in changes}),
            extra_writes=write,
        ),
    )


def _digest(ctx: JobContext) -> Outcome:
    if ctx.cancelled():  # also renews the slot lease
        raise Cancelled()
    # The notification is as visible as the job: count only records at or below its level.
    allowed = [s for s, rank in SENSITIVITY_ORDER.items()
               if rank <= SENSITIVITY_ORDER[ctx.job["sensitivity"]]]  # fmt: skip
    marks = ",".join("?" for _ in allowed)
    ctx.checkpoint()
    conn = ctx.store.connect(readonly=True)
    try:
        with db.read_snapshot(conn):
            current = project_seq(conn)
            since = (ctx.previous or {}).get("project_seq")
            if since is None:
                created = conn.execute(
                    "SELECT created_seq FROM records WHERE id = ?", (ctx.job["id"],)
                ).fetchone()
                since = int(created["created_seq"])
            # A purged record (lifecycle 'deleted', which only purge writes, with its tombstone)
            # is neither listed nor counted: its tombstone revision is no change to report.
            rows = conn.execute(
                "SELECT r.kind, v.record_id, MAX(v.commit_seq) AS seq FROM revisions v "  # noqa: S608
                "JOIN records r ON r.id = v.record_id WHERE v.commit_seq > ? AND v.commit_seq <= ? "
                f"AND r.kind != 'job' AND r.lifecycle != 'deleted' AND r.sensitivity IN ({marks}) "
                "GROUP BY v.record_id ORDER BY seq DESC, v.record_id",
                (since, current, *allowed),
            ).fetchall()
    finally:
        conn.close()
    ctx.checkpoint()  # the one query above is this handler's only unbounded-looking step
    kinds: dict[str, int] = {}
    for row in rows:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    result = {"project_seq": current, "since_seq": since, "kinds": kinds, "usage": USAGE}
    digest = canonical_sha256({"since": since, "current": current, "kinds": kinds})
    if not rows:
        return Outcome(result, digest)
    limit = min(20, ctx.body["budget"]["max_inputs"])
    summary = ", ".join(f"{k}: {n}" for k, n in sorted(kinds.items()))
    return Outcome(
        result,
        digest,
        Finding(
            title=f"Project digest: {len(rows)} record(s) changed since sequence {since}",
            summary=_clip(summary, ctx.body["budget"]["max_output_chars"]),
            record_ids=[r["record_id"] for r in rows[:limit]],
            category="digest",
        ),
    )


HANDLERS: dict[str, Handler] = {
    "paper_check": _paper_check,
    "source_refresh": _source_refresh,
    "digest": _digest,
}


# ----- scheduler -----------------------------------------------------------------------------


def slot_request_id(job_id: str, due_utc: str) -> str:
    """A request key derived from the slot, so a slot's finding commits once."""
    return derived_request_id("job-finding", job_id, due_utc)


def _acquire(conn: sqlite3.Connection, owner: str, now: datetime) -> None:
    with db.write_transaction(conn):
        row = conn.execute("SELECT owner, expires_at FROM scheduler_lease").fetchone()
        if row is not None and row["owner"] != owner and parse(row["expires_at"]) > now:
            raise PrometheosError(
                "busy",
                "another scheduler is running for this project",
                {"until": row["expires_at"]},
            )
        expires = fmt_precise(now + timedelta(seconds=LEASE_SECONDS))
        conn.execute(
            "INSERT INTO scheduler_lease (singleton, owner, expires_at, heartbeat_at) "
            "VALUES (1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET owner = excluded.owner, "
            "expires_at = excluded.expires_at, heartbeat_at = excluded.heartbeat_at",
            (owner, expires, fmt_precise(now)),
        )


def _release(conn: sqlite3.Connection, owner: str) -> None:
    with db.write_transaction(conn):
        conn.execute("DELETE FROM scheduler_lease WHERE owner = ?", (owner,))


def _enabled_since(conn: sqlite3.Connection, job_id: str) -> datetime:
    """When the job most recently became enabled (creation or re-enable)."""
    since: str | None = None
    enabled = False
    for row in conn.execute(
        "SELECT v.recorded_at, b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
        "WHERE v.record_id = ? ORDER BY v.rev",
        (job_id,),
    ):
        body = json.loads(row["encoded"]) if row["encoded"] else {}
        now_enabled = bool(body.get("enabled"))
        if now_enabled and not enabled:
            since = row["recorded_at"]
        enabled = now_enabled
    return parse(since) if since else datetime.now(UTC)


def _schedule_slots(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    """Record due slots for enabled jobs; coalesce a backlog to one pending slot."""
    report = {"pending": 0, "coalesced": 0}
    stamp = fmt_precise(now)
    with db.write_transaction(conn):
        for row in conn.execute(
            "SELECT id FROM records WHERE kind = 'job' AND lifecycle = 'active' ORDER BY id"
        ).fetchall():
            job = current_record(conn, row["id"])
            if job is None or not job["body"]["enabled"]:
                continue
            last = conn.execute(
                "SELECT MAX(due_utc) AS due FROM job_slots WHERE job_id = ?", (job["id"],)
            ).fetchone()["due"]
            after = _enabled_since(conn, job["id"])
            if last is not None:
                after = max(after, parse(last))
            due, total = due_between(job["body"]["schedule"], after, now)
            if not due:
                continue
            *earlier, latest = due
            not_itemised = total - len(due)
            for moment in earlier:
                conn.execute(
                    "INSERT OR IGNORE INTO job_slots (job_id, due_utc, state, reason, created_at, "
                    "updated_at) VALUES (?, ?, 'skipped', ?, ?, ?)",
                    (
                        job["id"],
                        fmt(moment),
                        "coalesced: missed while no scheduler ran",
                        stamp,
                        stamp,
                    ),
                )
            # Older slots still waiting are superseded by the current one. One whose finding an
            # earlier (pre-0027) attempt recorded succeeded, and is finished so, not coalesced.
            for old in conn.execute(
                "SELECT due_utc FROM job_slots WHERE job_id = ? AND state = 'pending' "
                "AND due_utc < ?",
                (job["id"], fmt(latest)),
            ).fetchall():
                if _finding_recorded(conn, job["id"], old["due_utc"]):
                    _succeed_recorded_earlier(conn, job["id"], old["due_utc"], stamp)
            stale = conn.execute(
                "UPDATE job_slots SET state = 'skipped', reason = ?, updated_at = ? "
                "WHERE job_id = ? AND state = 'pending' AND due_utc < ?",
                ("coalesced: superseded by a later slot", stamp, job["id"], fmt(latest)),
            ).rowcount
            note = {"coalesced_slots": len(earlier) + not_itemised + stale}
            if not_itemised:
                note["not_itemised"] = not_itemised
            inserted = conn.execute(
                "INSERT OR IGNORE INTO job_slots (job_id, due_utc, state, result, created_at, "
                "updated_at) VALUES (?, ?, 'pending', ?, ?, ?)",
                (job["id"], fmt(latest), dumps_canonical(note).decode(), stamp, stamp),
            ).rowcount
            report["pending"] += inserted
            report["coalesced"] += len(earlier) + not_itemised + stale
    return report


def _recover(
    store: ProjectStore, conn: sqlite3.Connection, now: datetime, actor: str
) -> list[dict[str, Any]]:
    """Expired running slots: unknown after an external mark, else bounded retry. A slot whose
    finding an earlier attempt already recorded succeeded and is finished as such."""
    recovered = []
    stamp = fmt_precise(now)
    rows = conn.execute(
        "SELECT * FROM job_slots WHERE state = 'running' AND lease_expires_at < ?", (stamp,)
    ).fetchall()
    for row in rows:
        result: str | None = None
        if _finding_recorded(conn, row["job_id"], row["due_utc"]):
            state, reason = "succeeded", RECORDED_EARLIER
            result = dumps_canonical({"recorded_earlier": True, "usage": USAGE}).decode()
        elif row["external_started_at"]:
            state, reason = (
                "unknown",
                (
                    "lease expired after an external request began; the outcome is unknown "
                    "and the slot is not repeated automatically"
                ),
            )
        elif row["attempts"] >= MAX_ATTEMPTS:
            state, reason = "failed", "lease expired; no attempts left"
        else:
            state, reason = "pending", "lease expired before any side effect; will retry"
        next_at = (
            fmt_precise(now + timedelta(seconds=BACKOFF_SECONDS[min(row["attempts"], 3) - 1]))
            if state == "pending"
            else None
        )
        if state in ("unknown", "failed"):
            # Notice first (keyed by the slot, so a retry after a crash replays it).
            _notify_problem(store, row["job_id"], row["due_utc"], state, reason, actor)
        with db.write_transaction(conn):
            changed = conn.execute(
                "UPDATE job_slots SET state = ?, reason = ?, result = COALESCE(?, result), "
                "lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = ?, "
                "updated_at = ? WHERE job_id = ? AND due_utc = ? AND state = 'running' "
                "AND lease_expires_at < ?",
                (state, reason, result, next_at, stamp, row["job_id"], row["due_utc"], stamp),
            ).rowcount
        if changed:
            recovered.append(
                {
                    "job_id": row["job_id"],
                    "due_utc": row["due_utc"],
                    "state": state,
                    "reason": reason,
                }
            )
    return recovered


def _notify_problem(
    store: ProjectStore, job_id: str, due_utc: str, state: str, reason: str, actor: str
) -> None:
    finding = Finding(
        title=f"Job slot {state}: decision needed" if state == "unknown" else f"Job slot {state}",
        summary=reason,
        record_ids=[job_id],
        category=f"slot_{state}",
    )
    _commit_finding(store, job_id, due_utc, finding, actor, f"slot-{state}", touch_job=False)
    # A crash here leaves the slot running with an expired lease; recovery notifies again
    # (replayed, so recorded once) and then changes the state. Notice first, state second.
    fault_point("job_after_notice")


RECORDED_EARLIER = "succeeded: an earlier attempt recorded this slot's finding"
WITHHELD = "withheld: every record it listed was purged before it could be recorded"
WITHHELD_OBSERVATIONS = "withheld: every version it observed was purged before it could be recorded"
"""Marks the refresh's withheld exception only. It is never stored: such a refresh result names
the purged versions in ``changed``, so ``_finish_row`` clears it as purge clears slot details."""
PURGED = "[purged record]"
"""What a purged record's ID becomes in a finding's text: neutral like ``reads.HIDDEN``, it
reveals neither the ID nor anything about the record (decision 0027 r3). The rest of the line
stays: a readiness finding keeps its code (``new: <code> [purged record]``), a refresh line its
observation state, and a title its counts, which include the purged item. All of it describes
a record the job's readers could see when the handler read it."""
_RECORD_ID_IN_TEXT = re.compile(rf"(?:{'|'.join(KINDS)})_{UUID4_PATTERN}")


class _Withheld(Exception):
    """Nothing the finding is about is left unpurged: nothing of it is committed. ``marker``
    says why; it becomes the slot result's ``finding_withheld``, unless that result names a
    purged record and is cleared (always so for a refresh)."""

    def __init__(self, marker: str) -> None:
        super().__init__(marker)
        self.marker = marker


def _succeed_recorded_earlier(
    conn: sqlite3.Connection, job_id: str, due_utc: str, stamp: str
) -> None:
    """Finish a pending slot whose finding an earlier attempt recorded as ``succeeded``,
    without running it or counting an attempt. The caller holds a write transaction."""
    conn.execute(
        "UPDATE job_slots SET state = 'succeeded', reason = ?, result = ?, "
        "next_attempt_at = NULL, updated_at = ? "
        "WHERE job_id = ? AND due_utc = ? AND state = 'pending'",
        (
            RECORDED_EARLIER,
            dumps_canonical({"recorded_earlier": True, "usage": USAGE}).decode(),
            stamp,
            job_id,
            due_utc,
        ),
    )


def _clip(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, never inside a record ID: an ID the cut
    would split is dropped whole. A fragment of an ID matches neither the redaction below nor
    purge's own rule, yet can identify the record to anyone holding its full ID (decision 0027
    r4), so every summary and reason this module cuts goes through here."""
    if len(text) <= limit:
        return text
    for match in _RECORD_ID_IN_TEXT.finditer(text):
        if match.start() < limit < match.end():
            return text[: match.start()]
        if match.start() >= limit:
            break
    return text[:limit]


def _redact_purged(text: str, purged: set[str]) -> str:
    """``text`` with every purged record ID replaced by :data:`PURGED`."""
    if not purged:
        return text
    return _RECORD_ID_IN_TEXT.sub(lambda m: PURGED if m.group(0) in purged else m.group(0), text)


def _purged_named(conn: sqlite3.Connection, text: str) -> set[str]:
    """Record IDs named in ``text`` whose record has been purged (its current body is the
    purge tombstone). Purge removes every notification and slot detail naming a purged
    record, so a later job write must not name one either (decision 0027)."""
    ids = sorted(set(_RECORD_ID_IN_TEXT.findall(text)))
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT r.id, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id "  # noqa: S608
        f"AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.id IN ({marks})",
        ids,
    ).fetchall()
    return {
        str(r["id"])
        for r in rows
        if r["encoded"] is not None and loads_internal(r["encoded"]).get("purged") is True
    }


def _finding_recorded(conn: sqlite3.Connection, job_id: str, due_utc: str) -> bool:
    """Whether this slot's finding is committed (under its slot-derived request key)."""
    return (
        conn.execute(
            "SELECT 1 FROM requests WHERE request_id = ?",
            (slot_request_id(job_id, f"{due_utc}:finding"),),
        ).fetchone()
        is not None
    )


def _commit_finding(
    store: ProjectStore,
    job_id: str,
    due_utc: str,
    finding: Finding,
    actor: str,
    label: str = "finding",
    touch_job: bool = True,
    owner: str | None = None,
    success: tuple[dict[str, Any], str] | None = None,
) -> dict[str, Any]:
    """Commit a finding or notice under the slot-derived request key. A replay of an identical
    earlier commit returns its stored response; a different earlier one ``already_recorded``.

    ``owner`` and ``success`` (the attempt's result and input digest) go together: the slot
    must still be running under that owner in the same transaction, and is finished there as
    ``succeeded``, so a finding is committed exactly when its slot succeeded and a claimant
    whose slot was recovered meanwhile records nothing (``LeaseLost``). Either alone is a
    ``TypeError``: the ownership check alone would not finish the slot.

    Record IDs purged meanwhile are dropped from ``record_ids`` and replaced by
    :data:`PURGED` in the title and summary. Only when the finding listed records and none is
    left, or its ``extra_writes`` find nothing left to write, is nothing committed; the
    response's ``withheld`` then says why (decision 0027 r3)."""
    if (owner is None) != (success is None):
        raise TypeError("_commit_finding takes owner and success together, or neither")
    content = {
        "op": "job_finding",
        "job": job_id,
        "slot": due_utc,
        "label": label,
        "title": finding.title,
        "summary": finding.summary,
        "record_ids": finding.record_ids,
        "category": finding.category,
    }

    def handler(txn: Txn) -> None:
        if owner is not None and not _holds(txn.conn, job_id, due_utc, owner):
            raise LeaseLost("slot")
        purged = _purged_named(
            txn.conn, f"{finding.title}\n{finding.summary}\n{' '.join(finding.record_ids)}"
        )
        record_ids = [r for r in finding.record_ids if r not in purged]
        if finding.record_ids and not record_ids:
            raise _Withheld(WITHHELD)
        if finding.extra_writes is not None:
            finding.extra_writes(txn)  # may withhold too: it applies the rule to what it writes
        txn.conn.execute(
            "INSERT INTO job_notifications (job_id, due_utc, commit_seq, category, title, summary, "
            "record_ids, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                job_id,
                due_utc,
                txn.seq,
                finding.category,
                _redact_purged(finding.title, purged),
                _redact_purged(finding.summary, purged),
                dumps_canonical(record_ids).decode(),
                txn.recorded_at,
            ),
        )
        txn.audit(f"job_finding:{label}", job_id, None)
        state = txn.get(job_id)
        if touch_job and state is not None:
            body = validate_job(dict(state.body, last_slot=due_utc)).model_dump(mode="json")
            body_id = txn.insert_body(body)
            txn.write_revision(
                job_id,
                "job",
                state.rev + 1,
                body_id,
                state.sensitivity,
                state.lifecycle,
                None,
                "job_finding",
                body=body,
            )
        if success is not None:
            assert owner is not None  # given together (checked above); narrows the type
            result, input_digest = success
            if not _finish_row(
                txn.conn, job_id, due_utc, owner, "succeeded", result=result,
                input_digest=input_digest,
            ):  # fmt: skip
                raise LeaseLost("slot")
        txn.result(job_id, None)

    request_id = slot_request_id(job_id, f"{due_utc}:{label}")
    try:
        return store.commit(
            request_id=request_id,
            request_sha256=canonical_sha256(content),
            expected_seq=None,
            actor=actor,
            handler=handler,
        )
    except _Withheld as withheld:
        return {"ok": True, "withheld": withheld.marker}
    except PrometheosError as exc:
        if exc.code == "request_key_conflict":
            # An earlier attempt of this slot already recorded a (different) finding.
            return {"ok": True, "already_recorded": True}
        raise


def _previous_result(conn: sqlite3.Connection, job_id: str, due_utc: str) -> dict[str, Any] | None:
    # A slot the owner resolved as succeeded, or one finished from a finding an earlier
    # attempt recorded, carries no handler result to compare with. A slot whose finding was
    # withheld reported nothing, so it is no baseline either: the next run compares with the
    # one before and reports again what it could not (decision 0027 r3).
    skip = {"manual_resolution", "recorded_earlier", "finding_withheld"}
    for row in conn.execute(
        "SELECT result FROM job_slots WHERE job_id = ? AND state = 'succeeded' AND due_utc < ? "
        "ORDER BY due_utc DESC",
        (job_id, due_utc),
    ):
        result: dict[str, Any] | None = loads_internal(row["result"]) if row["result"] else None
        if result is not None and not skip & set(result):
            return result
    return None


def _claim_row(
    conn: sqlite3.Connection, job_id: str, due_utc: str, owner: str, now: datetime
) -> bool:
    """The claim's atomic predicate: the slot is still pending and no other slot of the job
    is running. The caller holds a write transaction."""
    return (
        conn.execute(
            "UPDATE job_slots SET state = 'running', lease_owner = ?, lease_expires_at = ?, "
            "attempts = attempts + 1, next_attempt_at = NULL, updated_at = ? "
            "WHERE job_id = ? AND due_utc = ? AND state = 'pending' AND NOT EXISTS "
            "(SELECT 1 FROM job_slots WHERE job_id = ? AND state = 'running')",
            (
                owner,
                fmt_precise(now + timedelta(seconds=LEASE_SECONDS)),
                fmt_precise(now),
                job_id,
                due_utc,
                job_id,
            ),
        ).rowcount
        == 1
    )


def _claim(conn: sqlite3.Connection, job_id: str, due_utc: str, owner: str, now: datetime) -> bool:
    with db.write_transaction(conn):
        return _claim_row(conn, job_id, due_utc, owner, now)


def _take(
    conn: sqlite3.Connection, job_id: str, due_utc: str, owner: str, now: datetime
) -> tuple[dict[str, Any], dict[str, Any] | None, int] | str:
    """Re-read a candidate slot and claim it in one write transaction, or say why not.

    A row selected earlier in the tick is selection input only: the slot, its job and its
    cancellation are read again here and the claim's own predicate decides (decision 0027).
    Returns the job, the previous result and this attempt's number; or ``skipped`` (the job
    is disabled or the slot cancelled), ``not_claimed`` (no longer due, or another slot of
    the job is running) or ``succeeded`` (an earlier attempt recorded the slot's finding; it
    is finished without running again or counting an attempt). A refused claim changes
    nothing and counts no attempt."""
    stamp = fmt_precise(now)
    with db.write_transaction(conn):
        row = conn.execute(
            "SELECT cancel_requested, attempts FROM job_slots WHERE job_id = ? AND due_utc = ? "
            "AND state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
            (job_id, due_utc, stamp),
        ).fetchone()
        if row is None:
            return "not_claimed"
        if _finding_recorded(conn, job_id, due_utc):
            _succeed_recorded_earlier(conn, job_id, due_utc, stamp)
            return "succeeded"
        job = current_record(conn, job_id)
        if job is None or job["lifecycle"] != "active" or not job["body"]["enabled"]:
            reason = "job disabled"
        elif row["cancel_requested"]:
            reason = "cancelled"
        elif _claim_row(conn, job_id, due_utc, owner, now):
            return job, _previous_result(conn, job_id, due_utc), int(row["attempts"]) + 1
        else:
            return "not_claimed"
        conn.execute(
            "UPDATE job_slots SET state = 'skipped', reason = ?, updated_at = ? "
            "WHERE job_id = ? AND due_utc = ? AND state = 'pending'",
            (reason, stamp, job_id, due_utc),
        )
        return "skipped"


def _holds(conn: sqlite3.Connection, job_id: str, due_utc: str, owner: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM job_slots WHERE job_id = ? AND due_utc = ? AND state = 'running' "
            "AND lease_owner = ?",
            (job_id, due_utc, owner),
        ).fetchone()
        is not None
    )


def _held_external(store: ProjectStore, job_id: str, due_utc: str, owner: str) -> bool | None:
    """In one short read: ``None`` when this owner no longer holds the slot (another tick
    recovered it; the caller records nothing), else whether its external mark is set."""
    with closing(store.connect(readonly=True)) as conn:
        row = conn.execute(
            "SELECT external_started_at FROM job_slots WHERE job_id = ? AND due_utc = ? "
            "AND state = 'running' AND lease_owner = ?",
            (job_id, due_utc, owner),
        ).fetchone()
    return None if row is None else bool(row["external_started_at"])


def _finish_row(
    conn: sqlite3.Connection,
    job_id: str,
    due_utc: str,
    owner: str,
    state: str,
    *,
    result: dict[str, Any] | None = None,
    reason: str | None = None,
    input_digest: str | None = None,
    next_at: str | None = None,
) -> bool:
    """The owner-qualified finishing update, inside the caller's write transaction. A result or
    reason naming a record purged meanwhile is recorded as purge records it: no result, and
    the reason ``details removed by purge`` (decision 0027)."""
    encoded = dumps_canonical(result).decode() if result is not None else None
    cleared = bool(_purged_named(conn, f"{encoded or ''}\n{reason or ''}"))
    if cleared:
        encoded, reason = None, "details removed by purge"
    return (
        conn.execute(
            "UPDATE job_slots SET state = ?, "
            "result = CASE WHEN ? THEN NULL ELSE COALESCE(?, result) END, reason = ?, "
            "input_digest = COALESCE(?, input_digest), lease_owner = NULL, "
            "lease_expires_at = NULL, next_attempt_at = ?, updated_at = ? "
            "WHERE job_id = ? AND due_utc = ? AND state = 'running' AND lease_owner = ?",
            (
                state,
                cleared,
                encoded,
                reason,
                input_digest,
                next_at,
                utc_now(),
                job_id,
                due_utc,
                owner,
            ),
        ).rowcount
        == 1
    )


def _finish(
    store: ProjectStore,
    job_id: str,
    due_utc: str,
    owner: str,
    state: str,
    *,
    result: dict[str, Any] | None = None,
    reason: str | None = None,
    input_digest: str | None = None,
    next_at: str | None = None,
) -> bool:
    """Finish a slot this owner still holds, in its own short connection; ``False`` when the
    lease was lost meanwhile, which every caller reports as ``lease_lost``."""
    with closing(store.connect()) as conn, db.write_transaction(conn):
        return _finish_row(
            conn, job_id, due_utc, owner, state, result=result, reason=reason,
            input_digest=input_digest, next_at=next_at,
        )  # fmt: skip


def _complete(
    store: ProjectStore, job_id: str, due_utc: str, owner: str, actor: str, outcome: Outcome
) -> tuple[bool, bool]:
    """Record a successful attempt; returns (still held, finding recorded).

    With a finding, the finding and the slot's ``succeeded`` state are one transaction: a
    refused completion records neither, and no finding is ever committed beside a slot that
    did not succeed (decision 0027). A finding an earlier attempt already recorded (a replay)
    leaves only the owner-qualified finish to do. A withheld finding (nothing it was about is
    left unpurged) leaves a quiet success whose result says why."""
    if outcome.finding is None:
        held = _finish(
            store, job_id, due_utc, owner, "succeeded", result=outcome.result,
            input_digest=outcome.input_digest,
        )  # fmt: skip
        return held, False
    result = outcome.result | {"finding": True}
    done = _commit_finding(
        store, job_id, due_utc, outcome.finding, actor, owner=owner,
        success=(result, outcome.input_digest),
    )  # fmt: skip
    if done.get("withheld"):
        result = outcome.result | {"finding": False, "finding_withheld": done["withheld"]}
        held = _finish(
            store, job_id, due_utc, owner, "succeeded", result=result,
            input_digest=outcome.input_digest,
        )  # fmt: skip
        return held, False
    if done.get("replayed") or done.get("already_recorded"):
        held = _finish(
            store, job_id, due_utc, owner, "succeeded", result=result,
            input_digest=outcome.input_digest,
        )  # fmt: skip
        return held, True
    return True, True


def _run_slot(
    store: ProjectStore, job_id: str, due_utc: str, owner: str, now: datetime, actor: str
) -> dict[str, Any]:
    slot: dict[str, Any] = {"job_id": job_id, "due_utc": due_utc}
    # Another tick recovered (or re-claimed) the slot: its record stands, nothing is written.
    lost = slot | {"state": "lease_lost", "lost": "slot"}
    with closing(store.connect()) as conn:
        taken = _take(conn, job_id, due_utc, owner, now)
    if taken == "succeeded":
        return slot | {"state": "succeeded", "finding": True}
    if isinstance(taken, str):
        return slot | {"state": taken}
    job, previous, attempts = taken
    # No store connection is open from here while the handler works (decision 0027); each
    # step below opens its own short one.
    ctx = JobContext(store, job, due_utc, owner, now, previous)
    fault_point("job_after_claim")
    try:
        outcome = HANDLERS[job["body"]["handler"]](ctx)
        if ctx.cancelled():
            raise Cancelled()
    except LeaseLost as gone:
        # Another tick recovered this slot; its record stands and nothing was sent here.
        return slot | {"state": "lease_lost", "lost": gone.lost}
    except Cancelled:
        external = _held_external(store, job_id, due_utc, owner)
        if external is None:
            return lost
        state = "unknown" if external else "skipped"
        reason = (
            "cancelled after an external request began; transmitted data cannot be recalled"
            if external
            else "cancelled"
        )
        if state == "unknown":
            # The owner has something to resolve, so tell them first (as the other paths do).
            _notify_problem(store, job_id, due_utc, state, reason, actor)
        if not _finish(store, job_id, due_utc, owner, state, reason=reason):
            return lost
        return slot | {"state": state}
    except Exception as exc:  # a handler failure must never leave the slot running
        code = exc.code if isinstance(exc, PrometheosError) else type(exc).__name__
        external = _held_external(store, job_id, due_utc, owner)
        if external is None:
            return lost
        if external:
            state, next_at = "unknown", None
            reason = f"failed after an external request began ({code}); outcome unknown"
        elif attempts < MAX_ATTEMPTS:
            state = "pending"
            next_at = fmt_precise(now + timedelta(seconds=BACKOFF_SECONDS[attempts - 1]))
            reason = f"attempt {attempts} failed ({code}); will retry"
        else:
            state, next_at = "failed", None
            reason = f"failed after {attempts} attempts ({code})"
        if state in ("unknown", "failed"):
            # Notice first: a crash before the state change is recovered later and the
            # notice, keyed by the slot, is not duplicated.
            _notify_problem(store, job_id, due_utc, state, reason, actor)
        if not _finish(store, job_id, due_utc, owner, state, reason=reason, next_at=next_at):
            return lost
        return slot | {"state": state, "reason": reason}
    # The handler succeeded. Recording that is not part of the attempt: a refusal here (a busy
    # store) records nothing and propagates, the slot keeps its lease and is recovered when it
    # expires, and it is never reclassified as a handler failure (decision 0027).
    fault_point("job_before_finish")
    try:
        held, found = _complete(store, job_id, due_utc, owner, actor, outcome)
    except LeaseLost as gone:
        return slot | {"state": "lease_lost", "lost": gone.lost}
    if not held:
        return lost
    return slot | {"state": "succeeded", "finding": found}


def tick(
    store: ProjectStore, actor: str, now: datetime | None = None, owner: str | None = None
) -> dict[str, Any]:
    """One bounded scheduler pass (spec/08). ``now`` is injectable for tests.

    Every step uses its own short admitted connection and none is open while a slot's
    handler works (decision 0027); the durable scheduler lease excludes a second tick."""
    moment = now or datetime.now(UTC)
    who = owner or f"tick:{new_id('run')[4:]}"
    with closing(store.connect()) as conn:
        _acquire(conn, who, moment)
    try:
        with closing(store.connect()) as conn:
            recovered = _recover(store, conn, moment, actor)
            scheduled = _schedule_slots(conn, moment)
            due = [
                (r["job_id"], r["due_utc"])
                for r in conn.execute(
                    "SELECT job_id, due_utc FROM job_slots WHERE state = 'pending' "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                    "ORDER BY due_utc, job_id LIMIT ?",
                    (fmt_precise(moment), MAX_SLOTS_PER_TICK),
                ).fetchall()
            ]
        ran = []
        started = time.monotonic()
        for job_id, due_utc in due:
            at = moment + timedelta(seconds=time.monotonic() - started)
            # Renew the scheduler lease before each slot, so a long pass keeps it.
            with closing(store.connect()) as conn:
                _acquire(conn, who, at)
            ran.append(_run_slot(store, job_id, due_utc, who, at, actor))
    finally:
        try:
            with closing(store.connect()) as conn:
                _release(conn, who)  # owner-qualified: never another scheduler's newer lease
        except PrometheosError as exc:
            # The lease expires on its own; a refused release must not replace what this
            # tick recorded (or the error it is already raising) with ``busy``.
            if exc.code != "busy":
                raise
    return {
        "now": fmt_precise(moment),
        "recovered": recovered,
        "scheduled": scheduled,
        "ran": ran,
        "quiet": [r for r in ran if r["state"] == "succeeded" and not r.get("finding")],
    }


def slots(conn: sqlite3.Connection, caller: Caller, job_id: str) -> list[dict[str, Any]]:
    job = current_record(conn, job_id)
    if job is None or job["kind"] != "job" or not caller.may_see(job["sensitivity"]):
        raise PrometheosError("not_found", "job not found", {"id": job_id})
    return [
        _slot_out(r)
        for r in conn.execute(
            "SELECT * FROM job_slots WHERE job_id = ? ORDER BY due_utc", (job_id,)
        ).fetchall()
    ]
