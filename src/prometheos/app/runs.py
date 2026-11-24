"""Durable operation runs (A02, CP10.3; spec/04 "Operation runs"; decision 0017).

``operation_runs`` is the single run of record for app-owned long operations. A run is
created at once (``oprun_<UUID4>``) with what resume needs (parameters and their digest,
input digests, configuration digest, model, workflow version, budget reservation) and is then
executed by a worker: a thread of a long-lived server (hub, MCP), the foreground of a CLI
command, or a detached ``prometheos run resume`` process.

A handler declares its operation's stages through :class:`RunContext`:

- ``ctx.output(name)`` is the stored output of a completed stage (``None`` when it did not
  complete), so a resumed run skips what is done;
- ``ctx.complete(name, output, charged)`` records a stage as completed (and what it cost);
- ``ctx.checkpoint(label)`` renews the lease and raises :class:`Cancelled` once a
  cancellation was requested: long handlers call it between stages;
- ``ctx.charge(amount)`` checks the run's budget reservation and raises
  :class:`BudgetPause` (state ``paused_budget``) when a stage would exceed it;
- ``ctx.external_started(...)`` is written *before* a billable or external write is sent;
  ``ctx.external_confirmed()`` after its outcome is known; an uncertain outcome raises
  :class:`ExternalUncertain` (state ``unknown_external``), which is never retried
  automatically and needs an explicit acknowledgement.

Status writes (state, lease, heartbeat, logs, stages) are operational rows: they never move
the project sequence. Research-visible results commit through the transaction service under
request keys derived from the run, so a resumed or replayed stage never duplicates them.

A worker that dies leaves a ``running`` row whose lease expires (or, on this computer, whose
process is gone). Recovery then makes the state truthful: ``queued`` (resume continues after
the last completed stage), ``cancelled`` (a cancellation was requested and nothing runs any
more) or ``unknown_external`` (an external call had started and its outcome is unknown).
"""

from __future__ import annotations

import functools
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from prometheos.app import outbox, reads
from prometheos.domain import redact
from prometheos.domain.canonical import canonical_sha256, dumps_canonical, loads_internal
from prometheos.domain.ids import is_request_id, new_operational_id
from prometheos.domain.models import RETRYABLE, PrometheosError
from prometheos.faults import fault_point
from prometheos.interpreter import child_flags
from prometheos.store import db, pending
from prometheos.store import operational as op
from prometheos.store.home import app_home
from prometheos.store.project import ProjectStore, rank_bound_sha256

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 15.0
REMOVED_MESSAGE = "the run was removed (the records it worked on were purged)"
MAX_WAIT_SECONDS = 20
MAX_OUTPUT_BYTES = 256 * 1024
VIEW_LINES = 50
ACTIVE_STATES = frozenset({"queued", "running"})
RESUMABLE_STATES = frozenset({"queued", "failed", "paused_budget"})
SIX = Decimal("0.000001")

# In-process count of run writes (decision 0076): creation, every status write and every
# worker transaction (stage, progress, log). The hub's run watch waits on it without reading
# the store. Runs written by another process (the CLI, a detached resume) are not counted: the
# hub's slower fallback read sees those.
_WRITES_LOCK = threading.Lock()
write_generation = 0


def _note_write() -> None:
    global write_generation
    with _WRITES_LOCK:
        write_generation += 1


def stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ----- control flow raised by handlers ----------------------------------------------------


class RunStop(Exception):
    """Base of the signals a handler raises to stop a run in a non-failed state."""


class Cancelled(RunStop):
    """A cancellation was requested; the worker stops at this stage boundary."""


