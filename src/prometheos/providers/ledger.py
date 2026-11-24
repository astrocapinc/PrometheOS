"""The provider call ledger and budget reservations (spec/08 "Jobs and budgets").

A call is *reserved* in the same transaction that checks the budget, before anything is
sent: if the estimated cost would take spending past the cap, or the run already used its
calls, nothing is sent. A reservation that was sent and never answered stays counted
(``unknown``); only a call that provably never left this computer is released. Rows hold
digests, counts and costs, never prompt or reply text, and never a credential.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Any

from prometheos.domain.models import PrometheosError, utc_now
from prometheos.providers.config import ProviderConfig, price_per_token
from prometheos.store import capabilities, db
from prometheos.store.project import ProjectStore

COST_LABEL = "estimate, not a bill: token counts are approximate and prices are what you entered"
SIX = Decimal("0.000001")


def estimate_tokens(chars: int) -> int:
    """About four characters per token for English prose; deliberately a rough upper-ish guess."""
    return math.ceil(chars / 4)


def money(value: Decimal) -> str:
    return str(value.quantize(SIX, rounding=ROUND_UP))


def estimate_cost(config: ProviderConfig, input_tokens: int, output_tokens: int) -> Decimal | None:
    pin = price_per_token(config.price_input_per_mtok_usd)
    pout = price_per_token(config.price_output_per_mtok_usd)
    if pin is None or pout is None:
        return None
    return pin * input_tokens + pout * output_tokens


@dataclass(frozen=True)
class Totals:
    calls: int
    spent_usd: Decimal
    unknown: int


def _cost_of(row: sqlite3.Row) -> Decimal:
    for column in ("actual_cost", "reserved_cost"):
        if row[column] is not None:
            return Decimal(row[column])
    return Decimal(0)


def totals(conn: sqlite3.Connection) -> Totals:
    rows = conn.execute("SELECT * FROM provider_calls WHERE state != 'refused'").fetchall()
    return Totals(
        calls=len(rows),
        spent_usd=sum((_cost_of(r) for r in rows), Decimal(0)),
        unknown=sum(1 for r in rows if r["state"] == "unknown"),
    )


def reserve(
    store: ProjectStore,
    config: ProviderConfig,
    *,
    run_key: str,
    purpose: str,
    route: str,
    host: str,
    attempt: int,
    input_digest: str,
    request_identity: str,
    input_refs: list[str],
    task_id: str | None,
    max_rank: int,
    reserved_cost: Decimal | None,
    acknowledges: str | None = None,
    consume: list[tuple[str, str]] | None = None,
    secret: capabilities.Secret = None,
    egress_secret: capabilities.Secret = None,
) -> str:
    """Check the budget, spend the owner's capability(ies) and record the reservation, atomically.

    ``consume`` lists ``(operation, digest)`` pairs to consume on the first attempt (the
    secret for ``provider_run`` is ``secret``; for ``provider_egress`` it is ``egress_secret``)."""
    call_id = str(uuid.uuid4())
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            made = conn.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE run_key = ? AND state != 'refused'",
                (run_key,),
            ).fetchone()[0]
            if made >= config.budget.max_calls_per_run or made + 1 != attempt:
                raise PrometheosError(
                    "budget_exceeded",
                    "this run has used its calls (one generation and at most one repair)",
                    {"calls_made": made, "max_calls_per_run": config.budget.max_calls_per_run},
                )
            cap = config.budget.spend_cap_usd
            if cap is not None:
                spent = totals(conn).spent_usd
                needed = reserved_cost if reserved_cost is not None else Decimal(0)
                if spent + needed > Decimal(cap):
                    raise PrometheosError(
                        "budget_exceeded",
                        "this call could take spending past the cap; nothing was sent",
                        {
                            "spent_estimate_usd": money(spent),
                            "this_call_estimate_usd": money(needed),
                            "cap_usd": cap,
                            "label": COST_LABEL,
                        },
                    )
            if attempt == 1:
                for operation, digest in consume or []:
                    capabilities.consume(
                        conn,
                        secret=egress_secret if operation == "provider_egress" else secret,
                        project_id=store.project_id,
                        operation=operation,
                        digest=digest,
                    )
            now = utc_now()
            conn.execute(
                "INSERT INTO provider_calls (id, run_key, purpose, route, endpoint_host, model, "
                "attempt, state, reserved_cost, cost_basis, input_digest, request_identity, "
                "input_refs, task_id, max_rank, acknowledges, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,'reserved',?,?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    run_key,
                    purpose,
                    route,
                    host,
                    config.model or "",
                    attempt,
                    None if reserved_cost is None else money(reserved_cost),
                    "estimate_upper_bound",
                    input_digest,
                    request_identity,
                    json.dumps(input_refs),
                    task_id,
                    max_rank,
                    acknowledges,
                    now,
                    now,
                ),
            )
    finally:
        conn.close()
    return call_id


def refuse(
    store: ProjectStore, *, run_key: str, purpose: str, route: str, host: str, model: str,
    attempt: int, input_digest: str, reason: str, max_rank: int,
) -> None:  # fmt: skip
    """Audit row for a call that was refused before anything was sent."""
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            now = utc_now()
            conn.execute(
                "INSERT INTO provider_calls (id, run_key, purpose, route, endpoint_host, model, "
                "attempt, state, input_digest, input_refs, error, max_rank, created_at, "
                "updated_at) "
                "VALUES (?,?,?,?,?,?,?,'refused',?,'[]',?,?,?,?)",
                (str(uuid.uuid4()), run_key, purpose, route, host, model, attempt, input_digest,
                 reason[:300], max_rank, now, now),
            )  # fmt: skip
    finally:
        conn.close()


def mark(store: ProjectStore, call_id: str, state: str, **fields: Any) -> None:
    if state not in ("sent", "succeeded", "failed", "unknown"):
        raise ValueError(state)
    columns = {"state": state, "updated_at": utc_now(), **fields}
    names = ", ".join(f"{k} = ?" for k in columns)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            conn.execute(
                f"UPDATE provider_calls SET {names} WHERE id = ?",  # noqa: S608
                (*columns.values(), call_id),
            )
    finally:
        conn.close()


def get_call(conn: sqlite3.Connection, call_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM provider_calls WHERE id = ?", (call_id,)
    ).fetchone()
    return row


def call_out(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "call_id": row["id"],
        "run_key": row["run_key"],
        "purpose": row["purpose"],
        "route": row["route"],
        "endpoint_host": row["endpoint_host"],
        "model": row["model"],
        "attempt": row["attempt"],
        "state": row["state"],
        "reserved_cost_usd": row["reserved_cost"],
        "actual_cost_usd": row["actual_cost"],
        "cost_basis": row["cost_basis"],
        "usage": json.loads(row["usage"]) if row["usage"] else None,
        "input_digest": row["input_digest"],
        "output_digest": row["output_digest"],
        "task_id": row["task_id"],
        "error": row["error"],
        "acknowledges": row["acknowledges"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def listing(conn: sqlite3.Connection, max_rank: int, limit: int = 100) -> list[dict[str, Any]]:
    """Calls the caller may see: a run made over restricted inputs (and its task ID and
    digests) is invisible to a lower rank (review cp6-a M3)."""
    rows = conn.execute(
        "SELECT * FROM provider_calls WHERE max_rank <= ? ORDER BY created_at DESC, rowid DESC "
        "LIMIT ?",
        (max_rank, limit),
    ).fetchall()
    return [call_out(r) for r in rows]


def unknown_unacknowledged(conn: sqlite3.Connection, request_identity: str) -> list[str]:
    """Unknown-outcome calls for this logical request that no later call acknowledged. The
    identity (purpose, meeting, scope, model, host) is stable where the payload digest is not
    (the context carries the project sequence)."""
    rows = conn.execute(
        "SELECT id FROM provider_calls WHERE request_identity = ? AND state = 'unknown' "
        "AND id NOT IN (SELECT acknowledges FROM provider_calls WHERE acknowledges IS NOT NULL)",
        (request_identity,),
    ).fetchall()
    return [r["id"] for r in rows]


def usage_summary(
    conn: sqlite3.Connection, config: ProviderConfig, full: bool = True
) -> dict[str, Any]:
    """Budget accounting. ``full=False`` (agent scope) omits the call counts: spend, cap and
    remaining explain a budget blocker, while a total call count minus the visible ledger rows
    would tell an agent that hidden calls exist (review cp6-a R2-I1)."""
    total = totals(conn)
    cap = config.budget.spend_cap_usd
    counts = {"calls": total.calls, "unknown_outcomes": total.unknown} if full else {}
    return {
        **counts,
        "spent_estimate_usd": money(total.spent_usd),
        "cap_usd": cap,
        "remaining_usd": None
        if cap is None
        else money(max(Decimal(0), Decimal(cap) - total.spent_usd)),
        "label": COST_LABEL,
    }
