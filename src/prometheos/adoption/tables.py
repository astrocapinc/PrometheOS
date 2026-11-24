"""Operational storage of adoption state (store schema 10; decision 0039 section 2).

Row models are the exported contracts; their fields are the table columns (pinned by
``tests/test_cp17_inventory.py``). Writes never advance the project sequence.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from prometheos.adoption.contracts import (
    AdoptionEvidence,
    AdoptionFamily,
    AdoptionFile,
    AdoptionScan,
)
from prometheos.domain.canonical import dumps_canonical
from prometheos.domain.models import PrometheosError, StrictModel

JSON_COLUMNS: dict[type[StrictModel], frozenset[str]] = {
    AdoptionScan: frozenset({"config", "summary", "analysis"}),
    AdoptionFile: frozenset({"detail"}),
    AdoptionEvidence: frozenset({"object", "locator"}),
}
TABLES: dict[str, type[StrictModel]] = {
    "adoption_scans": AdoptionScan,
    "adoption_files": AdoptionFile,
    "adoption_evidence": AdoptionEvidence,
}


def to_row(model: StrictModel) -> dict[str, Any]:
    json_cols = JSON_COLUMNS.get(type(model), frozenset())
    row: dict[str, Any] = {}
    for name, value in model.model_dump(mode="json").items():
        row[name] = (
            dumps_canonical(value).decode("utf-8")
            if name in json_cols and value is not None
            else value
        )
    return row


def from_row[M: StrictModel](model: type[M], row: sqlite3.Row) -> M:
    json_cols = JSON_COLUMNS.get(model, frozenset())
    values = {
        name: (json.loads(row[name]) if name in json_cols and row[name] is not None else row[name])
        for name in model.model_fields
    }
    return model.model_validate(values)


def _insert(conn: sqlite3.Connection, table: str, model: StrictModel) -> None:
    row = to_row(model)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",  # noqa: S608
        tuple(row.values()),
    )


# A pruned generation keeps its scan row (state `complete` in the table) and is listed in
# `adoption_pruned` (migration 11); it reads as state `pruned` (decision 0045).
_SCANS = (
    "SELECT scan_gen, root_alias, run_id, CASE WHEN scan_gen IN (SELECT scan_gen FROM "
    "adoption_pruned) THEN 'pruned' ELSE state END AS state, config, started_at, finished_at, "
    "root_digest_before, root_digest_after, summary, error, analysis FROM adoption_scans"
)


def next_scan_gen(conn: sqlite3.Connection) -> int:
    value = conn.execute("SELECT MAX(scan_gen) FROM adoption_scans").fetchone()[0]
    return int(value or 0) + 1


def insert_scan(conn: sqlite3.Connection, scan: AdoptionScan) -> None:
    _insert(conn, "adoption_scans", scan)


def replace_scan(conn: sqlite3.Connection, scan: AdoptionScan) -> None:
    row = to_row(scan)
    sets = ", ".join(f"{k} = ?" for k in row if k != "scan_gen")
    conn.execute(
        f"UPDATE adoption_scans SET {sets} WHERE scan_gen = ?",  # noqa: S608 - fixed columns
        (*[v for k, v in row.items() if k != "scan_gen"], scan.scan_gen),
    )


def get_scan(conn: sqlite3.Connection, scan_gen: int) -> AdoptionScan | None:
    row = conn.execute(f"{_SCANS} WHERE scan_gen = ?", (scan_gen,)).fetchone()
    return None if row is None else from_row(AdoptionScan, row)


def require_scan(conn: sqlite3.Connection, scan_gen: int) -> AdoptionScan:
    scan = get_scan(conn, scan_gen)
    if scan is None:
        raise PrometheosError("not_found", "no such adoption scan", {"scan_gen": scan_gen})
    return scan


def list_scans(conn: sqlite3.Connection, root_alias: str | None = None) -> list[AdoptionScan]:
    if root_alias is None:
        rows = conn.execute(f"{_SCANS} ORDER BY scan_gen DESC").fetchall()
    else:
        rows = conn.execute(
            f"{_SCANS} WHERE root_alias = ? ORDER BY scan_gen DESC",
            (root_alias,),
        ).fetchall()
    return [from_row(AdoptionScan, r) for r in rows]


def latest_complete(conn: sqlite3.Connection, root_alias: str) -> AdoptionScan | None:
    row = conn.execute(
        f"{_SCANS} WHERE root_alias = ? AND state = 'complete' "  # noqa: S608
        "AND scan_gen NOT IN (SELECT scan_gen FROM adoption_pruned) ORDER BY scan_gen DESC LIMIT 1",
        (root_alias,),
    ).fetchone()
    return None if row is None else from_row(AdoptionScan, row)


def clear_files(conn: sqlite3.Connection, scan_gen: int) -> None:
    conn.execute("DELETE FROM adoption_files WHERE scan_gen = ?", (scan_gen,))


def insert_files(conn: sqlite3.Connection, rows: list[AdoptionFile]) -> None:
    if not rows:
        return
    first = to_row(rows[0])
    sql = (
        f"INSERT INTO adoption_files ({', '.join(first)}) "  # noqa: S608 - fixed columns
        f"VALUES ({', '.join('?' for _ in first)})"
    )
    conn.executemany(sql, [tuple(to_row(r).values()) for r in rows])


def list_files(
    conn: sqlite3.Connection,
    scan_gen: int,
    *,
    status: str | None = None,
    zone: str | None = None,
    container: str | None = None,
    prefix: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> tuple[list[AdoptionFile], int]:
    where = ["scan_gen = ?"]
    args: list[Any] = [scan_gen]
    for column, value in (("status", status), ("zone", zone), ("container", container)):
        if value is not None:
            where.append(f"{column} = ?")
            args.append(value)
    if prefix:
        where.append("substr(path, 1, ?) = ?")
        args += [len(prefix), prefix]
    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM adoption_files WHERE {clause}",  # noqa: S608 - fixed columns
        args,
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM adoption_files WHERE {clause} ORDER BY path LIMIT ? OFFSET ?",  # noqa: S608
        (*args, limit, offset),
    ).fetchall()
    return [from_row(AdoptionFile, r) for r in rows], int(total)


def replace_evidence(conn: sqlite3.Connection, scan_gen: int, rows: list[AdoptionEvidence]) -> None:
    conn.execute("DELETE FROM adoption_evidence WHERE scan_gen = ?", (scan_gen,))
    if not rows:
        return
    first = to_row(rows[0])
    sql = (
        f"INSERT INTO adoption_evidence ({', '.join(first)}) "  # noqa: S608 - fixed columns
        f"VALUES ({', '.join('?' for _ in first)})"
    )
    conn.executemany(sql, [tuple(to_row(r).values()) for r in rows])


def list_evidence(
    conn: sqlite3.Connection,
    scan_gen: int,
    *,
    subject: str | None = None,
    related: str | None = None,
    predicate: str | None = None,
    tier: str | None = None,
    analyzer: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> tuple[list[AdoptionEvidence], int]:
    """Evidence of one generation; ``related`` matches rows whose subject *or* object path is
    that path (everything said about a file, in both directions)."""
    where = ["scan_gen = ?"]
    args: list[Any] = [scan_gen]
    for column, value in (
        ("subject", subject),
        ("predicate", predicate),
        ("tier", tier),
        ("analyzer", analyzer),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            args.append(value)
    if related is not None:
        where.append("(subject = ? OR json_extract(object, '$.path') = ?)")
        args += [related, related]
    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM adoption_evidence WHERE {clause}",  # noqa: S608 - fixed columns
        args,
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM adoption_evidence WHERE {clause} "  # noqa: S608 - fixed columns
        "ORDER BY subject, predicate, evidence_id LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    return [from_row(AdoptionEvidence, r) for r in rows], int(total)


def all_evidence(conn: sqlite3.Connection, scan_gen: int) -> list[AdoptionEvidence]:
    rows = conn.execute(
        "SELECT * FROM adoption_evidence WHERE scan_gen = ? "
        "ORDER BY subject, predicate, evidence_id",
        (scan_gen,),
    ).fetchall()
    return [from_row(AdoptionEvidence, r) for r in rows]


def all_files(conn: sqlite3.Connection, scan_gen: int) -> list[AdoptionFile]:
    rows = conn.execute(
        "SELECT * FROM adoption_files WHERE scan_gen = ? ORDER BY path", (scan_gen,)
    ).fetchall()
    return [from_row(AdoptionFile, r) for r in rows]


def replace_families(
    conn: sqlite3.Connection, scan_gen: int, families: list[AdoptionFamily]
) -> None:
    conn.execute("DELETE FROM adoption_families WHERE scan_gen = ?", (scan_gen,))
    conn.executemany(
        "INSERT INTO adoption_families (scan_gen, family_id, payload) VALUES (?, ?, ?)",
        [
            (scan_gen, f.family_id, dumps_canonical(f.model_dump(mode="json")).decode("utf-8"))
            for f in families
        ],
    )


def list_families(conn: sqlite3.Connection, scan_gen: int) -> list[AdoptionFamily]:
    rows = conn.execute(
        "SELECT payload FROM adoption_families WHERE scan_gen = ? ORDER BY family_id", (scan_gen,)
    ).fetchall()
    return [AdoptionFamily.model_validate(json.loads(r[0])) for r in rows]


def get_family(conn: sqlite3.Connection, scan_gen: int, family_id: str) -> AdoptionFamily:
    row = conn.execute(
        "SELECT payload FROM adoption_families WHERE scan_gen = ? AND family_id = ?",
        (scan_gen, family_id),
    ).fetchone()
    if row is None:
        raise PrometheosError(
            "not_found", "no such family in this scan", {"family_id": family_id[:40]}
        )
    return AdoptionFamily.model_validate(json.loads(row[0]))