class BudgetPause(RunStop):
    """The next stage would exceed the budget: the run waits in ``paused_budget``."""

    def __init__(self, message: str, error: PrometheosError | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.error = error


class ExternalUncertain(RunStop):
    """An external call's outcome is unknown: ``unknown_external``, never repeated."""

    def __init__(self, message: str, error: PrometheosError | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.error = error


class LeaseLost(RunStop):
    """Another worker took the run over; this one stops without touching it."""


# ----- handlers ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fingerprint:
    """What resume compares: the input digests, the configuration (its canonical digest) and
    the model. Recomputed from the current state when a run is resumed."""

    inputs: tuple[tuple[str, str], ...]
    config: Any
    model: str | None = None

    def input_digests(self) -> list[dict[str, str]]:
        return [{"name": name, "sha256": sha} for name, sha in self.inputs]

    def config_digest(self) -> str:
        return canonical_sha256(self.config)


@dataclass
class Env:
    """Where a run lives: the application service, the project and its store."""

    service: Any
    project_id: str
    store: ProjectStore

    @property
    def home(self) -> Path:
        return Path(self.service.home)


class Handler:
    """One long operation. Subclasses set ``operation`` and ``version`` and implement
    ``fingerprint`` and ``execute``; the hooks are optional."""

    operation: str = ""
    version: str = ""
    # Executing a ``failed`` run directly retries it (CP6 transcription semantics). Other
    # operations are retried only through ``resume``, which checks compatibility first.
    retry_failed_on_execute: bool = False
    # An owner-approved external effect (an install, a provider call; decision 0017 round 2,
    # F3): a failed run is final, never retried by ``resume``; the effect needs a new approval.
    protected: bool = False

    def fingerprint(
        self,
        env: Env,
        params: dict[str, Any],
        caller: reads.Caller,
        run: op.OperationRun | None = None,
    ) -> Fingerprint:
        """Inputs, configuration and model, computed *at the caller's visibility* (the rank
        that starts or started the run: nothing above it is ever named; round 2, F1) from
        stable state: ``run`` is the stored run when resuming, so a handler can fingerprint
        what its completed stages fixed (round 2, F2)."""
        raise NotImplementedError

    def execute(self, ctx: RunContext) -> dict[str, Any]:
        raise NotImplementedError

    def finish_txn(self, ctx: RunContext, conn: Any, state: str) -> None:
        """Runs inside the transaction that records the final state."""

    def after_finish(self, env: Env, run: op.OperationRun, state: str) -> None:
        """Runs after that transaction committed (best-effort cleanup)."""

    def recover_uncertain(self, env: Env, run: op.OperationRun) -> None:
        """Called when recovery finds the run stopped during an external call."""

    def after_recover(self, env: Env, run: op.OperationRun) -> None:
        """Called, outside any transaction, for each run that crash recovery has just made
        truthful (queued, cancelled, completed or unknown): its worker is gone and so are the
        handles it held."""

    def local_outcome(self, env: Env, run: op.OperationRun) -> dict[str, Any] | None:
        """For an effect whose result is checkable on this computer (an install): the run's
        result when the effect verifiably completed before the worker stopped, else ``None``
        (decision 0021, CP10.3 review R2-L4). Recovery then completes the run instead of
        reporting an unknown outcome. Never repeats or starts anything."""
        return None

    def model_of(self, params: dict[str, Any], fingerprint: Fingerprint) -> str | None:
        return fingerprint.model


HANDLERS: dict[str, Handler] = {}


def register(handler: Handler) -> Handler:
    if not handler.operation or not handler.version:
        raise ValueError("a run handler names its operation and version")
    HANDLERS[handler.operation] = handler
    return handler


def handler_for(operation: str) -> Handler:
    _load_handlers()
    handler = HANDLERS.get(operation)
    if handler is None:
        raise PrometheosError(
            "unsupported",
            "this operation does not run as an operation run",
            {"operation": operation},
        )
    return handler


def load_handlers() -> None:
    """Register every long operation's handler (an idempotent import)."""
    _load_handlers()


def _load_handlers() -> None:
    from prometheos.app import run_handlers  # noqa: F401 - registers the handlers (cached import)


# ----- lease owners and process liveness ------------------------------------------------------


def owner_label(actor: str) -> str:
    """``host:pid:actor:token``: unique per execution; another process on this computer can
    tell from it that the worker's process has ended."""
    host = (socket.gethostname() or "host").replace(":", "-")[:60]
    return f"{host}:{os.getpid()}:{actor.replace(':', '-')[:40]}:{uuid.uuid4().hex[:8]}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return bool(
                ctypes.get_last_error() == 5
            )  # ERROR_ACCESS_DENIED: it exists  # type: ignore[attr-defined]
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return bool(code.value == 259)  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owner_gone(owner: str | None) -> bool:
    """True only when the lease owner was a process on this computer that has ended. Labels
    of another form, another host or this very process are never declared gone: their lease
    simply runs out."""
    if not owner:
        return True
    parts = owner.split(":")
    if len(parts) < 3 or not parts[1].isdigit():
        return False
    host = (socket.gethostname() or "host").replace(":", "-")[:60]
    pid = int(parts[1])
    if parts[0] != host or pid == os.getpid():
        return False
    return not _pid_alive(pid)


def _lease_live(run: op.OperationRun, now: datetime) -> bool:
    return bool(
        run.lease_owner
        and run.lease_expires_at
        and run.lease_expires_at >= stamp(now)
        and not owner_gone(run.lease_owner)
    )


# ----- rows ----------------------------------------------------------------------------------


def with_update(run: op.OperationRun, update: dict[str, Any]) -> op.OperationRun:
    """``run`` with ``update`` applied and validated again by the row contract (nested values
    may be plain JSON)."""
    document = run.model_dump(mode="json")
    document.update(_jsonable(update))
    return op.OperationRun.model_validate(document, strict=False)


def _save(conn: Any, run: op.OperationRun) -> None:
    """Write every column of ``run`` (validated by its row contract first)."""
    row = op.to_row(run)
    row.pop("id")
    names = ", ".join(f"{k} = ?" for k in row)
    conn.execute(
        f"UPDATE operation_runs SET {names} WHERE id = ?",  # noqa: S608 - fixed column names
        (*row.values(), run.id),
    )
    _note_write()


def _get(conn: Any, run_id: str) -> op.OperationRun:
    run = op.get_operation_run(conn, run_id)
    if run is None:
        raise PrometheosError("not_found", "operation run not found", {"run_id": run_id})
    return run


def _removed(run_id: str) -> PrometheosError:
    return PrometheosError("not_found", REMOVED_MESSAGE, {"run_id": run_id})


def _run_exists(env: Env, run_id: str) -> bool:
    """Whether the run's record is still there (a store that cannot be read answers "yes")."""
    try:
        conn = env.store.connect(readonly=True)
    except PrometheosError:
        return True
    try:
        return op.get_operation_run(conn, run_id) is not None
    finally:
        conn.close()


def normalise_id(run_id: str) -> str:
    """Accept a CP6 transcription run's bare UUID for the run it became (``oprun_<UUID>``)."""
    if is_request_id(run_id):
        return f"oprun_{run_id}"
    return run_id


def visible(conn: Any, run_id: str, rank: int) -> op.OperationRun:
    """A run started at a higher rank reads as absent (its parameters and results may name
    restricted records; decision 0010)."""
    run = op.get_operation_run(conn, normalise_id(run_id))
    if run is None or run.caller_rank > rank:
        raise PrometheosError("not_found", "operation run not found", {"run_id": run_id})
    return run


def stage_rows(conn: Any, run_id: str) -> list[op.OperationRunStage]:
    rows = conn.execute(
        "SELECT * FROM operation_run_stages WHERE run_id = ? ORDER BY idx", (run_id,)
    ).fetchall()
    return [op.from_row(op.OperationRunStage, r) for r in rows]


def stage_output(conn: Any, run_id: str, name: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT output FROM operation_run_stages WHERE run_id = ? AND name = ?", (run_id, name)
    ).fetchone()
    return None if row is None else dict(loads_internal(row[0]))


def charged_total(conn: Any, run_id: str) -> Decimal:
    total = Decimal(0)
    for (value,) in conn.execute(
        "SELECT charged FROM operation_run_stages WHERE run_id = ? AND charged IS NOT NULL",
        (run_id,),
    ):
        total += Decimal(value)
    return total


def money(value: Decimal) -> str:
    return str(value.quantize(SIX))


def _insert_stage(
    conn: Any, run_id: str, name: str, output: dict[str, Any], charged: str | None, at: str
) -> bool:
    """Insert a completed stage once; ``False`` when it was already recorded."""
    if conn.execute(
        "SELECT 1 FROM operation_run_stages WHERE run_id = ? AND name = ?", (run_id, name)
    ).fetchone():
        return False
    data = dumps_canonical(output)
    if len(data) > MAX_OUTPUT_BYTES:
        raise PrometheosError(
            "budget_exceeded", "a stage output is larger than the bound", {"stage": name}
        )
    nxt = conn.execute(
        "SELECT COALESCE(MAX(idx), -1) + 1 FROM operation_run_stages WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    op.insert(
        conn,
        "operation_run_stages",
        op.OperationRunStage(
            run_id=run_id,
            idx=int(nxt),
            name=name,
            output=loads_internal(data),
            charged=charged,
            completed_at=at,
        ),
    )
    return True


def _scrub_roots() -> tuple[tuple[str, str], ...]:
    """The local roots run text is labelled with, as the hub labels them: the PrometheOS
    home, the user's profile folder and the temporary folder."""
    pairs = [(str(Path.home()), "<user home>"), (tempfile.gettempdir(), "<temp>")]
    try:
        pairs.append((str(app_home()), "<PrometheOS home>"))
    except PrometheosError:  # no home could be determined: the other roots still apply
        pass
    return tuple(pairs)


@functools.lru_cache(maxsize=16)
def _free_text(roots: tuple[tuple[str, str], ...], posix: bool = True) -> redact.FreeText:
    # posix: MSYS, WSL and other absolute POSIX-style spellings too, outside API route text
    # (integration review INT-CP10-L1).
    return redact.FreeText(lambda: list(roots), posix=posix)


DIAGNOSTIC_KEYS = ("error", "limitations", "logs")
"""Run text that is diagnostic (decision 0021, integration review R2-L1): only here does the
POSIX-style alternative apply. Parameters and results are researcher data, where text such as
``/v1/ prefix`` or ``-o /dev/null`` must stay readable; known local roots, Windows and UNC paths
and path-like fields are still labelled there."""


def scrub(value: Any, *, diagnostic: bool = True) -> Any:
    """Absolute local paths in run text are replaced (decision 0013 I4; round 2, F7): run
    logs, errors and limitations are read by agents, and an exception's text often names a
    file under the user's home. The rule is the hub's free-text rule
    (:mod:`prometheos.domain.redact`; decision 0019, review R2-L1): the whole path is
    consumed, spaces included, and a known root keeps its label; MSYS, WSL and other absolute
    POSIX-style paths count too (not API route text) in diagnostic text (``diagnostic``: errors,
    logs, limitations; decision 0021). A field whose name is path-like (``archive``, ``path``,
    ``*_dir``, ...) never shows an absolute value (INT-CP10-M1). MCP reads runs without the
    hub's middleware, so this is the only guard there."""
    return _scrubbed(value, _free_text(_scrub_roots(), diagnostic))


def _scrubbed(value: Any, text: redact.FreeText, path: bool = False) -> Any:
    if isinstance(value, str):
        return text.path(value) if path else text.scrub(value)
    if isinstance(value, dict):
        return {k: _scrubbed(v, text, redact.is_path_key(str(k))) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrubbed(v, text, path) for v in value]
    return value


def _log(conn: Any, run_id: str, level: str, message: str, at: str | None = None) -> None:
    text = scrub(message.replace("\r", " "))
    op.append_run_log(conn, run_id, at or stamp(), level, text[:2000] or "-")


def _error_info(exc: PrometheosError) -> dict[str, Any]:
    info = exc.to_response().error
    return dict(scrub(info.model_dump(mode="json")))


# ----- creating runs -------------------------------------------------------------------------


def start_key(operation: str, params: dict[str, Any], rank: int) -> str:
    return rank_bound_sha256(
        canonical_sha256({"op": operation, "params_digest": canonical_sha256(params)}), rank
    )


def build(
    handler: Handler,
    caller: reads.Caller,
    params: dict[str, Any],
    fp: Fingerprint,
    *,
    request_id: str | None = None,
    key: str | None = None,
    budget: dict[str, str] | None = None,
    job: tuple[str, str] | None = None,
    waiting_on: dict[str, Any] | None = None,
    now: str | None = None,
) -> op.OperationRun:
    """A new run row (not yet written), validated by its row contract."""
    moment = now or stamp()
    inputs = fp.input_digests()
    return op.OperationRun.model_validate(
        {
            "id": new_operational_id("oprun"),
            "operation": handler.operation,
            "state": "waiting_for_input" if waiting_on else "queued",
            "caller_rank": caller.rank,
            "actor": caller.actor[:100],
            "start_request_id": request_id,
            "start_request_sha256": key,
            "params": params,
            "params_digest": canonical_sha256(params),
            "input_digests": inputs,
            "input_digest": canonical_sha256(inputs),
            "config_digest": fp.config_digest(),
            "workflow_version": handler.version,
            "model": handler.model_of(params, fp),
            "budget_reservation": budget,
            "external_effect": "none",
            "external_call": None,
            "waiting_on": waiting_on,
            "stage": None,
            "progress": None,
            "cancel_requested_at": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "attempts": 0,
            "error": None,
            "limitations": [],
            "artifacts": [],
            "result_seq": None,
            "job_id": job[0] if job else None,
            "job_due_utc": job[1] if job else None,
            "created_at": moment,
            "updated_at": moment,
            "finished_at": None,
        },
        strict=False,
    )


def create(
    env: Env,
    caller: reads.Caller,
    operation: str,
    params: dict[str, Any],
    *,
    request_id: str | None = None,
    budget: dict[str, str] | None = None,
    job: tuple[str, str] | None = None,
    waiting_on: dict[str, Any] | None = None,
    authorize: Callable[[Any], None] | None = None,
    fingerprint: Fingerprint | None = None,
    log: str | None = None,
) -> tuple[op.OperationRun, bool]:
    """Create a run (``queued``, or ``waiting_for_input`` with ``waiting_on``) and return it at
    once with ``True``; nothing runs here. With a ``request_id`` the start is idempotent: the
    same request at the same rank returns the stored run with ``False``; anything else under
    that ID is ``request_key_conflict``. ``authorize(conn)`` runs inside the creating
    transaction (for example spending an owner's capability once, with the run)."""
    handler = handler_for(operation)
    canonical: dict[str, Any] = dict(loads_internal(dumps_canonical(params)))
    if request_id is not None and not is_request_id(request_id):
        raise PrometheosError("validation_error", "request_id must be a canonical UUID4", {})
    key = start_key(operation, canonical, caller.rank) if request_id else None
    conn = env.store.connect()
    try:
        if request_id is not None:
            row = conn.execute(
                "SELECT * FROM operation_runs WHERE start_request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                existing = op.from_row(op.OperationRun, row)
                if existing.start_request_sha256 != key:
                    raise PrometheosError(
                        "request_key_conflict",
                        "request_id was already used for a different request",
                        {"request_id": request_id},
                    )
                return existing, False
    finally:
        conn.close()
    fp = fingerprint or handler.fingerprint(env, canonical, caller)
    now = stamp()
    run = build(
        handler, caller, canonical, fp, request_id=request_id, key=key, budget=budget, job=job,
        waiting_on=waiting_on, now=now,
    )  # fmt: skip
    conn = env.store.connect()
    try:
        with db.write_transaction(conn):
            if request_id is not None:
                row = conn.execute(
                    "SELECT * FROM operation_runs WHERE start_request_id = ?", (request_id,)
                ).fetchone()
                if row is not None:  # a concurrent identical start won the race
                    existing = op.from_row(op.OperationRun, row)
                    if existing.start_request_sha256 != key:
                        raise PrometheosError(
                            "request_key_conflict",
                            "request_id was already used for a different request",
                            {"request_id": request_id},
                        )
                    return existing, False
            if authorize is not None:
                authorize(conn)
            op.insert_operation_run(conn, run)
            _log(conn, run.id, "info", log or f"created by {caller.actor} ({operation})", now)
    finally:
        conn.close()
    _note_write()
    return run, True


# ----- the run context -----------------------------------------------------------------------


@dataclass
class RunContext:
    env: Env
    run: op.OperationRun
    owner: str
    clock: Callable[[], datetime]
    options: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    result_seq: int | None = None
    limitations: list[str] = field(default_factory=list)

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.run.params)

    @property
    def caller(self) -> reads.Caller:
        """The rank that started the run: every commit and read of the run is bound to it."""
        return reads.Caller(
            actor=self.run.actor, scope="owner" if self.run.caller_rank >= 2 else "agent"
        )

    @property
    def store(self) -> ProjectStore:
        return self.env.store

    @contextmanager
    def txn(self) -> Iterator[Any]:
        """A write transaction that holds only while this worker still owns the lease (and
        renews it). A lost lease raises :class:`LeaseLost`."""
        now = self.clock()
        conn = self.store.connect()
        try:
            with db.write_transaction(conn):
                row = conn.execute(
                    "SELECT state, lease_owner, cancel_requested_at FROM operation_runs "
                    "WHERE id = ?",
                    (self.run.id,),
                ).fetchone()
                if row is None or row["lease_owner"] != self.owner or row["state"] != "running":
                    raise LeaseLost(self.run.id)
                conn.execute(
                    "UPDATE operation_runs SET lease_expires_at = ?, heartbeat_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (
                        stamp(now + timedelta(seconds=LEASE_SECONDS)),
                        stamp(now),
                        stamp(now),
                        self.run.id,
                    ),
                )
                yield conn
        finally:
            conn.close()
        _note_write()  # committed: a waiting run watch may read it now

    def checkpoint(self, label: str | None = None) -> None:
        """A stage boundary: renew the lease, record the stage label and stop here (raise
        :class:`Cancelled`) when a cancellation was requested."""
        with self.txn() as conn:
            if label is not None:
                conn.execute(
                    "UPDATE operation_runs SET stage = ? WHERE id = ?", (label[:100], self.run.id)
                )
            requested = conn.execute(
                "SELECT cancel_requested_at FROM operation_runs WHERE id = ?", (self.run.id,)
            ).fetchone()[0]
        if requested is not None:
            raise Cancelled(self.run.id)
        fault_point(f"run_checkpoint:{label}")  # kill probes: stop here like a killed process

    def output(self, name: str) -> dict[str, Any] | None:
        conn = self.store.connect(readonly=True)
        try:
            return stage_output(conn, self.run.id, name)
        finally:
            conn.close()

    def complete(
        self,
        name: str,
        output: dict[str, Any] | None = None,
        charged: str | None = None,
        *,
        within: Callable[[Any], None] | None = None,
    ) -> None:
        """Record stage ``name`` as completed (once). ``within(conn)`` writes stage detail in
        the same transaction (for example a transcription chunk)."""
        with self.txn() as conn:
            if within is not None:
                within(conn)
            if _insert_stage(conn, self.run.id, name, output or {}, charged, stamp(self.clock())):
                conn.execute(
                    "UPDATE operation_runs SET stage = ? WHERE id = ?", (name[:100], self.run.id)
                )
        fault_point(f"run_after:{name}")  # kill probes: the stage is recorded, nothing after it

    def charge(self, amount: Decimal | str) -> str | None:
        """Check that a stage costing ``amount`` (in the reservation's unit) fits the run's
        budget; returns the amount to record with :meth:`complete`, or raises
        :class:`BudgetPause`. Without a reservation nothing is limited here."""
        value = Decimal(str(amount))
        conn = self.store.connect(readonly=True)
        try:
            run = _get(conn, self.run.id)
            spent = charged_total(conn, self.run.id)
        finally:
            conn.close()
        if run.budget_reservation is None:
            return money(value)
        limit = Decimal(run.budget_reservation.amount)
        if spent + value > limit:
            unit = run.budget_reservation.unit
            raise BudgetPause(
                f"the run's budget is used up ({money(spent)} of {money(limit)} {unit} charged; "
                f"the next stage needs {money(value)}); resume with a larger budget"
            )
        return money(value)

    def progress(self, done: int, total: int | None, unit: str) -> None:
        with self.txn() as conn:
            conn.execute(
                "UPDATE operation_runs SET progress = ? WHERE id = ?",
                (
                    dumps_canonical({"done": done, "total": total, "unit": unit}).decode(),
                    self.run.id,
                ),
            )

    def log(self, level: str, message: str) -> None:
        with self.txn() as conn:
            _log(conn, self.run.id, level, message, stamp(self.clock()))

    def external_started(
        self, provider: str, external_ref: str | None, request_sha256: str | None
    ) -> None:
        """Written and committed *before* the request is sent: if this worker dies now, the
        run reads as an uncertain external call, never as safe to send again."""
        call = {
            "provider": provider[:200],
            "external_ref": external_ref,
            "request_sha256": request_sha256,
            "started_at": stamp(self.clock()),
        }
        op.ExternalCall.model_validate(call)
        with self.txn() as conn:
            conn.execute(
                "UPDATE operation_runs SET external_effect = 'started', external_call = ? "
                "WHERE id = ?",
                (dumps_canonical(call).decode(), self.run.id),
            )
            _log(conn, self.run.id, "info", f"external call started ({provider})")

    def external_confirmed(self, note: str = "external call outcome known") -> None:
        with self.txn() as conn:
            conn.execute(
                "UPDATE operation_runs SET external_effect = 'confirmed' WHERE id = ? "
                "AND external_effect = 'started'",
                (self.run.id,),
            )
            _log(conn, self.run.id, "info", note)

    def artifact(self, record_id: str, rev: int, role: str) -> None:
        entry = {"id": record_id, "rev": rev, "role": role}
        if entry not in self.artifacts:
            self.artifacts.append(entry)


