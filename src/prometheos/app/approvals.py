"""Pending approvals: the lifecycle of ``approval_requests`` (A02, CP10.2; spec/04 "Pending
approvals"; decisions 0015 and 0016).

An agent that asks for something it has no authority for gets ``approval_required``: the
service stores the exact canonical plan here and nothing else commits. The owner grants or
denies the request in an owner channel (the hub review queue, an interactive ``decision
review``). A grant issues a single-use exact-plan capability held by the server (its secret
is discarded at once; the row names it by ``capability_id``). ``approval resume`` presents
it as a :class:`~prometheos.store.capabilities.HeldGrant`, which the protected operation
consumes in its own write transaction; that same transaction moves the request to ``used``.

Lifecycle (``operational.check_approval_lifecycle``): pending -> granted | denied |
withdrawn | expired; granted -> used | withdrawn | expired | revoked. Expiry is applied
lazily by every reader and writer here. Ending a granted request revokes its capability in
the same transaction. Rows are operational state: none of this advances the project
sequence.

This module holds the lifecycle only; what each operation's plan is and how it executes is
in ``approval_kinds``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from prometheos.app import outbox
from prometheos.domain.canonical import canonical_sha256, dumps_canonical
from prometheos.domain.ids import new_operational_id
from prometheos.domain.models import ApprovalRequiredDetails, ApprovalView, PrometheosError
from prometheos.store import capabilities, db, operational
from prometheos.store.operational import ApprovalRequest
from prometheos.store.project import ProjectStore

DEFAULT_TTL_SECONDS = 24 * 3600
"""How long a pending request (and its grant) stays usable."""

MAX_PENDING_PER_RANK = 200
"""Pending requests one scope may hold at once (an agent cannot flood the owner's queue)."""

MIN_GRANT_SECONDS = 30
"""``capabilities.issue`` refuses shorter lifetimes; a request closer to its expiry than this
cannot be granted (it would expire before the agent could resume it)."""

TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"granted", "denied", "withdrawn", "expired"}),
    "granted": frozenset({"used", "withdrawn", "expired", "revoked"}),
}

OWNER_ROUTE_HUB = "/approvals/{id}"
OWNER_ROUTE_CLI = "prometheos decision review --request {id}"


BINDING_KEY_FILE = Path("keys") / "approval-binding.key"
BINDING_KEY_BYTES = 32


def binding_key(home: Path) -> bytes:
    """The server-held key of owner-view bindings (review R2-1): a random 32-byte key in the
    PrometheOS home, created once with owner-only permissions. It never leaves the server."""
    import os

    from prometheos.store.private_files import write_private

    path = home / BINDING_KEY_FILE
    if not path.exists():
        # Review R3-1: the key appears atomically and complete. It is written to a restricted
        # temporary file in the same folder and then hard-linked into place, which never
        # overwrites; a process that loses the race (or dies before the link) leaves no
        # partial key under the real name.
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        write_private(temp, secrets.token_bytes(BINDING_KEY_BYTES))
        try:
            os.link(temp, path)
        except FileExistsError:
            pass  # another process created it first
        finally:
            temp.unlink(missing_ok=True)
    key = path.read_bytes()
    if len(key) != BINDING_KEY_BYTES:
        raise PrometheosError(
            "io_error",
            f"the approval binding key ({BINDING_KEY_FILE.as_posix()} in the PrometheOS home) "
            f"holds {len(key)} bytes instead of {BINDING_KEY_BYTES}; it is damaged and is not "
            "used. Remove it to create a new key (pending push and purge requests must then "
            "be requested again)",
            {},
        )
    return key


def keyed_digest(key: bytes, value: object) -> str:
    """HMAC-SHA256 over the canonical JSON of ``value``: an agent that sees the binding cannot
    test guesses of the owner's view (restricted file names) against it."""
    return hmac.new(key, dumps_canonical(value), hashlib.sha256).hexdigest()


def _now() -> datetime:
    """The clock (tests replace it to move past an expiry)."""
    return datetime.now(UTC)


