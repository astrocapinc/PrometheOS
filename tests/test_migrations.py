from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from prometheos.domain.models import PrometheosError
from prometheos.store.migrations import Migration, migrate, schema_version

_OPEN: list[sqlite3.Connection] = []


def connect(path: Path, **kwargs: object) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=kwargs.get("isolation_level"))  # type: ignore[arg-type]
    _OPEN.append(conn)
    return conn


@pytest.fixture(autouse=True)
def close_connections() -> Iterator[None]:
    yield
    while _OPEN:
        _OPEN.pop().close()


def m1(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE a (x INTEGER)")


def m2(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE b (y INTEGER)")


def broken(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE c (z INTEGER)")
    raise RuntimeError("simulated migration failure")


def tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_applies_in_order_and_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    migrations = [Migration(1, "a", m1), Migration(2, "b", m2)]
    assert migrate(conn, migrations) == [1, 2]
    assert schema_version(conn) == 2
    assert migrate(conn, migrations) == []
    assert {"a", "b"} <= tables(conn)


def test_failure_rolls_back_schema_and_version(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    migrate(conn, [Migration(1, "a", m1)])
    with pytest.raises(RuntimeError):
        migrate(conn, [Migration(1, "a", m1), Migration(2, "broken", broken)])
    assert schema_version(conn) == 1
    assert "c" not in tables(conn)


def test_refuses_downgrade(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    migrate(conn, [Migration(1, "a", m1), Migration(2, "b", m2)])
    with pytest.raises(PrometheosError) as err:
        migrate(conn, [Migration(1, "a", m1)])
    assert err.value.code == "unsupported"
    assert schema_version(conn) == 2


def test_rejects_gaps_and_requires_autocommit_connection(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    with pytest.raises(ValueError):
        migrate(conn, [Migration(2, "b", m2)])
    implicit = connect(tmp_path / "t.sqlite", isolation_level="DEFERRED")
    with pytest.raises(ValueError):
        migrate(implicit, [Migration(1, "a", m1)])


def test_backup_hook_runs_only_before_upgrading_existing_store(tmp_path: Path) -> None:
    calls: list[tuple[int, int]] = []
    conn = connect(tmp_path / "s.sqlite")
    migrate(conn, [Migration(1, "a", m1)], before_upgrade=lambda a, b: calls.append((a, b)))
    assert calls == []
    migrate(
        conn,
        [Migration(1, "a", m1), Migration(2, "b", m2)],
        before_upgrade=lambda a, b: calls.append((a, b)),
    )
    assert calls == [(1, 2)]


def test_failed_backup_hook_prevents_migration(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")
    migrate(conn, [Migration(1, "a", m1)])

    def fail(old: int, new: int) -> None:
        raise PrometheosError("io_error", "backup failed")

    with pytest.raises(PrometheosError):
        migrate(conn, [Migration(1, "a", m1), Migration(2, "b", m2)], before_upgrade=fail)
    assert schema_version(conn) == 1


def test_migration_that_ends_its_own_transaction_reports_original_error(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.sqlite")

    def premature(c: sqlite3.Connection) -> None:
        c.execute("COMMIT")
        raise RuntimeError("original failure")

    with pytest.raises(RuntimeError, match="original failure"):
        migrate(conn, [Migration(1, "premature", premature)])