# ----- execution -----------------------------------------------------------------------------


_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()


@contextmanager
def single_flight(run_id: str) -> Iterator[None]:
    """One execution of a run per process at a time (a duplicate is ``busy``)."""
    with _RUNNING_LOCK:
        if run_id in _RUNNING:
            raise PrometheosError(
                "busy", "this run is already running in this process", {"run_id": run_id}
            )
        _RUNNING.add(run_id)
    try:
        yield
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(run_id)


class _Heartbeat(threading.Thread):
    """Renews the lease while a long stage runs (an engine, a download), so a live worker is
    never mistaken for a dead one. Busy writes are skipped; a heartbeat never touches the
    project sequence."""

    def __init__(self, store: ProjectStore, run_id: str, owner: str) -> None:
        super().__init__(daemon=True, name=f"heartbeat-{run_id[:14]}")
        self.store = store
        self.run_id = run_id
        self.owner = owner
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.wait(HEARTBEAT_SECONDS):
            heartbeat(self.store, self.run_id, self.owner)

    def stop(self) -> None:
        self.stopped.set()


def heartbeat(store: ProjectStore, run_id: str, owner: str, now: datetime | None = None) -> bool:
    """Renew a held lease; ``False`` when it is not held (or the store was busy)."""
    moment = now or datetime.now(UTC)
    try:
        conn = store.connect()
    except PrometheosError:
        return False
    try:
        with db.write_transaction(conn):
            cur = conn.execute(
                "UPDATE operation_runs SET heartbeat_at = ?, lease_expires_at = ? "
                "WHERE id = ? AND lease_owner = ? AND state = 'running'",
                (stamp(moment), stamp(moment + timedelta(seconds=LEASE_SECONDS)), run_id, owner),
            )
            return bool(cur.rowcount == 1)
    except PrometheosError:
        return False
    finally:
        conn.close()


