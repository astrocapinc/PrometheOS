"""Per-user application home, project registry and store-location policy.

Home: ``PROMETHEOS_HOME`` or ``%LOCALAPPDATA%\\PrometheOS-v1`` (``~/.local/share/
prometheos-v1`` elsewhere). The registry is a small SQLite database mapping
project IDs to local store directories. Store authorities on UNC/network/known
synchronized locations are refused; unknown filesystems need an explicit
local declaration, which is recorded as an environment limitation.
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from prometheos.domain.models import PrometheosError, utc_now
from prometheos.store.migrations import Migration, migrate, pending, schema_version
from prometheos.store.operational import (
    MaintenanceApproval,
    StoreQuarantine,
    from_row,
    insert,
)

SYNC_MARKERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "icloud",
    "box sync",
    "pcloud",
)
DRIVE_REMOTE = 4


def app_home() -> Path:
    override = os.environ.get("PROMETHEOS_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise PrometheosError("io_error", "LOCALAPPDATA is not set; set PROMETHEOS_HOME", {})
        return Path(base) / "PrometheOS-v1"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "prometheos-v1"


@dataclass(frozen=True)
class LocationReport:
    path: Path
    accepted: bool
    declared_local: bool
    reason: str


def _drive_type(path: Path) -> int | None:
    if sys.platform != "win32":
        return None
    drive = path.drive
    if not drive or not drive.endswith(":"):
        return None
    try:
        windll = ctypes.windll  # type: ignore[attr-defined,unused-ignore]
        return int(windll.kernel32.GetDriveTypeW(drive + "\\"))
    except (AttributeError, OSError):
        return None


def check_store_location(path: Path, declared_local: bool = False) -> LocationReport:
    raw = str(path)
    if raw.startswith(("\\\\", "//")):
        return LocationReport(
            path, False, declared_local, "UNC/network authority locations are refused"
        )
    absolute = Path(os.path.abspath(path))
    lowered = str(absolute).lower().replace("\\", "/")
    for marker in SYNC_MARKERS:
        if f"/{marker}" in lowered:
            return LocationReport(
                absolute, False, declared_local, f"synchronized folder ({marker}) is refused"
            )
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        sync_root = os.environ.get(env)
        if sync_root and lowered.startswith(sync_root.lower().replace("\\", "/")):
            return LocationReport(absolute, False, declared_local, "OneDrive folder is refused")
    kind = _drive_type(absolute)
    if kind == DRIVE_REMOTE:
        return LocationReport(absolute, False, declared_local, "network drive is refused")
    if kind is None and sys.platform == "win32" and not declared_local:
        return LocationReport(
            absolute, False, declared_local, "filesystem type unknown; declare it local explicitly"
        )
    note = "local fixed drive" if kind in (2, 3) else "declared local by the owner (unverified)"
    return LocationReport(absolute, True, declared_local, note)


def _registry_v1(conn: sqlite3.Connection) -> None:
    # Registries written before CP10 already hold this table at user_version 0.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS projects (project_id TEXT PRIMARY KEY, store_path TEXT "
        "NOT NULL, title TEXT NOT NULL, registered_at TEXT NOT NULL, previous_store_path TEXT, "
        "location_note TEXT NOT NULL)"
    )


REGISTRY_V2 = r"""
CREATE TABLE maintenance_approvals (
    id TEXT PRIMARY KEY CHECK (id LIKE 'mapreq\_%' ESCAPE '\'),
    project_id TEXT NOT NULL,
    damaged_store_path TEXT NOT NULL,
    damaged_db_sha256 TEXT NOT NULL,
    backup_manifest_sha256 TEXT,
    target_store_path TEXT,
    caller_rank INTEGER NOT NULL CHECK (caller_rank BETWEEN 0 AND 2),
    actor TEXT NOT NULL,
    request_id TEXT,
    plan TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending','granted','denied','withdrawn','expired','revoked','used')
    ),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    grant_expires_at TEXT,
    used_at TEXT,
    closed_at TEXT,
    -- decision 0021: a registry switch names both a backup and a target, and a migrate
    -- request (plan action 'migrate') names neither.
    CHECK ((backup_manifest_sha256 IS NULL) = (target_store_path IS NULL)),
    CHECK ((state IN ('pending','granted')) = (closed_at IS NULL)),
    CHECK ((state = 'used') = (used_at IS NOT NULL)),
    CHECK ((decided_at IS NULL) = (decided_by IS NULL)),
    CHECK (grant_expires_at IS NULL OR decided_at IS NOT NULL),
    CHECK (CASE WHEN state = 'pending' THEN decided_at IS NULL
                WHEN state IN ('granted','denied','used','revoked') THEN decided_at IS NOT NULL
                ELSE 1 END),
    CHECK (CASE WHEN state IN ('granted','used','revoked') THEN grant_expires_at IS NOT NULL
                WHEN state IN ('pending','denied') THEN grant_expires_at IS NULL
                ELSE (decided_at IS NULL) = (grant_expires_at IS NULL) END)
);
CREATE INDEX maintenance_approvals_project ON maintenance_approvals(project_id, state);
CREATE UNIQUE INDEX maintenance_approvals_request ON maintenance_approvals(request_id)
    WHERE request_id IS NOT NULL;

