"""The home's spend ledger for the intelligence layer (A07.6; spec/03 (x); decision 0092).

Three layers bound what the layer spends:

1. the key's own limit, set by the owner at the provider (read before a batch: ``key_limit``);
2. this ledger: the home's monthly cap and each run's cap, with a reservation taken before every
   call under a home-wide lock (SQLite ``BEGIN IMMEDIATE`` on ``<home>/intelligence/spend.sqlite``,
   so two projects or two processes can never both reserve the last cents);
3. the task's own ceiling, checked against the call's estimate before it is reserved.

A reservation is settled with the billed cost when the reply reports one, or with the estimate
(labelled as such) when it does not; a call whose outcome is unknown stays held at its estimate
and still counts. Amounts are decimal strings in US dollars.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from prometheos.domain.models import PrometheosError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
    reservation_id TEXT PRIMARY KEY,
    month TEXT NOT NULL,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved','settled','released','held')),
    estimate TEXT NOT NULL,
    amount TEXT NOT NULL,
    basis TEXT NOT NULL CHECK (basis IN ('estimate','billed')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_month ON spend (month);
CREATE INDEX IF NOT EXISTS spend_run ON spend (run_id);
CREATE TABLE IF NOT EXISTS caps (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    monthly_cap TEXT NOT NULL,
    set_at REAL NOT NULL
);
"""
COUNTED = ("reserved", "settled", "held")


def month_of(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m")


@contextmanager
def _db(home: Path) -> Iterator[sqlite3.Connection]:
    path = home / "intelligence" / "spend.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        yield conn
    finally:
        conn.close()


def _sum(conn: sqlite3.Connection, where: str, value: str) -> Decimal:
    rows = conn.execute(
        f"SELECT amount FROM spend WHERE {where} = ? AND state IN ('reserved','settled','held')",  # noqa: S608
        (value,),
    ).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal(0))


def set_monthly_cap(home: Path, cap: Decimal) -> None:
    if cap <= 0:
        raise PrometheosError("validation_error", "the monthly cap must be above zero", {})
    with _db(home) as conn:
        conn.execute(
            "INSERT INTO caps (singleton, monthly_cap, set_at) VALUES (1, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET monthly_cap = excluded.monthly_cap, "
            "set_at = excluded.set_at",
            (str(cap), time.time()),
        )


def monthly_cap(home: Path) -> Decimal | None:
    with _db(home) as conn:
        row = conn.execute("SELECT monthly_cap FROM caps WHERE singleton = 1").fetchone()
    return Decimal(row[0]) if row else None


def reserve(
    home: Path,
    *,
    project_id: str,
    run_id: str,
    task_kind: str,
    model: str,
    estimate: Decimal,
    run_cap: Decimal,
    task_ceiling: Decimal,
    now: float | None = None,
    monthly_required: bool = True,
) -> str:
    """Reserve ``estimate`` for one call, or raise ``budget_exceeded`` naming the cap. A run the
    owner approved per call (``monthly_required=False``) is bound by its own approved cap; the
    home's monthly cap still applies when one is set (review cp22-a R3-F1)."""
    moment = time.time() if now is None else now
    month = month_of(moment)
    if estimate > task_ceiling:
        raise PrometheosError(
            "budget_exceeded",
            "this call's estimate is above the task's ceiling",
            {"cap": "task", "estimate": str(estimate), "ceiling": str(task_ceiling)},
        )
    with _db(home) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT monthly_cap FROM caps WHERE singleton = 1").fetchone()
            if row is None and monthly_required:
                raise PrometheosError(
                    "budget_exceeded", "the home's monthly cap is not set", {"cap": "monthly"}
                )
            cap = Decimal(row[0]) if row is not None else None
            used = _sum(conn, "month", month)
            if cap is not None and used + estimate > cap:
                raise PrometheosError(
                    "budget_exceeded",
                    "the home's monthly cap would be passed",
                    {"cap": "monthly", "used": str(used), "limit": str(cap)},
                )
            run_used = _sum(conn, "run_id", run_id)
            if run_used + estimate > run_cap:
                raise PrometheosError(
                    "budget_exceeded",
                    "this run's cap would be passed",
                    {"cap": "run", "used": str(run_used), "limit": str(run_cap)},
                )
            reservation = "rsv_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO spend VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, 'estimate', ?, ?)",
                (reservation, month, project_id, run_id, task_kind, model, str(estimate),
                 str(estimate), moment, moment),
            )  # fmt: skip
            conn.execute("COMMIT")
            return reservation
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _finish(home: Path, reservation: str, state: str, amount: Decimal | None, basis: str) -> None:
    with _db(home) as conn:
        changed = conn.execute(
            "UPDATE spend SET state = ?, amount = COALESCE(?, amount), basis = ?, updated_at = ? "
            "WHERE reservation_id = ? AND state = 'reserved'",
            (state, None if amount is None else str(amount), basis, time.time(), reservation),
        ).rowcount
    if not changed:
        raise PrometheosError("revision_conflict", "the reservation is not open", {})


def settle(home: Path, reservation: str, billed: Decimal | None) -> None:
    """The call ended with a reply: its billed cost, or its estimate when none was reported."""
    _finish(home, reservation, "settled", billed, "billed" if billed is not None else "estimate")


def release(home: Path, reservation: str) -> None:
    """Nothing was sent (or the provider refused before work): the reservation is returned."""
    _finish(home, reservation, "released", Decimal(0), "estimate")


def hold(home: Path, reservation: str) -> None:
    """The outcome is unknown: the estimate stays counted until the owner acknowledges it."""
    _finish(home, reservation, "held", None, "estimate")


def summary(home: Path, now: float | None = None) -> dict[str, Any]:
    month = month_of(time.time() if now is None else now)
    with _db(home) as conn:
        cap = conn.execute("SELECT monthly_cap FROM caps WHERE singleton = 1").fetchone()
        rows = conn.execute(
            "SELECT state, basis, amount FROM spend WHERE month = ?", (month,)
        ).fetchall()
    used = sum((Decimal(r[2]) for r in rows if r[0] in COUNTED), Decimal(0))
    billed = sum((Decimal(r[2]) for r in rows if r[0] == "settled" and r[1] == "billed"),
                 Decimal(0))  # fmt: skip
    return {
        "month": month,
        "monthly_cap": cap[0] if cap else None,
        "used": str(used),
        "billed": str(billed),
        "estimated": str(used - billed),
        "held_calls": sum(1 for r in rows if r[0] == "held"),
    }