def _acquire(env: Env, handler: Handler, run_id: str, owner: str, now: datetime) -> op.OperationRun:
    conn = env.store.connect()
    try:
        with db.write_transaction(conn):
            run = _get(conn, run_id)
            state = run.state
            if state in ("completed", "cancelled"):
                raise PrometheosError(
                    "validation_error", f"the run is already {state}", {"run_id": run_id}
                )
            if state == "unknown_external":
                raise PrometheosError(
                    "validation_error",
                    "an external call of this run has an unknown outcome; it is never repeated "
                    "automatically: acknowledge it (run acknowledge) and start a new run",
                    {"run_id": run_id, "state": state},
                )
            if state == "waiting_for_input":
                raise PrometheosError(
                    "validation_error",
                    "the run is waiting for input or an approval",
                    {"run_id": run_id, "waiting_on": run.model_dump(mode="json")["waiting_on"]},
                )
            if state == "paused_budget":
                raise PrometheosError(
                    "budget_exceeded",
                    "the run is paused at its budget; resume it with a larger budget",
                    {"run_id": run_id},
                )
            if state == "failed" and not handler.retry_failed_on_execute:
                raise PrometheosError(
                    "validation_error",
                    "the run failed; resume it (run resume) to retry",
                    {"run_id": run_id},
                )
            if state == "running" and _lease_live(run, now) and run.lease_owner != owner:
                raise PrometheosError(
                    "busy", "another worker is executing this run", {"run_id": run_id}
                )
            if run.cancel_requested_at is not None and state != "running":
                raise PrometheosError(
                    "validation_error", "a cancellation of this run is pending", {}
                )
            acquired = with_update(
                run,
                {
                    "state": "running",
                    "lease_owner": owner,
                    "lease_expires_at": stamp(now + timedelta(seconds=LEASE_SECONDS)),
                    "heartbeat_at": stamp(now),
                    "attempts": run.attempts + 1,
                    "error": None,
                    "finished_at": None,
                    "updated_at": stamp(now),
                },
            )
            _save(conn, acquired)
            _log(conn, run_id, "info", f"worker started (attempt {acquired.attempts})")
            return acquired
    finally:
        conn.close()


