"""Ordered, transactional, version-checked schema migrations.

The store records its schema version in ``PRAGMA user_version``. Each migration
runs inside its own ``BEGIN IMMEDIATE`` transaction together with the version
bump, so a failure leaves the previous schema intact. A store newer than the
running software is refused rather than downgraded by guesswork.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from prometheos.domain.models import PrometheosError


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def validate_sequence(migrations: Sequence[Migration]) -> None:
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise ValueError(
                f"migration versions must be contiguous from 1; got {migration.version} "
                f"at position {expected}"
            )


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def pending(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> list[Migration]:
    validate_sequence(migrations)
    current = schema_version(conn)
    latest = len(migrations)
    if current > latest:
        raise PrometheosError(
            "unsupported",
            f"store schema version {current} is newer than supported version {latest}; "
            "refusing to downgrade",
            {"store_schema_version": current, "supported_schema_version": latest},
        )
    return list(migrations[current:])


def migrate(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration],
    before_upgrade: Callable[[int, int], None] | None = None,
) -> list[int]:
    """Apply pending migrations; return the versions applied.

    ``before_upgrade(old, new)`` runs before migrating an existing (non-empty)
    store so callers can create and verify a backup first.
    """
    if conn.isolation_level is not None:
        raise ValueError("migrations require a connection with isolation_level=None")
    todo = pending(conn, migrations)
    if not todo:
        return []
    current = schema_version(conn)
    if current > 0 and before_upgrade is not None:
        before_upgrade(current, todo[-1].version)
    applied: list[int] = []
    for migration in todo:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if schema_version(conn) != migration.version - 1:
                raise PrometheosError(
                    "revision_conflict",
                    "store schema changed concurrently during migration",
                    {"expected": migration.version - 1},
                )
            migration.apply(conn)
            conn.execute(f"PRAGMA user_version = {int(migration.version)}")
            conn.execute("COMMIT")
        except BaseException:
            # A migration may already have ended the transaction (e.g. executescript);
            # never let a failing ROLLBACK mask the original error.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(migration.version)
    return applied
