"""SQLite connections and short write transactions.

Per connection: foreign_keys=ON, busy_timeout=5000, synchronous=FULL. Project
creation sets journal_mode=DELETE. Connections use isolation_level=None so
transactions are explicit (``BEGIN IMMEDIATE`` for writes).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from prometheos.domain.models import PrometheosError
from prometheos.store import admission

BUSY_TIMEOUT_MS = 5000


def connect(
    path: Path,
    *,
    create: bool = False,
    readonly: bool = False,
    immutable: bool = False,
    factory: type[sqlite3.Connection] = sqlite3.Connection,
) -> sqlite3.Connection:
    """``immutable`` (read-only only): SQLite neither locks nor looks for a journal or a
    write-ahead log, so it creates no side file (decision 0021 round 2, F5). Only for reading
    a store's fixed identity before anything else, never for data a writer may change."""
    if not create and not path.is_file():
        raise PrometheosError("not_found", "project store database is missing", {})
    if readonly:
        uri = path.resolve().as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro")
        conn = sqlite3.connect(
            uri, uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000, factory=factory
        )
    else:
        conn = sqlite3.connect(
            path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000, factory=factory
        )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if not readonly:
            conn.execute("PRAGMA synchronous = FULL")
        if create:
            conn.execute("PRAGMA journal_mode = DELETE")
    except BaseException:
        conn.close()  # a damaged file fails here; never leave the handle open (CP10.5)
        raise
    return conn


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``; any exception rolls back everything."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise PrometheosError("busy", "the project store is busy; retry shortly", {}) from exc
        raise PrometheosError("io_error", f"could not begin transaction: {exc}", {}) from exc
    try:
        yield conn
    except BaseException as failure:
        try:
            conn.execute("ROLLBACK")
        except BaseException as cleanup:
            admission.poison(conn)
            raise failure from cleanup
        raise
    try:
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        admission.poison(conn)
        try:
            conn.execute("ROLLBACK")
        except BaseException as cleanup:
            raise exc from cleanup
        if _is_busy(exc):
            raise PrometheosError("busy", "the project store is busy; retry shortly", {}) from exc
        raise PrometheosError("io_error", f"commit failed: {exc}", {}) from exc


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A short consistent read (``BEGIN`` deferred, no writes)."""
    try:
        conn.execute("BEGIN")
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise PrometheosError("busy", "the project store is busy; retry shortly", {}) from exc
        raise
    try:
        yield conn
    finally:
        try:
            conn.execute("ROLLBACK")
        except BaseException:
            admission.poison(conn)
            raise