def _finish(
    ctx: RunContext,
    handler: Handler,
    state: str,
    *,
    result: dict[str, Any] | None = None,
    error: PrometheosError | None = None,
    message: str | None = None,
    level: str = "info",
) -> op.OperationRun | None:
    """Record the final state of this execution, only while the lease is still ours."""
    now = ctx.clock()
    conn = ctx.store.connect()
    try:
        with db.write_transaction(conn):
            found = op.get_operation_run(conn, ctx.run.id)
            if found is None:
                return None  # the run's record is gone (a purge removed it): nothing to record
            run = found
            if run.lease_owner != ctx.owner or run.state != "running":
                return None  # another worker took over, or a recovery already decided
            update: dict[str, Any] = {
                "state": state,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": stamp(now),
                "finished_at": stamp(now) if state in op.TERMINAL_RUN_STATES else None,
                "limitations": (list(run.limitations) + ctx.limitations)[:50],
            }
            if state == "completed":
                assert result is not None
                if _insert_stage(conn, run.id, "result", result, None, stamp(now)) is False:
                    pass  # a replayed completion keeps the first result
                update["artifacts"] = ctx.artifacts[:200]
                update["result_seq"] = ctx.result_seq
                update["error"] = None
                update["stage"] = "completed"
                if run.external_effect == "started":
                    update["external_effect"] = "confirmed"
            elif state == "failed":
                assert error is not None
                update["error"] = _error_info(error)
            elif state == "unknown_external":
                update["external_effect"] = "uncertain"
                if run.external_call is None:  # pragma: no cover - handlers mark first
                    update["external_call"] = {
                        "provider": run.operation,
                        "external_ref": None,
                        "request_sha256": None,
                        "started_at": stamp(now),
                    }
                update["limitations"] = [
                    *update["limitations"],
                    "an external call's outcome is unknown; it is not repeated "
                    "automatically: acknowledge it to close the run",
                ][:50]
            elif state == "cancelled" and run.external_effect == "started":  # pragma: no cover
                update["external_effect"] = "uncertain"
            final = with_update(run, update)
            handler.finish_txn(ctx, conn, state)
            _save(conn, final)
            if state == "unknown_external":
                _outcome_unknown_event(conn, final)
            if message:
                _log(conn, run.id, level, message, stamp(now))
    finally:
        conn.close()
    handler.after_finish(ctx.env, final, state)
    return final


def _outcome_unknown_event(conn: Any, run: op.OperationRun) -> None:
    """``run.outcome_unknown`` in the transaction that made the run ``unknown_external``
    (decision 0035): the outcome is reported, never retried."""
    # A handler's stage label ("call 1") in the event's closed shape ("call_1").
    stage = re.sub(r"[^a-z0-9_:.-]", "_", (run.stage or "start").lower())[:100]
    outbox.emit(
        conn,
        "run.outcome_unknown",
        {"kind": "operation_run", "id": run.id},
        {"operation": run.operation, "stage": stage if stage[:1].isalpha() else f"s{stage}"[:100]},
        occurred_at=run.updated_at,
        project_seq=outbox.current_seq(conn),
    )


def _fail_or_uncertain(
    ctx: RunContext, handler: Handler, error: PrometheosError, message: str
) -> op.OperationRun | None:
    """A failure while an external call is marked ``started`` (sent, outcome not recorded as
    known) is not a known failure: the call may have been received or billed. The run becomes
    ``unknown_external`` (never retried; round 2, F4) and the handler marks its own records
    (the provider ledger) uncertain too. Handlers that know the outcome confirm it first."""
    conn = ctx.store.connect(readonly=True)
    try:
        row = op.get_operation_run(conn, ctx.run.id)
        effect = row.external_effect if row is not None else None
    finally:
        conn.close()
    if effect != "started":
        return _finish(ctx, handler, "failed", error=error, message=message, level="error")
    final = _finish(
        ctx, handler, "unknown_external",
        message=f"{message}; an external call had been sent and its outcome is unknown",
        level="warning",
    )  # fmt: skip
    if final is not None:
        try:
            handler.recover_uncertain(ctx.env, final)
        except PrometheosError:
            pass  # the run already says the outcome is unknown
    return final