def _ts(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ts() -> str:
    return _ts(_now())


# ----- reading and lazy expiry ------------------------------------------------------------


def expire_due(conn: sqlite3.Connection) -> int:
    """End every open request whose expiry has passed (inside the caller's write
    transaction); a granted one has its capability revoked in the same transaction."""
    now = now_ts()
    rows = conn.execute(
        "SELECT * FROM approval_requests WHERE state IN ('pending','granted') AND expires_at <= ?",
        (now,),
    ).fetchall()
    for row in rows:
        _transition(conn, operational.from_row(ApprovalRequest, row), "expired", closed_at=now)
    return len(rows)


def _get(conn: sqlite3.Connection, approval_id: str) -> ApprovalRequest:
    row = operational.get_approval_request(conn, approval_id)
    if row is None:
        raise PrometheosError(
            "not_found", "approval request not found", {"approval_request_id": approval_id}
        )
    return row


def visible(row: ApprovalRequest, rank: int) -> bool:
    """A request made at a higher rank reads as absent (its plan may name hidden records)."""
    return row.caller_rank <= rank


def _transition(
    conn: sqlite3.Connection, row: ApprovalRequest, state: str, **fields: Any
) -> ApprovalRequest:
    if state not in TRANSITIONS.get(row.state, frozenset()):
        raise PrometheosError(
            "validation_error",
            f"this approval request is {row.state}; it cannot become {state}",
            {"approval_request_id": row.id, "state": row.state},
        )
    if row.state == "granted" and state in ("withdrawn", "expired", "revoked"):
        assert row.capability_id is not None
        capabilities.revoke(conn, row.capability_id)
    values = row.model_dump(mode="json") | {"state": state, **fields}
    updated = ApprovalRequest.model_validate(values, strict=True)  # lifecycle re-checked
    cols = operational.to_row(updated)
    assignments = ", ".join(f"{name} = ?" for name in cols if name != "id")
    cur = conn.execute(
        f"UPDATE approval_requests SET {assignments} WHERE id = ? AND state = ?",  # noqa: S608
        (*[v for k, v in cols.items() if k != "id"], row.id, row.state),
    )
    if cur.rowcount != 1:
        raise PrometheosError(
            "revision_conflict",
            "the approval request changed; read it again",
            {"approval_request_id": row.id},
        )
    return updated


def _write[T](store: ProjectStore, fn: Callable[[sqlite3.Connection], T]) -> T:
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            expire_due(conn)
            result: T = fn(conn)
            return result
    finally:
        conn.close()


# ----- creating a request -----------------------------------------------------------------


def create(
    store: ProjectStore,
    *,
    operation: str,
    caller_rank: int,
    actor: str,
    request_id: str | None,
    plan: dict[str, Any],
    required_scope: str,
    summary: list[str],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> ApprovalRequest:
    """Store the exact plan (or return the stored row for an identical retry: same request
    ID, rank, operation and plan; decision 0015 R10)."""
    created = _now()
    approval_id = new_operational_id("apreq")
    labels = [_printable(s)[:300] for s in summary if _printable(s)][:50] or [operation]
    row = ApprovalRequest(
        id=approval_id,
        project_id=store.project_id,
        operation=operation,
        caller_rank=caller_rank,
        actor=actor[:100],
        request_id=request_id,
        plan=plan,
        plan_sha256=canonical_sha256(plan),
        required_scope=required_scope,
        owner_route_hub=OWNER_ROUTE_HUB.format(id=approval_id),
        owner_route_cli=OWNER_ROUTE_CLI.format(id=approval_id),
        summary=labels,
        state="pending",
        created_at=_ts(created),
        expires_at=_ts(created + timedelta(seconds=ttl_seconds)),
        decided_at=None,
        decided_by=None,
        decision_note=None,
        capability_id=None,
        used_at=None,
        used_seq=None,
        result=None,
        closed_at=None,
    )

    def run(conn: sqlite3.Connection) -> ApprovalRequest:
        retry = (
            request_id is not None
            and conn.execute(
                "SELECT 1 FROM approval_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        )
        if not retry:
            _pending_cap(conn, caller_rank)
        stored = operational.insert_approval_request(conn, row)
        if stored.id == row.id:  # a new request (an identical retry returns the stored one)
            outbox.emit(
                conn,
                "approval.requested",
                {"kind": "approval_request", "id": row.id},
                {"operation": row.operation, "scope": row.required_scope},
                occurred_at=row.created_at,
                project_seq=outbox.current_seq(conn),
            )
        return stored

    return _write(store, run)


def _printable(text: str) -> str:
    """Summary lines are shown on the owner's terminal and page: control characters (newlines,
    escape sequences) from agent-supplied text are replaced, so no line can be forged."""
    return "".join(ch if ch.isprintable() else " " for ch in text).strip()


def _pending_cap(conn: sqlite3.Connection, rank: int) -> None:
    """A new request (not a retry) is refused once its scope has too many pending ones."""
    count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE state = 'pending' AND caller_rank = ?",
        (rank,),
    ).fetchone()[0]
    if count >= MAX_PENDING_PER_RANK:
        raise PrometheosError(
            "budget_exceeded",
            f"{count} approval requests are already pending at this scope; withdraw some or wait "
            "for the owner",
            {"pending": count, "max_pending": MAX_PENDING_PER_RANK},
        )


def required_error(row: ApprovalRequest) -> PrometheosError:
    """The typed ``approval_required`` answer for a stored request (never a secret)."""
    details = ApprovalRequiredDetails(
        approval_request_id=row.id,
        plan_sha256=row.plan_sha256,
        required_scope=row.required_scope,
        owner_route={"hub": row.owner_route_hub, "cli": row.owner_route_cli},  # type: ignore[arg-type]
        expires_at=row.expires_at,
    ).model_dump(mode="json")
    state = "" if row.state == "pending" else f" (it is {row.state})"
    return PrometheosError(
        "approval_required",
        f"this needs the owner's approval; nothing was changed. Approval request {row.id}"
        f"{state} waits for the owner in the hub (Approvals) or `prometheos decision review`; "
        f"after a grant, resume it with `approval resume {row.id}`",
        details,
    )


def answer(row: ApprovalRequest) -> PrometheosError:
    """What a request (or an identical retry of it) answers: ``approval_required`` while it is
    open, ``policy_denied`` naming its state once it has ended (review F10)."""
    if row.state in OPEN_STATES:
        return required_error(row)
    return PrometheosError(
        "policy_denied",
        f"approval request {row.id} is {row.state}; it will not run again. Submit a new request "
        "(with a new request_id) if it is still needed",
        {"approval_request_id": row.id, "state": row.state},
    )


OPEN_STATES = ("pending", "granted")
RECENT_ENDED = 50


def queue(store: ProjectStore) -> list[ApprovalRequest]:
    """The owner's queue: every open request (bounded by the pending cap per scope) and the 50
    most recent ended ones, oldest first. No previews here (computed per request on demand)."""

    def run(conn: sqlite3.Connection) -> list[ApprovalRequest]:
        open_rows = conn.execute(
            "SELECT * FROM approval_requests WHERE state IN ('pending','granted') "
            "ORDER BY created_at, rowid"
        ).fetchall()
        ended = conn.execute(
            "SELECT * FROM approval_requests WHERE state NOT IN ('pending','granted') "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (RECENT_ENDED,),
        ).fetchall()
        rows = [*reversed(ended), *open_rows]
        return [operational.from_row(ApprovalRequest, r) for r in rows]

    return _write(store, run)


# ----- agent side: list, get, withdraw ----------------------------------------------------


def view(row: ApprovalRequest) -> dict[str, Any]:
    """What a caller who may see the request is shown (the ``approval_view`` contract). The
    grant is server-held: neither its secret (never stored) nor its capability ID is part of
    any view."""
    values = {
        "approval_request_id": row.id,
        "operation": row.operation,
        "state": row.state,
        "caller_rank": row.caller_rank,
        "actor": row.actor,
        "request_id": row.request_id,
        "plan": row.plan,
        "plan_sha256": row.plan_sha256,
        "required_scope": row.required_scope,
        "summary": row.summary,
        "owner_route": {"hub": row.owner_route_hub, "cli": row.owner_route_cli},
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "decided_at": row.decided_at,
        "decided_by": row.decided_by,
        "decision_note": row.decision_note,
        "used_at": row.used_at,
        "used_seq": row.used_seq,
        "closed_at": row.closed_at,
        "result": row.result,
    }
    shown = ApprovalView.model_validate(values, strict=True)
    return shown.model_dump(mode="json", exclude={"preview"})


def listing(store: ProjectStore, rank: int, state: str | None, limit: int) -> list[ApprovalRequest]:
    """The newest ``limit`` requests visible at ``rank`` (oldest first): filtered and limited in
    SQL, so old requests can never push new ones out of the owner's queue."""
    if state is not None and state not in operational.APPROVAL_STATES:
        raise PrometheosError("validation_error", "unknown approval state", {"state": state})

    def run(conn: sqlite3.Connection) -> list[ApprovalRequest]:
        sql = "SELECT * FROM approval_requests WHERE caller_rank <= ?"
        params: list[Any] = [rank]
        if state is not None:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"  # rowid: insertion order
        rows = conn.execute(sql, (*params, limit)).fetchall()
        return [operational.from_row(ApprovalRequest, r) for r in reversed(rows)]

    return _write(store, run)


def get(store: ProjectStore, approval_id: str, rank: int) -> ApprovalRequest:
    def run(conn: sqlite3.Connection) -> ApprovalRequest:
        row = _get(conn, approval_id)
        if not visible(row, rank):
            raise PrometheosError(
                "not_found", "approval request not found", {"approval_request_id": approval_id}
            )
        return row

    return _write(store, run)


def withdraw(store: ProjectStore, approval_id: str, rank: int) -> ApprovalRequest:
    """The requesting rank ends its own request (a granted one loses its grant)."""

    def run(conn: sqlite3.Connection) -> ApprovalRequest:
        row = _get(conn, approval_id)
        if not visible(row, rank):
            raise PrometheosError(
                "not_found", "approval request not found", {"approval_request_id": approval_id}
            )
        if row.caller_rank != rank:
            raise PrometheosError(
                "request_key_conflict",
                "only the scope that made an approval request can withdraw it",
                {"approval_request_id": approval_id},
            )
        return _transition(conn, row, "withdrawn", closed_at=now_ts())

    return _write(store, run)


def close_applied_together(
    store: ProjectStore, approval_ids: list[str], seq: int, decided_by: str
) -> None:
    """CP20.3 (decision 0084): the requests folded into an applied "apply all recommended"
    request are closed (withdrawn, with a note); their own plans can no longer run."""

    def run(conn: sqlite3.Connection) -> None:
        for approval_id in approval_ids:
            row = _get(conn, approval_id)
            if row.state == "pending":
                _transition(
                    conn,
                    row,
                    "withdrawn",
                    closed_at=now_ts(),
                    # A withdrawn row keeps a decider only with a grant, so the owner who
                    # applied the set is named in the note (review cp20-3-a F2).
                    decision_note=(
                        f"applied together with the recommended set at sequence {seq} "
                        f"by {decided_by}"
                    ),
                )

    _write(store, run)


def for_resume(store: ProjectStore, approval_id: str, rank: int) -> ApprovalRequest:
    """The request an agent may resume: visible, made at exactly this rank (replay rule of
    decision 0010) and granted. A used request is returned for its stored result."""
    row = get(store, approval_id, rank)
    if row.caller_rank != rank:
        raise PrometheosError(
            "request_key_conflict",
            "an approval request resumes only at the scope that made it",
            {"approval_request_id": approval_id},
        )
    if row.state == "pending":
        raise required_error(row)
    if row.state not in ("granted", "used"):
        raise PrometheosError(
            "policy_denied",
            f"approval request {row.id} is {row.state}; it cannot be resumed (submit a new "
            "request)",
            {"approval_request_id": row.id, "state": row.state},
        )
    return row


def state_of(store: ProjectStore, approval_id: str) -> str | None:
    conn = store.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT state FROM approval_requests WHERE id = ?", (approval_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row["state"])


def record_result(
    store: ProjectStore, approval_id: str, result: dict[str, Any], *, failure: bool = False
) -> None:
    """Keep the single execution's response with the used request (and its sequence). A
    failure is kept only while no result is stored (a concurrent loser never overwrites)."""
    nested = [v for v in result.values() if isinstance(v, dict)]
    found = [d.get("project_seq") for d in (result, *nested)]
    seq = next((v for v in found if isinstance(v, int) and not isinstance(v, bool)), None)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            row = operational.get_approval_request(conn, approval_id)
            if row is None or row.state != "used" or (failure and row.result is not None):
                return
            values = row.model_dump(mode="json") | {"result": result, "used_seq": seq}
            updated = ApprovalRequest.model_validate(values, strict=True)
            cols = operational.to_row(updated)
            conn.execute(
                "UPDATE approval_requests SET result = ?, used_seq = ? WHERE id = ?",
                (cols["result"], cols["used_seq"], approval_id),
            )
    finally:
        conn.close()


def spend(store: ProjectStore, grant: capabilities.HeldGrant, operation: str, digest: str) -> str:
    """Consume a server-held grant in its own write transaction (for operations whose effect
    is outside the project store: the use, and the request's ``used`` state, commit before
    the effect, so it happens at most once). Returns the capability ID."""
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            return capabilities.consume(
                conn, secret=grant, project_id=store.project_id, operation=operation, digest=digest
            )
    finally:
        conn.close()


# ----- owner side: decide and revoke (owner channels only) --------------------------------


def decide(
    store: ProjectStore,
    approval_id: str,
    *,
    grant: bool,
    plan_sha256: str,
    grant_digest: str | None,
    decided_by: str,
    note: str | None,
) -> ApprovalRequest:
    """Grant or deny exactly the plan the owner was shown (``plan_sha256``). A grant issues a
    single-use capability bound to ``grant_digest`` whose secret nobody ever sees."""

    def run(conn: sqlite3.Connection) -> ApprovalRequest:
        row = _get(conn, approval_id)
        if row.state != "pending":
            raise PrometheosError(
                "revision_conflict",
                f"this approval request is {row.state}; only a pending request can be decided",
                {"approval_request_id": row.id, "state": row.state},
            )
        if plan_sha256 != row.plan_sha256:
            raise PrometheosError(
                "revision_conflict",
                "the plan differs from the one shown; review it again",
                {"approval_request_id": row.id},
            )
        now = _now()
        decided = {
            "decided_at": _ts(now),
            "decided_by": decided_by[:100],
            "decision_note": (note or None) and note[:2000],
        }
        if not grant:
            denied = _transition(conn, row, "denied", closed_at=_ts(now), **decided)
            _decided_event(conn, denied, "denied")
            return denied
        remaining = int(
            (
                datetime.strptime(row.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) - now
            ).total_seconds()
        )
        if remaining < MIN_GRANT_SECONDS:
            raise PrometheosError(
                "validation_error",
                "this approval request expires too soon to be granted; the agent can request again",
                {"approval_request_id": row.id},
            )
        issued = capabilities.issue(
            conn,
            project_id=store.project_id,
            operation=row.required_scope,
            digest=grant_digest,
            ttl_seconds=min(remaining, 86400),
            max_uses=1,
            label=f"approval {row.id}"[:200],
        )
        # Server-held: the secret is dropped here and never stored, logged or returned.
        granted = _transition(conn, row, "granted", capability_id=issued.capability_id, **decided)
        _decided_event(conn, granted, "granted")
        return granted

    return _write(store, run)


def _decided_event(conn: sqlite3.Connection, row: ApprovalRequest, decision: str) -> None:
    """``approval.decided`` in the decision's own transaction (decision 0035)."""
    assert row.decided_at is not None
    outbox.emit(
        conn,
        "approval.decided",
        {"kind": "approval_request", "id": row.id},
        {"decision": decision},
        occurred_at=row.decided_at,
        project_seq=outbox.current_seq(conn),
    )


def revoke(store: ProjectStore, approval_id: str) -> ApprovalRequest:
    """The owner withdraws a grant before it is used (its capability is revoked)."""

    def run(conn: sqlite3.Connection) -> ApprovalRequest:
        row = _get(conn, approval_id)
        if row.state != "granted":
            raise PrometheosError(
                "validation_error",
                f"this approval request is {row.state}; only a granted request can be revoked",
                {"approval_request_id": row.id, "state": row.state},
            )
        return _transition(conn, row, "revoked", closed_at=now_ts())

    return _write(store, run)