CREATE TABLE store_quarantine (
    id TEXT PRIMARY KEY CHECK (id LIKE 'quarantine\_%' ESCAPE '\'),
    project_id TEXT NOT NULL,
    damaged_store_path TEXT NOT NULL,
    damaged_db_sha256 TEXT NOT NULL,
    damaged_schema_version INTEGER,
    damaged_integrity TEXT NOT NULL CHECK (
        damaged_integrity IN ('ok','corrupt','unreadable','missing')
    ),
    replacement_store_path TEXT NOT NULL,
    backup_sha256 TEXT NOT NULL,
    backup_manifest_sha256 TEXT NOT NULL,
    maintenance_approval_id TEXT REFERENCES maintenance_approvals(id),
    switched_at TEXT NOT NULL,
    switched_by TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX store_quarantine_project ON store_quarantine(project_id, switched_at);
"""


def _registry_v2(conn: sqlite3.Connection) -> None:
    for statement in REGISTRY_V2.split(";"):
        if statement.strip():
            conn.execute(statement)


REGISTRY_MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "projects", _registry_v1),
    Migration(2, "maintenance approvals, store quarantine records (A02, CP10)", _registry_v2),
)


class Registry:
    """``<home>/registry.sqlite``: project_id -> store directory, plus (registry schema 2,
    A02/CP10) maintenance approvals and quarantine records, which must work while no project
    database can be opened.

    The registry schema is versioned in ``PRAGMA user_version`` and migrated forward on
    connect, after a verified copy of the previous registry is written to
    ``<home>/backups/`` (decision 0015). A registry newer than this software is refused."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.path = home / "registry.sqlite"

    def _connect(self, create: bool) -> sqlite3.Connection:
        if create:
            self.home.mkdir(parents=True, exist_ok=True)
        elif not self.path.exists():
            raise PrometheosError("not_found", "no PrometheOS projects are registered", {})
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")  # decision 0015 round 2 (R9)
        try:
            self._upgrade(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _upgrade(self, conn: sqlite3.Connection) -> None:
        for _attempt in range(3):
            todo = pending(conn, REGISTRY_MIGRATIONS)
            if not todo:
                return
            has_projects = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
            ).fetchone()
            if has_projects:
                self._verified_copy(conn, schema_version(conn), todo[-1].version)
            try:
                migrate(conn, REGISTRY_MIGRATIONS)
                return
            except PrometheosError as exc:
                # Another process migrated between our check and our transaction.
                if exc.code != "revision_conflict":
                    raise
        if pending(conn, REGISTRY_MIGRATIONS):
            raise PrometheosError("busy", "the registry is being upgraded; retry", {})

    def _verified_copy(self, conn: sqlite3.Connection, old: int, new: int) -> Path:
        """Copy the registry before migrating and verify the copy (integrity, row counts)."""
        folder = self.home / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = "".join(ch for ch in utc_now() if ch.isalnum())
        out = folder / f"registry-v{old}-to-v{new}-{stamp}-{uuid.uuid4().hex[:8]}.sqlite"
        target = sqlite3.connect(out)
        try:
            conn.backup(target)
        finally:
            target.close()
        check = sqlite3.connect(out)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            copied = check.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            version = check.execute("PRAGMA user_version").fetchone()[0]
        finally:
            check.close()
        source = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        if not ok or copied != source or version != old:
            raise PrometheosError(
                "io_error", "the registry backup did not verify; the registry was not upgraded", {}
            )
        return out

    def register(
        self, project_id: str, store: Path, title: str, note: str, *, replace: bool = False
    ) -> str | None:
        conn = self._connect(create=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT store_path FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            previous = row["store_path"] if row else None
            if row and not replace:
                conn.execute("ROLLBACK")
                raise PrometheosError(
                    "revision_conflict", "project is already registered", {"project_id": project_id}
                )
            conn.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                "store_path=excluded.store_path, title=excluded.title, "
                "registered_at=excluded.registered_at, "
                "previous_store_path=excluded.previous_store_path, "
                "location_note=excluded.location_note",
                (project_id, str(store), title, utc_now(), previous, note),
            )
            conn.execute("COMMIT")
            return previous
        finally:
            conn.close()

    def lookup(self, project_id: str) -> Path:
        conn = self._connect(create=False)
        try:
            row = conn.execute(
                "SELECT store_path FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise PrometheosError(
                "not_found", "project is not registered", {"project_id": project_id}
            )
        return Path(row["store_path"])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` registry transaction (CP10.5, decision 0021): a
        maintenance approval's use and the registry switch it authorises commit together."""
        conn = self._connect(create=False)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise PrometheosError("busy", "the registry is busy; retry shortly", {}) from exc
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def entry(self, project_id: str) -> dict[str, str | None]:
        """The registry row of one project (``not_found`` when it is not registered)."""
        conn = self._connect(create=False)
        try:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise PrometheosError(
                "not_found", "project is not registered", {"project_id": project_id}
            )
        return dict(row)

    def entries(self) -> list[dict[str, str | None]]:
        if not self.path.exists():
            return []
        conn = self._connect(create=False)
        try:
            rows = conn.execute("SELECT * FROM projects ORDER BY registered_at").fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    # ----- CP10 (A02): maintenance approvals and quarantine records ----------------------
    # Minimal accessors; requesting, granting, using and switching belong to CP10.5.

    def insert_maintenance_approval(self, approval: MaintenanceApproval) -> MaintenanceApproval:
        """Insert, or return the stored approval for an identical retry; the same
        ``request_id`` from another rank or for another plan is ``request_key_conflict``."""
        conn = self._connect(create=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if approval.request_id is not None:
                    row = conn.execute(
                        "SELECT * FROM maintenance_approvals WHERE request_id = ?",
                        (approval.request_id,),
                    ).fetchone()
                    if row is not None:
                        existing = from_row(MaintenanceApproval, row)
                        if (
                            existing.caller_rank != approval.caller_rank
                            or existing.plan_sha256 != approval.plan_sha256
                        ):
                            raise PrometheosError(
                                "request_key_conflict",
                                "request_id was already used for a different request",
                                {"request_id": approval.request_id},
                            )
                        conn.execute("COMMIT")
                        return existing
                insert(conn, "maintenance_approvals", approval)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return approval

    def maintenance_approval(self, approval_id: str) -> MaintenanceApproval | None:
        conn = self._connect(create=False)
        try:
            row = conn.execute(
                "SELECT * FROM maintenance_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else from_row(MaintenanceApproval, row)

    def maintenance_approvals(self, project_id: str) -> list[MaintenanceApproval]:
        if not self.path.exists():
            return []
        conn = self._connect(create=False)
        try:
            rows = conn.execute(
                "SELECT * FROM maintenance_approvals WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
        finally:
            conn.close()
        return [from_row(MaintenanceApproval, r) for r in rows]

    def insert_quarantine(self, record: StoreQuarantine) -> None:
        conn = self._connect(create=True)
        try:
            insert(conn, "store_quarantine", record)
        except sqlite3.IntegrityError as exc:
            raise PrometheosError(
                "validation_error", "the quarantine record names an unknown approval", {}
            ) from exc
        finally:
            conn.close()

    def quarantine_records(self, project_id: str) -> list[StoreQuarantine]:
        if not self.path.exists():
            return []
        conn = self._connect(create=False)
        try:
            rows = conn.execute(
                "SELECT * FROM store_quarantine WHERE project_id = ? ORDER BY switched_at, id",
                (project_id,),
            ).fetchall()
        finally:
            conn.close()
        return [from_row(StoreQuarantine, r) for r in rows]