def execute(
    env: Env,
    run_id: str,
    *,
    actor: str = "worker",
    owner: str | None = None,
    options: dict[str, Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    raise_errors: bool = False,
) -> op.OperationRun:
    """Execute (or continue) a run in this thread until it completes, fails, is cancelled,
    pauses at its budget or meets an uncertain external call. Returns the final row.

    With ``raise_errors`` a failure (and a budget pause or uncertain call carrying the
    operation's error) is raised after it was recorded: synchronous callers keep the error
    envelope the operation always had."""
    run_id = normalise_id(run_id)
    clock = clock or (lambda: datetime.now(UTC))
    owner = owner or owner_label(actor)
    conn = env.store.connect(readonly=True)
    try:
        operation = _get(conn, run_id).operation
    finally:
        conn.close()
    handler = handler_for(operation)
    with single_flight(run_id):
        run = _acquire(env, handler, run_id, owner, clock())
        ctx = RunContext(env=env, run=run, owner=owner, clock=clock, options=dict(options or {}))
        beat = _Heartbeat(env.store, run_id, owner)
        beat.start()
        raised: PrometheosError | None = None
        try:
            try:
                result = handler.execute(ctx)
            except Cancelled:
                final = _finish(
                    ctx, handler, "cancelled", message="cancellation confirmed by the worker"
                )
            except BudgetPause as pause:
                final = _finish(ctx, handler, "paused_budget", message=pause.message)
                raised = pause.error
            except ExternalUncertain as uncertain:
                final = _finish(
                    ctx, handler, "unknown_external", message=uncertain.message, level="warning"
                )
                raised = uncertain.error
            except LeaseLost:
                final = None
                raised = PrometheosError(
                    "busy", "this worker no longer holds the run's lease", {"run_id": run_id}
                )
            except PrometheosError as exc:
                final = _fail_or_uncertain(ctx, handler, exc, f"failed: {exc.message}")
                raised = exc
            except Exception as exc:
                # The exception's own text may carry local paths or data: only its type is
                # kept (round 2, F7).
                wrapped = PrometheosError(
                    "io_error",
                    f"the run failed ({type(exc).__name__}); resume it to retry",
                    {"run_id": run_id, "error": type(exc).__name__},
                )
                final = _fail_or_uncertain(ctx, handler, wrapped, f"failed: {type(exc).__name__}")
                raised = wrapped
            else:
                final = _finish(ctx, handler, "completed", result=result, message="completed")
            if final is None and not _run_exists(env, run_id):
                # Not a takeover and not the worker's own failure: the run's record is gone. A
                # purge of the records it worked on removes it, whatever the worker was doing
                # (a lease check, a stage write, the transcript commit) when it found out.
                raised = _removed(run_id)
        finally:
            beat.stop()
    if raise_errors and raised is not None:
        # A waiting caller can find its run. Not for typed details, nor for policy_denied,
        # whose details deliberately name nothing (decision 0014 R1-L1).
        if raised.code not in ("approval_required", "capability_unavailable", "policy_denied"):
            raised.details.setdefault("run_id", run_id)
        raise raised
    if final is None:
        conn = env.store.connect(readonly=True)
        try:
            final = op.get_operation_run(conn, run_id)
        finally:
            conn.close()
        if final is None:
            raise _removed(run_id)
    return final


# Background runs in one process are bounded (round 2, F6): at most GLOBAL_LIMIT at once, and
# per family at most its limit (one compile, one transcription, one install, one provider call,
# two imports). The rest stay ``queued`` (truthfully: waiting for a worker) and start in order
# as slots free up. If the process ends first, they stay queued for ``run resume``.
GLOBAL_LIMIT = 4
FAMILIES: dict[str, tuple[str, int]] = {
    "paper_compile": ("compile", 1),
    "audio_transcribe": ("transcribe", 1),
    "tex_engine_install": ("install", 1),
    "audio_models_install": ("install", 1),
    "provider_run": ("provider", 1),
    "source_import_plan": ("import", 2),
    "source_import_commit": ("import", 2),
    "audio_import": ("import", 2),
    "library_import_plan": ("import", 2),
    "library_import_commit": ("import", 2),
    "zotero_import_plan": ("import", 2),
    "zotero_import_commit": ("import", 2),
    "matrix_extract": ("extract", 1),
}


@dataclass
class _Pending:
    env: Env
    run_id: str
    actor: str
    family: str


class _Dispatcher:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active: dict[str, int] = {}
        self.total = 0
        self.pending: list[_Pending] = []
        self.known: set[str] = set()
        # A submission that arrived while the same run was still executing here (a resume in
        # the slot-release window; CP10.3 review R2-L2): rechecked when that execution ends.
        self.again: dict[str, _Pending] = {}

    def _limit(self, family: str) -> int:
        for name, limit in FAMILIES.values():
            if name == family:
                return limit
        return 1

    def _startable(self, item: _Pending) -> bool:
        return self.total < GLOBAL_LIMIT and self.active.get(item.family, 0) < self._limit(
            item.family
        )

    def submit(self, item: _Pending) -> None:
        with self.lock:
            if item.run_id in self.known:
                if item not in self.pending:
                    self.again[item.run_id] = item  # recheck when the current execution ends
                return  # already running or waiting here
            self.known.add(item.run_id)
            self.pending.append(item)
            ready = self._take()
        for chosen in ready:
            self._start(chosen)

    def _take(self) -> list[_Pending]:
        """Under the lock: the waiting items that may start now, in order."""
        ready: list[_Pending] = []
        for item in list(self.pending):
            if self._startable(item):
                self.pending.remove(item)
                self.active[item.family] = self.active.get(item.family, 0) + 1
                self.total += 1
                ready.append(item)
        return ready

    def _start(self, item: _Pending) -> None:
        def body() -> None:
            try:
                execute(item.env, item.run_id, actor=item.actor)
            except PrometheosError:
                pass  # busy (another worker), already finished: the row says what happened
            finally:
                with self.lock:
                    self.active[item.family] -= 1
                    self.total -= 1
                    self.known.discard(item.run_id)
                    again = self.again.pop(item.run_id, None)
                    ready = self._take()
                for chosen in ready:
                    self._start(chosen)
                if again is not None and _still_queued(again):
                    self.submit(again)  # the resume re-queued it: execute it again here

        threading.Thread(target=body, daemon=True, name=f"run-{item.run_id[:14]}").start()


def _still_queued(item: _Pending) -> bool:
    try:
        conn = item.env.store.connect(readonly=True)
        try:
            return _get(conn, item.run_id).state == "queued"
        finally:
            conn.close()
    except PrometheosError:
        return False


DISPATCHER = _Dispatcher()


def execute_in_background(env: Env, run_id: str, *, actor: str) -> None:
    """Execute in a daemon thread of this long-lived process (hub, MCP server), within the
    process's bounds (see ``FAMILIES``). Whatever happens is recorded in the run row; nothing
    is raised."""
    conn = env.store.connect(readonly=True)
    try:
        operation = _get(conn, normalise_id(run_id)).operation
    finally:
        conn.close()
    family = FAMILIES.get(operation, (operation, 1))[0]
    DISPATCHER.submit(_Pending(env, normalise_id(run_id), actor, family))


def spawn_worker(home: Path, project_id: str, run_id: str, extra: list[str] | None = None) -> int:
    """A detached ``prometheos run resume`` process that executes a queued run after the CLI
    command that created it has returned (``--no-wait``). ``extra`` passes the caller's scope
    options, so the worker resumes the run at the rank that started it."""
    argv = [sys.executable, *child_flags(), "-m", "prometheos", "run", "resume", run_id,
            "--project", project_id, "--json", *(extra or [])]  # fmt: skip
    environment = dict(os.environ, PROMETHEOS_HOME=str(home))
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": environment,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, **kwargs)
    return int(process.pid)


def wait(env: Env, run_id: str, seconds: float) -> op.OperationRun:
    """Wait at most ``seconds`` for a run to leave queued/running; returns the row."""
    deadline = time.monotonic() + max(0.0, min(float(seconds), MAX_WAIT_SECONDS))
    delay = 0.02
    while True:
        conn = env.store.connect(readonly=True)
        try:
            run = _get(conn, normalise_id(run_id))
        finally:
            conn.close()
        if run.state not in ACTIVE_STATES or time.monotonic() >= deadline:
            return run
        time.sleep(delay)
        delay = min(delay * 1.5, 0.25)


# ----- recovery ------------------------------------------------------------------------------


def recover(env: Env, now: datetime | None = None) -> list[str]:
    """A recovery pass. It starts by sweeping the project's pending-deletion list (decision
    0031): what a purge listed for a worker that has died since goes now, although the purge
    removed that worker's run and recovery has nothing left to see of it. Then
    :func:`_recover`."""
    pending.sweep(env.store.directory, env.store)
    return _recover(env, now)


def _recover(env: Env, now: datetime | None = None) -> list[str]:
    """Make interrupted runs truthful (never runs anything). Returns the IDs it changed.

    A ``running`` run whose worker is gone (lease expired, or its process on this computer
    ended) becomes ``unknown_external`` if an external call had started, ``cancelled`` if a
    cancellation was requested, and otherwise ``queued`` for resume. A run waiting on an
    approval that ended (denied, withdrawn, expired, revoked) is ``cancelled``."""
    moment = now or datetime.now(UTC)
    changed: list[op.OperationRun] = []
    uncertain: list[op.OperationRun] = []
    try:
        conn = env.store.connect()
    except PrometheosError:
        return []
    try:
        with db.write_transaction(conn):
            rows = conn.execute(
                "SELECT * FROM operation_runs WHERE state IN ('running', 'waiting_for_input')"
            ).fetchall()
            for row in rows:
                run = op.from_row(op.OperationRun, row)
                if run.state == "running":
                    if _lease_live(run, moment):
                        continue
                    stage = run.stage or "start"
                    base: dict[str, Any] = {
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "updated_at": stamp(moment),
                    }
                    outcome = _local_outcome(env, run) if run.external_effect == "started" else None
                    if outcome is not None:
                        update = base | {
                            "state": "completed",
                            "external_effect": "confirmed",
                            "stage": "completed",
                            "error": None,
                            "finished_at": stamp(moment),
                            "limitations": [
                                *run.limitations,
                                "the worker stopped after the effect completed; recovery "
                                "verified its result on this computer (nothing was repeated)",
                            ][:50],
                        }
                        _insert_stage(conn, run.id, "result", outcome, None, stamp(moment))
                        message = "the worker stopped after the effect; verified as completed"
                        level = "info"
                    elif run.external_effect == "started":
                        call = run.external_call
                        provider = call.provider if call else "external"
                        update = base | {
                            "state": "unknown_external",
                            "external_effect": "uncertain",
                            "limitations": [
                                *run.limitations,
                                f"the worker stopped during an external call ({provider}) "
                                f"in stage {stage}; its outcome is unknown and it is not "
                                "repeated automatically",
                            ][:50],
                        }
                        message = f"worker stopped during an external call ({provider})"
                        level = "warning"
                    elif run.cancel_requested_at is not None:
                        update = base | {"state": "cancelled", "finished_at": stamp(moment)}
                        message = "cancellation confirmed: the worker had stopped"
                        level = "info"
                    else:
                        update = base | {"state": "queued"}
                        message = f"interrupted during {stage} (the worker stopped); resume"
                        level = "warning"
                    fixed = with_update(run, update)
                    _save(conn, fixed)
                    _log(conn, run.id, level, message, stamp(moment))
                    changed.append(fixed)
                    if fixed.state == "unknown_external":
                        uncertain.append(fixed)
                        _outcome_unknown_event(conn, fixed)
                elif run.waiting_on is not None and run.waiting_on.kind == "approval":
                    approval = conn.execute(
                        "SELECT state, expires_at FROM approval_requests WHERE id = ?",
                        (run.waiting_on.approval_request_id,),
                    ).fetchone()
                    lapsed = (
                        approval is not None
                        and approval["state"] == "pending"
                        and approval["expires_at"] < stamp(moment)
                    )
                    ended = (
                        approval is None
                        or approval["state"] in ("denied", "withdrawn", "expired", "revoked")
                        or lapsed
                    )
                    # Used, but this run never took it over (a crash between the grant's use
                    # and the take-over, or the grant ran another run): this run never ran
                    # under it, and it cannot (round 2, F13).
                    used_elsewhere = approval is not None and approval["state"] == "used"
                    if ended or used_elsewhere:
                        why = approval["state"] if approval is not None else "missing"
                        if approval is not None and approval["state"] == "pending":
                            why = "expired"
                        fixed = with_update(
                            run,
                            {
                                "state": "cancelled",
                                "waiting_on": None,
                                "finished_at": stamp(moment),
                                "updated_at": stamp(moment),
                                "limitations": [
                                    *run.limitations,
                                    (
                                        "the approval was used without this run starting; "
                                        "nothing ran in this run"
                                        if used_elsewhere
                                        else f"the approval request ended ({why}); nothing ran"
                                    ),
                                ][:50],
                            },
                        )
                        _save(conn, fixed)
                        _log(conn, run.id, "info", f"approval {why}: the run never started")
                        changed.append(fixed)
    except PrometheosError:
        return []  # busy: the next reader recovers
    finally:
        conn.close()
    for run in uncertain:
        try:
            handler_for(run.operation).recover_uncertain(env, run)
        except PrometheosError:
            pass
    for run in changed:
        try:
            handler_for(run.operation).after_recover(env, run)
        except (PrometheosError, OSError):
            pass  # best-effort cleanup; the next run or recovery tries again
    return [r.id for r in changed]


# ----- control: resume, cancel, acknowledge --------------------------------------------------


def _local_outcome(env: Env, run: op.OperationRun) -> dict[str, Any] | None:
    try:
        return handler_for(run.operation).local_outcome(env, run)
    except (PrometheosError, OSError, ValueError):
        return None  # cannot verify: the outcome stays unknown


def run_caller(run: op.OperationRun) -> reads.Caller:
    """The rank that started ``run`` (its fingerprint and commits are bound to it)."""
    return reads.Caller(actor=run.actor, scope="owner" if run.caller_rank >= 2 else "agent")


def resume_refusal(handler: Handler, run: op.OperationRun) -> str | None:
    """Why a run can never be resumed (``None`` when it can be, compatibility aside)."""
    if run.state in ("completed", "cancelled"):
        return f"the run is already {run.state}"
    if run.external_effect == "uncertain":
        return (
            "an external call of this run has an unknown outcome; it is never repeated: "
            "acknowledge it (if not yet done) and start a new run"
        )
    if handler.protected and run.state == "failed":
        return (
            "this run carried out an owner-approved external effect and failed; it is not "
            "repeated under the same approval: ask for a new approval and start a new run"
        )
    if run.cancel_requested_at is not None:
        return "a cancellation of this run is pending"
    return None


def check_compatible(env: Env, run: op.OperationRun) -> None:
    """Resume continues only with identical inputs, parameters, configuration, model and
    workflow version (spec/04); anything else needs a new run."""
    handler = handler_for(run.operation)
    changed: list[str] = []
    if canonical_sha256(run.params) != run.params_digest:  # pragma: no cover - model-enforced
        changed.append("params_digest")
    fp = handler.fingerprint(env, dict(run.params), run_caller(run), run)
    inputs = fp.input_digests()
    if canonical_sha256(inputs) != run.input_digest:
        changed.append("input_digest")
    if fp.config_digest() != run.config_digest:
        changed.append("config_digest")
    if handler.model_of(dict(run.params), fp) != run.model:
        changed.append("model")
    if handler.version != run.workflow_version:
        changed.append("workflow_version")
    if changed:
        raise PrometheosError(
            "stale_task",
            "this run cannot resume: "
            + ", ".join(changed)
            + " changed since it started; start a new run",
            {"run_id": run.id, "changed": changed},
        )


def resume(
    env: Env, caller: reads.Caller, run_id: str, *, budget_amount: str | None = None
) -> op.OperationRun:
    """Re-queue a queued, failed or budget-paused run after the compatibility check (and
    optionally a larger budget reservation in the same unit). The caller then executes it."""
    recover(env)
    conn = env.store.connect()
    try:
        with db.write_transaction(conn):
            run = visible(conn, run_id, caller.rank)
            handler = handler_for(run.operation)
            refusal = resume_refusal(handler, run) if run.state != "running" else None
            if refusal is not None:
                raise PrometheosError(
                    "validation_error",
                    refusal,
                    {"run_id": run.id, "state": run.state, "external_effect": run.external_effect},
                )
            if run.state == "running":
                raise PrometheosError(
                    "busy", "the run is executing now", {"run_id": run.id, "state": run.state}
                )
            if run.state == "waiting_for_input":
                raise PrometheosError(
                    "validation_error",
                    "the run is waiting for input or an approval",
                    {"run_id": run.id, "state": run.state},
                )
            check_compatible(env, run)
            update: dict[str, Any] = {
                "state": "queued",
                "error": None,
                "finished_at": None,
                "updated_at": stamp(),
            }
            if budget_amount is not None:
                reservation = run.budget_reservation
                if reservation is None:
                    raise PrometheosError(
                        "validation_error", "this run has no budget reservation to raise", {}
                    )
                try:
                    new = Decimal(budget_amount)
                except InvalidOperation as exc:
                    raise PrometheosError(
                        "validation_error", "budget must be a number", {}
                    ) from exc
                if new < Decimal(reservation.amount):
                    raise PrometheosError(
                        "validation_error", "a resumed budget cannot be lower than before", {}
                    )
                update["budget_reservation"] = {"unit": reservation.unit, "amount": money(new)}
                _log(
                    conn, run.id, "info",
                    f"budget raised from {reservation.amount} to {money(new)} {reservation.unit}",
                )  # fmt: skip
            queued = with_update(run, update)
            _save(conn, queued)
            _log(conn, run.id, "info", f"resumed by {caller.actor} (was {run.state})")
            return queued
    finally:
        conn.close()


def cancel(env: Env, caller: reads.Caller, run_id: str) -> dict[str, Any]:
    """Cancel: a run that nothing executes is cancelled at once (``cancelled``); an executing
    one gets ``cancel_requested_at`` and stops at its next stage boundary (``cancel_requested``:
    a request, not a confirmed stop)."""
    recover(env)
    now = stamp()
    withdraw: str | None = None
    conn = env.store.connect()
    try:
        with db.write_transaction(conn):
            run = visible(conn, run_id, caller.rank)
            if run.state in ("completed", "cancelled"):
                raise PrometheosError(
                    "validation_error", f"the run is already {run.state}", {"run_id": run.id}
                )
            if run.state == "unknown_external":
                raise PrometheosError(
                    "validation_error",
                    "this run ended with an uncertain external call: acknowledge it instead",
                    {"run_id": run.id, "state": run.state},
                )
            if run.state == "running":
                if run.cancel_requested_at is None:
                    _save(conn, with_update(run, {"cancel_requested_at": now, "updated_at": now}))
                    _log(conn, run.id, "info", f"cancellation requested by {caller.actor}")
                outcome = "cancel_requested"
            else:
                if run.waiting_on is not None and run.waiting_on.kind == "approval":
                    withdraw = run.waiting_on.approval_request_id
                _save(
                    conn,
                    with_update(
                        run,
                        {
                            "state": "cancelled",
                            "waiting_on": None,
                            "cancel_requested_at": run.cancel_requested_at or now,
                            "finished_at": now,
                            "updated_at": now,
                        },
                    ),
                )
                handler = handler_for(run.operation)
                ctx = RunContext(env=env, run=run, owner="", clock=lambda: datetime.now(UTC))
                handler.finish_txn(ctx, conn, "cancelled")
                _log(conn, run.id, "info", f"cancelled by {caller.actor} (nothing was executing)")
                outcome = "cancelled"
            final = _get(conn, run.id)
    finally:
        conn.close()
    if outcome == "cancelled":
        handler_for(final.operation).after_finish(env, final, "cancelled")
    if withdraw is not None:
        try:
            from prometheos.app import approvals

            approvals.withdraw(env.store, withdraw, caller.rank)
        except PrometheosError:
            pass  # already decided or ended: the approval keeps its own state
    return {"run_id": final.id, "outcome": outcome, "state": final.state}


def acknowledge(
    env: Env, caller: reads.Caller, run_id: str, note: str | None = None
) -> op.OperationRun:
    """Close an ``unknown_external`` run: it becomes ``failed`` with the uncertainty kept in
    its error and limitations. Nothing is sent again; a new run is a new, explicit choice."""
    recover(env)
    now = stamp()
    conn = env.store.connect()
    try:
        with db.write_transaction(conn):
            run = visible(conn, run_id, caller.rank)
            if run.state != "unknown_external":
                raise PrometheosError(
                    "validation_error",
                    "only a run with an uncertain external call is acknowledged",
                    {"run_id": run.id, "state": run.state},
                )
            text = (note or "").strip()[:500]
            message = (
                "the outcome of an external call is unknown; acknowledged by "
                f"{caller.actor} and not repeated" + (f": {text}" if text else "")
            )
            final = with_update(
                run,
                {
                    "state": "failed",
                    "error": {
                        "code": "source_unavailable",
                        "message": message[:4096],
                        "retryable": "source_unavailable" in RETRYABLE,
                        "details": {"run_id": run.id, "acknowledged": True},
                    },
                    "finished_at": now,
                    "updated_at": now,
                    "limitations": [*run.limitations, message[:2000]][:50],
                },
            )
            _save(conn, final)
            _log(conn, run.id, "warning", message)
            return final
    finally:
        conn.close()


# ----- reads ---------------------------------------------------------------------------------


def view(conn: Any, run: op.OperationRun) -> dict[str, Any]:
    """The run view every transport shows (contract ``operation_run_view``)."""
    stages = stage_rows(conn, run.id)
    logs = op.run_logs(conn, run.id)[-VIEW_LINES:]
    result = None
    if run.state == "completed":
        result = next((s.output for s in stages if s.name == "result"), None)
    listed = [s for s in stages if s.name != "result"]
    document = {
        "run_id": run.id,
        "operation": run.operation,
        "state": run.state,
        "stage": run.stage,
        "progress": run.progress,
        "actor": run.actor,
        "caller_rank": run.caller_rank,
        "params": run.params,
        "params_digest": run.params_digest,
        "input_digests": run.input_digests,
        "input_digest": run.input_digest,
        "config_digest": run.config_digest,
        "workflow_version": run.workflow_version,
        "model": run.model,
        "budget": {
            "reservation": run.budget_reservation,
            "charged": money(charged_total(conn, run.id)),
        },
        "external_effect": run.external_effect,
        "external_call": run.external_call,
        "waiting_on": run.waiting_on,
        "cancel_requested_at": run.cancel_requested_at,
        "cancel_confirmed": run.state == "cancelled",
        "heartbeat_at": run.heartbeat_at,
        "lease_expires_at": run.lease_expires_at,
        "attempts": run.attempts,
        "error": run.error,
        "limitations": run.limitations,
        "artifacts": run.artifacts,
        "result_seq": run.result_seq,
        "result": result,
        "resumable": run.state in RESUMABLE_STATES
        and resume_refusal(handler_for(run.operation), run) is None,
        "job_id": run.job_id,
        "job_due_utc": run.job_due_utc,
        "stages_completed": len(listed),
        "stages": [
            {"name": s.name, "completed_at": s.completed_at, "charged": s.charged}
            for s in listed[-VIEW_LINES:]
        ],
        "logs": [
            {"idx": line.idx, "at": line.at, "level": line.level, "message": line.message}
            for line in logs
        ],
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
    }
    checked = op.RunView.model_validate(
        loads_internal(dumps_canonical(_jsonable(document))), strict=False
    )
    return dict(checked.model_dump(mode="json"))


def redact_view(conn: Any, caller: reads.Caller, document: dict[str, Any]) -> dict[str, Any]:
    """The run view a caller is shown. For every rank, owner included, local paths in the
    parameters, result, error, limitations and logs are labelled (:func:`scrub`; integration
    review INT-CP10-M1: an owner install from ``--archive`` keeps the archive's absolute path
    in the stored parameters, which resume needs, but no view shows it). An agent-scope view
    also drops artifacts and job links that name a record the caller may not see (now) and
    replaces record IDs inside parameters, results, logs and errors by the neutral
    placeholder, like every other agent-scope output."""
    if caller.scope == "owner":
        labelled = dict(document)
        for key in ("params", "result", "error", "limitations", "logs"):
            labelled[key] = scrub(document[key], diagnostic=key in DIAGNOSTIC_KEYS)
        return labelled
    out = dict(document)

    def shown(record_id: str) -> bool:
        row = conn.execute("SELECT sensitivity FROM records WHERE id = ?", (record_id,)).fetchone()
        return row is None or caller.may_see(row[0])

    out["artifacts"] = [a for a in document["artifacts"] if shown(a["id"])]
    if document["job_id"] is not None and not shown(document["job_id"]):
        out["job_id"] = None
        out["job_due_utc"] = None
    for key in ("params", "result", "error", "limitations", "logs"):
        hidden = reads.redact_hidden(conn, caller, document[key])
        out[key] = scrub(hidden, diagnostic=key in DIAGNOSTIC_KEYS)
    return out


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def listing(
    conn: Any,
    rank: int,
    *,
    state: str | None = None,
    operation: str | None = None,
    limit: int = 50,
) -> list[op.OperationRun]:
    """Runs the caller may see, newest first."""
    if state is not None and state not in op.RUN_STATES:
        raise PrometheosError("validation_error", "unknown run state", {"state": state})
    if not 1 <= limit <= 200:
        raise PrometheosError("validation_error", "limit must be 1..200", {})
    rows = conn.execute(
        "SELECT * FROM operation_runs WHERE caller_rank <= ? AND (? IS NULL OR state = ?) "
        "AND (? IS NULL OR operation = ?) ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (rank, state, state, operation, operation, limit),
    ).fetchall()
    return [op.from_row(op.OperationRun, r) for r in rows]
