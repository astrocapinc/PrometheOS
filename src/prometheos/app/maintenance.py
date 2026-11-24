"""Maintenance service: inspect, verify, restore and switch without opening a project database
(A02, CP10.5; spec/04 "Maintenance service", spec/08 "Recovery"; decision 0021).

It works from the per-user registry, the store's files and backup archives only:

- **inspect** reads the registered (or a quarantined) store's files. The database file is
  read in place (header, digest) and *copied* into a scratch folder of the PrometheOS home;
  only the copy is opened (integrity, schema version, project identity). A hot rollback
  journal (an interrupted write) is reported as ``needs_journal_recovery``, never applied in
  place. The maintenance lock is probed with a read-only handle and never created. Nothing in
  the store is opened for writing and no file is created there. Results are cached per store
  state (file sizes and times, the journal's state), and one probe per store runs at a time.
- **verify** checks a selected backup's member paths, sizes, digests, schema and database
  integrity in a scratch staging folder (``store.backup.verify_into``) and that it belongs to
  the project.
- **restore** verifies again into a staging folder beside a *new* target folder and renames
  it into place. The registry does not change and the damaged store is never written. An
  agent's restore always lands in a folder PrometheOS creates under the home
  (``<home>/stores/<project>/restored-...``); only the owner names a location (round 2, F1).
- **journal recovery** copies the store and its hot journal into staging, lets SQLite roll the
  journal back into the COPY, checks it and turns it into a verified backup in the managed
  area, then restores that backup like any other (round 2, F3).
- **switch** points the registry at a verified restored store, only when the registered store
  does not open (damaged, missing, a hot journal; round 2, F2). An unreadable database refuses
  with ``io_error`` because its exact digest cannot be bound. It verifies the
  backup again, checks that the target's database is the backup's verified database byte for
  byte and that every media file matches its digest, and only then, in one registry
  transaction under the target's maintenance lock, rechecks the registry entry, both database
  digests, media containment/digests and absence of SQLite side files, uses the owner's grant
  (if any), switches and records a ``store_quarantine`` row for the damaged store, which
  stays untouched and listed.

Switching is protected: an agent's switch answers ``approval_required`` with a request held in
the registry's ``maintenance_approvals`` (it binds the project, the damaged store's location,
health and database digest, the verified backup's manifest digest, the target location, the
caller rank and the plan digest, and expires). The owner grants it on the hub's recovery screen
or in an interactive ``prometheos maintenance review`` session; the requesting rank resumes it
once. A changed store, backup or target refuses the resume (``revision_conflict``): ask again.

The same maintenance approvals carry an agent's request to migrate a store's schema
(decision 0016: a store that needs migration cannot hold a project approval request).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Any

from prometheos.app import approvals
from prometheos.app.authority import OwnerAct, require_owner
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.ids import is_record_id, is_request_id, new_operational_id
from prometheos.domain.models import ApprovalRequiredDetails, PrometheosError
from prometheos.domain.redact import ABSOLUTE, is_path_key
from prometheos.store import admission
from prometheos.store import backup as backup_mod
from prometheos.store import lock as lock_mod
from prometheos.store import project as project_mod
from prometheos.store.home import Registry, check_store_location
from prometheos.store.operational import (
    MaintenanceApproval,
    MaintenanceApprovalView,
    StoreQuarantine,
    from_row,
    insert,
    to_row,
)
from prometheos.store.project import DB_NAME, SUBDIRS, ProjectStore

HOME_LABEL = "<PrometheOS home>"
LOCAL_LABEL = "<local path>"
RECOVERY_HUB = "/recovery"
OWNER_ROUTE_CLI = "prometheos maintenance review --request {id} --project {project}"
DEFAULT_TTL_SECONDS = 24 * 3600
MIN_GRANT_SECONDS = 30
MAX_PENDING_PER_RANK = 50
MAX_UNSWITCHED_RESTORES = 10
MANAGED = "stores"
BACKUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.zip$")
OPERATIONS = {"registry_switch": "maintenance_switch", "migrate": "maintenance_migrate"}
SCOPES = {"registry_switch": "maintenance_switch", "migrate": "maintenance_migrate"}
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"granted", "denied", "withdrawn", "expired"}),
    "granted": frozenset({"used", "withdrawn", "expired", "revoked"}),
}
DAMAGED = frozenset(
    {"corrupt", "unreadable", "missing", "not_a_store", "wrong_project", "needs_journal_recovery"}
)
"""Store health a switch may replace (round 2, F2): the store does not open, and nothing in it
is a reason to keep it live instead (a newer PrometheOS, a migration, an owner's rollback)."""
SQLITE_MAGIC = b"SQLite format 3\x00"
JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
CHUNK = 1024 * 1024
INTEGRITY = {"ok", "corrupt", "unreadable", "missing"}


def _supported() -> int:
    """The schema version this software opens, read from the store module at call time (the
    module that opens stores uses this binding; tests of older schemas replace it there)."""
    migrations: tuple[object, ...] = project_mod.MIGRATIONS  # type: ignore[attr-defined]
    return len(migrations)


# ----- recovery pointers -----------------------------------------------------------------


def recovery_pointer(project_id: str | None) -> dict[str, str]:
    """Where a project that will not open is recovered: the same three steps on every route."""
    project = f" --project {project_id}" if project_id else ""
    return {
        "cli": f"prometheos maintenance inspect{project}",
        "mcp": "maintenance_inspect (prometheos mcp serve --stdio --maintenance)",
        "http": "GET /api/v1/maintenance/inspect",
        "hub": RECOVERY_HUB,
    }


def recovery_error(project_id: str, health: str, why: str) -> PrometheosError:
    """``io_error`` naming the maintenance routes instead of a crash (spec/04, S27)."""
    return PrometheosError(
        "io_error",
        f"the project store cannot be opened ({why}); nothing was changed. Inspect it and restore "
        "a verified backup into a new store with the maintenance service: `prometheos "
        "maintenance inspect`, the MCP maintenance tools or the hub recovery screen",
        {
            "project_id": project_id,
            "store_health": health,
            "recovery": recovery_pointer(project_id),
        },
    )


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "unknown error"
    return line[:300]


def _schema_error(project_id: str, version: int) -> PrometheosError:
    supported = _supported()
    details: dict[str, Any] = {
        "store_schema_version": version,
        "supported_schema_version": supported,
        "project_id": project_id,
        "store_health": "newer_schema" if version > supported else "needs_migration",
        "recovery": recovery_pointer(project_id),
    }
    if version > supported:
        message = (
            f"store schema version {version} is newer than supported version {supported}; "
            "refusing to downgrade"
        )
    else:
        message = "store schema requires migration before use (prometheos maintenance migrate)"
        details["recovery"]["migrate"] = f"prometheos maintenance migrate --project {project_id}"
    return PrometheosError("unsupported", message, details)


def _mode_error(project_id: str, *, hot: bool = False) -> PrometheosError:
    also = (
        "; a hot rollback journal is also present and is never applied to the original"
        if hot
        else ""
    )
    return PrometheosError(
        "unsupported",
        "the source uses an unsupported journal mode; inspect it without opening the "
        f"original, and use software supporting that mode before changing this store{also}",
        {
            "project_id": project_id,
            "store_health": "unsupported_journal_mode",
            "recovery": recovery_pointer(project_id),
        },
    )


@dataclass(frozen=True)
class SourceClass:
    """The header-level class of a guarded source (:func:`classify_source`).

    ``health`` is ``unsupported_journal_mode``, ``newer_schema`` (a main-header refusal hint),
    ``missing``, ``needs_journal_recovery``, ``corrupt``, or ``None`` when the header-level
    checks pass and the caller goes on (open: schema checks; inspect: a copy's integrity).
    ``header_version`` is the main header's schema value when the header is a complete SQLite
    header; ``rollback`` is ``none`` or ``hot``."""

    health: str | None
    header_version: int | None
    rollback: str
    mode_supported: bool


def classify_source(snapshot: admission.Snapshot) -> SourceClass:
    """The one precedence open, inspect and switch share (A04 round 2, decision 0026, R1-M1).

    1. An uncertain rollback-journal header refuses with ``io_error`` (raised here).
    2. A package outside the supported rollback mode (any ``-wal``/``-shm`` file, or a complete
       SQLite header whose format bytes are not the rollback value) is
       ``unsupported_journal_mode``, or ``newer_schema`` when that header names a newer schema.
       It outranks a missing or non-SQLite main file and a hot journal and is never in
       ``DAMAGED``: no route offers a switch or a journal recovery that would then refuse.
    3. Then a missing database, a hot rollback journal (an interrupted write, recovered only on
       a copy) and a main file without a complete SQLite header (``corrupt``).
    """
    rollback = snapshot.rollback_state()
    mode_supported = snapshot.mode_supported()
    database = snapshot.files.get(DB_NAME)
    header = database.read(100) if database is not None else b""
    complete = len(header) == 100 and header.startswith(SQLITE_MAGIC)
    version = int.from_bytes(header[60:64], "big") if complete else None
    health: str | None = None
    if not mode_supported:
        newer = version is not None and version > _supported()
        health = "newer_schema" if newer else "unsupported_journal_mode"
    elif database is None:
        health = "missing"
    elif rollback == "hot":
        health = "needs_journal_recovery"
    elif not complete:
        health = "corrupt"
    return SourceClass(health, version, rollback, mode_supported)


def check_openable(store_dir: Path, project_id: str, *, allow_pending: bool = False) -> None:
    """What ``Service.open`` checks before it reads the store's identity, from the file's bytes
    and the journal only (round 2, F3, F5): a missing, unreadable or non-SQLite file, a hot
    rollback journal (an interrupted write: ``needs_journal_recovery``, recovered into a new
    store, never in place) and a schema this version does not open answer with the recovery
    pointer. No database is opened here and no file is created."""
    with admission.source_snapshot(store_dir) as snapshot:
        found = classify_source(snapshot)
        if found.health == "unsupported_journal_mode":
            raise _mode_error(project_id, hot=found.rollback == "hot")
        if found.health == "newer_schema":
            # A main-header hint can refuse newer software without proving WAL identity.
            assert found.header_version is not None
            raise _schema_error(project_id, found.header_version)
        if found.health == "missing":
            raise recovery_error(project_id, "missing", "its database file is missing")
        if found.health == "needs_journal_recovery":
            raise recovery_error(
                project_id,
                "needs_journal_recovery",
                "an interrupted write left a rollback journal; recover it into a new store with "
                "`prometheos maintenance recover-journal` (it is never applied in place)",
            )
        if found.health == "corrupt":
            raise recovery_error(
                project_id, "corrupt", "its database file is not a SQLite database"
            )
        version = found.header_version
        assert version is not None
        if version > _supported():
            raise _schema_error(project_id, version)
        if version == 0:
            raise recovery_error(project_id, "not_a_store", "it is not a PrometheOS store")
        if version != _supported() and not (allow_pending and version < _supported()):
            raise _schema_error(project_id, version)


def open_failure(project_id: str, exc: BaseException) -> PrometheosError:
    """Public context for source admission/identity failures; typed refusals stay typed."""
    if isinstance(exc, PrometheosError):
        if "recovery" in exc.details:
            if exc.details.get("project_id") != "unknown":
                return exc
            details = dict(exc.details)
            details["project_id"] = project_id
            pointer = dict(details["recovery"])
            pointer.update(recovery_pointer(project_id))
            if "migrate" in pointer:
                pointer["migrate"] = f"prometheos maintenance migrate --project {project_id}"
            details["recovery"] = pointer
            return PrometheosError(exc.code, exc.message, details)
        if exc.code in ("busy", "io_error", "unsupported"):
            details = dict(exc.details)
            details.update(project_id=project_id, recovery=recovery_pointer(project_id))
            return PrometheosError(exc.code, exc.message, details)
        if exc.code == "not_found":
            return recovery_error(project_id, "missing", "its database file is missing")
        return recovery_error(project_id, "corrupt", exc.message)
    if isinstance(exc, OSError):
        why = f"its database file cannot be read ({exc.strerror or type(exc).__name__})"
        return recovery_error(project_id, "unreadable", why)
    text = str(exc)
    if "locked" in text.lower() or "busy" in text.lower():
        return PrometheosError(
            "busy",
            "the project store is busy; retry shortly",
            {"project_id": project_id, "recovery": recovery_pointer(project_id)},
        )
    return recovery_error(project_id, "corrupt", f"its database is unreadable: {_first_line(text)}")


def database_error(project_id: str | None, exc: sqlite3.DatabaseError) -> PrometheosError:
    """A transport's answer to a store that fails while it is being read (after it opened)."""
    text = str(exc)
    if "locked" in text.lower() or "busy" in text.lower():
        return PrometheosError("busy", "the project store is busy; retry shortly", {})
    details: dict[str, Any] = {"recovery": recovery_pointer(project_id)}
    if project_id:
        details["project_id"] = project_id
    return PrometheosError(
        "io_error",
        f"the project store could not be read ({_first_line(text)}); if this persists, inspect "
        "it with the maintenance service (prometheos maintenance inspect)",
        details,
    )


def file_error(exc: OSError) -> PrometheosError:
    """A transport's typed answer to a file-system failure (round 2, F4): never a bare tool
    error or a 500, and never the path (only the operating system's reason)."""
    return PrometheosError(
        "io_error", f"file system error: {exc.strerror or type(exc).__name__}", {}
    )


def scratch_error(exc: OSError) -> PrometheosError:
    return PrometheosError(
        "io_error",
        "the PrometheOS home's scratch folder could not hold a copy of the database "
        f"({exc.strerror or type(exc).__name__}); free space there and retry",
        {"where": "scratch"},
    )


# ----- labels, agent views and files -----------------------------------------------------


def store_label(path: str | Path, home: Path) -> str:
    """A store location without the local path: under the PrometheOS home it is shown relative
    to ``<PrometheOS home>``; elsewhere only its folder name, under ``<local path>``."""
    absolute = os.path.abspath(str(path))
    base = os.path.abspath(str(home))
    pure = PureWindowsPath if os.name == "nt" else Path
    try:
        relative = pure(absolute).relative_to(pure(base))
    except ValueError:
        name = (pure(absolute).name or "store")[:200]
        return f"{LOCAL_LABEL}/{name}"
    text = relative.as_posix()
    return HOME_LABEL if text in ("", ".") else f"{HOME_LABEL}/{text}"[:600]


def for_agent(value: Any) -> Any:
    """A maintenance reply as an agent is shown it (round 2, F9; decision 0012 item 47): every
    path-valued field with an absolute value is left out. Labels, archive names and digests
    stay, and labels and names are what every maintenance route accepts back."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if is_path_key(str(key)):
                if isinstance(item, str) and ABSOLUTE.match(item):
                    continue
                if isinstance(item, list):
                    item = [v for v in item if not (isinstance(v, str) and ABSOLUTE.match(v))]
            out[key] = for_agent(item)
        return out
    if isinstance(value, list):
        return [for_agent(v) for v in value]
    return value


def db_digest(store_dir: Path) -> str:
    """The sha256 of ``record.sqlite`` (read only), or ``missing``. An unreadable file is a
    typed refusal: it cannot supply the exact digest required by a maintenance grant."""
    with admission.source_snapshot(store_dir) as snapshot:
        database = snapshot.files.get(DB_NAME)
        return database.digest() if database is not None else "missing"


def lock_state(store_dir: Path) -> str:
    """Probe the maintenance lock with a read-only handle; the lock file is never created."""
    path = store_dir / "maintenance.lock"
    if not path.is_file():
        return "free"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return "unknown"
    try:
        if lock_mod._try_lock(fd):
            lock_mod._unlock(fd)
            return "free"
        return "held"
    finally:
        os.close(fd)


def journal_state(store_dir: Path) -> str:
    """Fresh ``none``/``hot`` from a guarded source; activity or uncertainty raises busy.

    No diagnostic impersonates a SQLite writer. Existing writable handles (including
    idle connections) conservatively refuse inspection (decision 0025).
    """
    with admission.source_snapshot(store_dir) as snapshot:
        return snapshot.journal_state()


class _SourceUnreadable(Exception):
    def __init__(self, cause: OSError) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _copy_file(source: Path, destination: Path) -> str:
    """Copy ``source`` into the scratch ``destination``, hashing it on the way. A failure to
    read the source is :class:`_SourceUnreadable`; a failure to write the copy is a typed
    scratch ``io_error`` (round 2, F4: the store is not called unreadable for it)."""
    digest = hashlib.sha256()
    try:
        reader = open(source, "rb")
    except OSError as exc:
        raise _SourceUnreadable(exc) from exc
    with reader:
        try:
            writer = open(destination, "xb")
        except OSError as exc:
            raise scratch_error(exc) from exc
        with writer:
            while True:
                try:
                    chunk = reader.read(CHUNK)
                except OSError as exc:
                    raise _SourceUnreadable(exc) from exc
                if not chunk:
                    break
                digest.update(chunk)
                try:
                    writer.write(chunk)
                except OSError as exc:
                    raise scratch_error(exc) from exc
    return digest.hexdigest()


def _copy_database(store_dir: Path, staging: Path, *, journal: bool) -> tuple[str, int]:
    """Copy ``record.sqlite`` (and a write-ahead log; with ``journal`` the rollback journal)
    into ``staging``; retried when the file changes while it is read."""
    with admission.source_snapshot(store_dir) as snapshot:
        if not snapshot.mode_supported():
            raise _mode_error("unknown")
        for leftover in staging.glob(DB_NAME + "*"):
            leftover.unlink()
        database = snapshot.files.get(DB_NAME)
        if database is None:
            raise _SourceUnreadable(FileNotFoundError("the source database is missing"))
        digest = _copy_held(database, staging / DB_NAME)
        for suffix in ("-wal", "-journal") if journal else ("-wal",):
            extra = snapshot.files.get(DB_NAME + suffix)
            if extra is not None:
                _copy_held(extra, staging / (DB_NAME + suffix))
        snapshot.validate()
        return digest, (staging / DB_NAME).stat().st_size


def _copy_held(source: admission.RawFile, destination: Path) -> str:
    digest = hashlib.sha256()
    source.reader.seek(0)
    try:
        writer = open(destination, "xb")
    except OSError as exc:
        raise scratch_error(exc) from exc
    with writer:
        while True:
            try:
                chunk = source.reader.read(CHUNK)
            except OSError as exc:
                raise _SourceUnreadable(exc) from exc
            if not chunk:
                break
            digest.update(chunk)
            try:
                writer.write(chunk)
            except OSError as exc:
                raise scratch_error(exc) from exc
    return digest.hexdigest()


def _probe_copy(copy: Path, database: dict[str, Any], project_id: str, supported: int) -> str:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(copy, isolation_level=None)
        rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        database["schema_version"] = version
        if rows != ["ok"]:
            database.update(integrity="corrupt", integrity_detail="; ".join(rows)[:300])
            return "corrupt"
        database["integrity"] = "ok"
        try:
            state = conn.execute("SELECT project_id, project_seq FROM project_state").fetchone()
        except sqlite3.DatabaseError:
            state = None
        if state is None:
            database["integrity_detail"] = "not a PrometheOS store (no project state)"
            return "not_a_store"
        database.update(project_id=str(state[0]), project_seq=int(state[1]))
        if state[0] != project_id:
            database["integrity_detail"] = "the store holds a different project"
            return "wrong_project"
        if version > supported:
            return "newer_schema"
        if version < supported:
            return "needs_migration"
        return "ok"
    except sqlite3.DatabaseError as exc:
        database.update(integrity="corrupt", integrity_detail=_first_line(str(exc)))
        return "corrupt"
    finally:
        if conn is not None:
            conn.close()


def source_binding(snapshot: admission.Snapshot) -> dict[str, dict[str, Any]]:
    """The exact source binding of a maintenance plan: per present file, its physical identity
    (as decimal strings: an NTFS file reference carries its sequence number in the high bits
    and often exceeds the JSON safe-integer range), digest and size, read under the guard."""
    return {
        name: {
            "identity": [str(part) for part in file.key],
            "sha256": file.digest(),
            "bytes": file.signature()[2],
        }
        for name, file in snapshot.files.items()
    }


def probe_store(store_dir: Path, scratch: Path, project_id: str) -> dict[str, Any]:
    """Health of one store from its files: the database is read from scratch copies only."""
    with admission.source_snapshot(store_dir) as snapshot:
        result = _probe_store_guarded(store_dir, scratch, project_id, snapshot)
        result["database"]["source_binding"] = source_binding(snapshot)
        return result


def _probe_store_guarded(
    store_dir: Path, scratch: Path, project_id: str, snapshot: admission.Snapshot
) -> dict[str, Any]:
    supported = _supported()
    found = classify_source(snapshot)
    jstate, mode_supported = found.rollback, found.mode_supported
    database: dict[str, Any] = {
        "present": False,
        "bytes": None,
        "sha256": "missing",
        "integrity": "missing",
        "integrity_detail": None,
        "schema_version": None,
        "supported_schema_version": supported,
        "project_id": None,
        "project_seq": None,
        "journal": {
            "wal": DB_NAME + "-wal" in snapshot.files,
            "rollback_journal": DB_NAME + "-journal" in snapshot.files,
            "state": jstate,
            "mode_supported": mode_supported,
        },
        "journal_recovery": None,
    }
    original = snapshot.files.get(DB_NAME)
    if not mode_supported:
        # Shared precedence: outranks a missing or non-SQLite main file and a hot journal.
        version = found.header_version
        newer = found.health == "newer_schema"
        if original is not None:
            database.update(
                present=True,
                bytes=original.signature()[2],
                sha256=original.digest(),
                integrity="unreadable",
                integrity_detail="not checked: unsupported journal mode; no WAL identity verified",
            )
        else:
            database["integrity_detail"] = (
                "no record.sqlite; not checked: unsupported journal-mode side files are present"
            )
        database.update(
            integrity_checked=False,
            schema_version=version if newer else None,
            header_schema_version=version,
            schema_version_source="main_header_hint" if newer else None,
        )
        if jstate == "hot":
            database["journal_recovery"] = {
                "health": "unsupported_journal_mode",
                "integrity": "unreadable",
            }
        assert found.health is not None
        return {"health": found.health, "opens": False, "database": database}
    if original is None:
        detail = "the store folder is missing" if not store_dir.is_dir() else "no record.sqlite"
        database["integrity_detail"] = detail
        return {"health": "missing", "opens": False, "database": database}
    database["present"] = True
    staging = scratch / f"inspect-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True)
    except OSError as exc:
        raise scratch_error(exc) from exc
    try:
        try:
            digest, size = _copy_database(store_dir, staging, journal=False)
        except _SourceUnreadable as exc:
            database.update(integrity="unreadable", sha256="unreadable")
            reason = exc.cause.strerror or type(exc.cause).__name__
            database["integrity_detail"] = f"cannot read the database file ({reason})"
            return {"health": "unreadable", "opens": False, "database": database}
        database.update(sha256=digest, bytes=size)
        health = _probe_copy(staging / DB_NAME, database, project_id, supported)
        if found.health == "corrupt" and health != "corrupt":
            # SQLite reads an empty or headerless file as an empty database; open's header
            # check calls it corrupt, and so does inspect (one precedence).
            database.update(
                integrity="corrupt",
                integrity_detail="not a SQLite database (no complete SQLite header)",
                schema_version=None,
                project_id=None,
                project_seq=None,
            )
            health = "corrupt"
        if jstate == "hot":
            # What SQLite's own rollback would give, applied to a second copy only.
            recovered_dir = staging / "with-journal"
            recovered_dir.mkdir()
            recovered: dict[str, Any] = {"integrity": "missing", "integrity_detail": None,
                                         "schema_version": None, "project_id": None,
                                         "project_seq": None}  # fmt: skip
            try:
                _copy_database(store_dir, recovered_dir, journal=True)
                after = _probe_copy(recovered_dir / DB_NAME, recovered, project_id, supported)
                recovered["health"] = after
            except _SourceUnreadable:
                recovered["health"] = "unreadable"
            database["journal_recovery"] = recovered
            health = "needs_journal_recovery"
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"health": health, "opens": health == "ok", "database": database}


def _signature(store_dir: Path) -> tuple[Any, ...]:
    """What a cached probe result is valid for: the store's database files as they are."""
    with admission.source_snapshot(store_dir) as snapshot:
        parts: list[Any] = [
            os.path.abspath(store_dir),
            snapshot.rollback_state(),
            snapshot.mode_supported(),
        ]
        for name in (DB_NAME, *(DB_NAME + side for side in admission.SIDES)):
            file = snapshot.files.get(name)
            parts.append((name, file.signature() if file is not None else None))
        return tuple(parts)


_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_RUNNING: dict[tuple[Any, ...], threading.Event] = {}


def probe_cached(
    store_dir: Path, scratch: Path, project_id: str, *, refresh: bool = False
) -> dict[str, Any]:
    """:func:`probe_store` for inspect (round 2, F11): a store whose files did not change is
    not copied and checked again, and one probe per store state runs at a time; concurrent
    callers wait for it."""
    flight = (os.path.abspath(store_dir),)
    while True:
        key = (*_signature(store_dir), project_id)
        with _CACHE_LOCK:
            if not refresh and key in _CACHE:
                return deepcopy(_CACHE[key])
            running = _RUNNING.get(flight)
            if running is None:
                running = _RUNNING[flight] = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            running.wait(30)
            if running.is_set():
                refresh = False  # this caller shares the fresh probe it waited for
            continue
        try:
            result = probe_store(store_dir, scratch, project_id)
            with _CACHE_LOCK:
                if len(_CACHE) > 64:
                    _CACHE.clear()
                # Access failures can clear without changing file metadata (for example a
                # byte-range lock). Never keep an unreadable answer after the lock clears.
                if result["health"] != "unreadable" and _signature(store_dir) == key[:-1]:
                    _CACHE[key] = deepcopy(result)
            return result
        finally:
            with _CACHE_LOCK:
                _RUNNING.pop(flight, None)
            running.set()


def _record_integrity(health: str, integrity: str, digest: str) -> str:
    if digest == "missing":
        return digest
    return integrity if integrity in INTEGRITY else "unreadable"


def _listed(folder: Path, source: str) -> list[dict[str, Any]]:
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and BACKUP_NAME.fullmatch(path.name):
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            found.append(
                {"name": path.name, "bytes": stat.st_size, "modified_at": modified,
                 "source": source}
            )  # fmt: skip
    return found


# ----- the service -----------------------------------------------------------------------


class Maintenance:
    """Bound to one PrometheOS home (the ``Service``'s). Never opens a project database except
    the one ``migrate`` call an owner act authorises (after its health check)."""

    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self.home: Path = svc.home
        self.registry: Registry = svc.registry

    # ----- helpers -------------------------------------------------------------------------

    def _scratch(self) -> Path:
        folder = self.home / "scratch"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise scratch_error(exc) from exc
        return folder

    def label(self, path: str | Path) -> str:
        return store_label(path, self.home)

    def _entry(self, project_id: str) -> dict[str, Any]:
        if not is_record_id(project_id, "project"):
            raise PrometheosError("validation_error", "malformed project id", {})
        return dict(self.registry.entry(project_id))

    def _store(self, project_id: str) -> Path:
        return Path(str(self._entry(project_id)["store_path"]))

    def managed_dir(self, project_id: str) -> Path:
        """``<home>/stores/<first 8 hex of the project UUID>``: where PrometheOS creates the
        stores it restores and the backups it recovers for an agent (round 2, F1). Short, so
        restored media paths stay within the Windows path limit without long-path support."""
        return self.home / MANAGED / project_id.removeprefix("project_")[:8]

    def is_managed_target(self, project_id: str, target: Path) -> bool:
        absolute = Path(os.path.abspath(target))
        managed = Path(os.path.abspath(self.managed_dir(project_id)))
        return (
            absolute.parent == managed
            and absolute.name.startswith("restored-")
            and absolute.resolve().is_relative_to(self.home.resolve())
            and absolute.resolve().parent == managed.resolve()
        )

    def _managed_root(self, project_id: str) -> Path:
        managed = self.managed_dir(project_id)
        self._managed_destination(project_id, managed)
        managed.mkdir(parents=True, exist_ok=True)
        return managed

    def _managed_destination(self, project_id: str, path: Path) -> Path:
        """Resolve existing children too: a home-labelled parent cannot authorize an archive,
        its .part file or recovery staging through a junction outside the managed area."""
        managed = self.managed_dir(project_id).resolve()
        if not managed.is_relative_to(self.home.resolve()) or not path.resolve().is_relative_to(
            managed
        ):
            raise PrometheosError(
                "policy_denied", "managed recovery output leaves the PrometheOS managed area", {}
            )
        return path

    def _check_restore_capacity(self, managed: Path) -> None:
        known = {p.resolve() for p in self._known_stores()}
        count = sum(p.is_dir() and p.resolve() not in known for p in managed.glob("restored-*"))
        if count >= MAX_UNSWITCHED_RESTORES:
            raise PrometheosError(
                "policy_denied",
                f"{count} restored stores remain unswitched; the owner must remove unused "
                "restores before another agent restore",
                {"limit": MAX_UNSWITCHED_RESTORES},
            )

    def resolve_backup(self, project_id: str, backup: str) -> Path:
        """A backup is named by its file name (in the current store's ``exports/`` folder or
        among the backups PrometheOS recovered in the managed area: what the recovery screen
        offers) or given as an absolute path (CLI, MCP, agent HTTP)."""
        if not isinstance(backup, str) or not backup or len(backup) > 4096:
            raise PrometheosError("validation_error", "name a backup archive", {})
        if BACKUP_NAME.fullmatch(backup):
            path = self._store(project_id) / "exports" / backup
            if not path.is_file():
                path = self._managed_destination(
                    project_id, self.managed_dir(project_id) / "backups" / backup
                )
        elif os.path.isabs(backup):
            path = Path(backup)
            if Path(os.path.abspath(path)).is_relative_to(
                Path(os.path.abspath(self.managed_dir(project_id)))
            ):
                self._managed_destination(project_id, path)
        else:
            raise PrometheosError(
                "validation_error",
                "a backup is an archive name from maintenance inspect or an absolute path",
                {},
            )
        if not path.is_file():
            raise PrometheosError("not_found", "backup archive not found", {"backup": path.name})
        return path

    def resolve_location(self, value: str | Path) -> Path:
        """A store location: an absolute path, or a label under the home as maintenance
        replies show it (``<PrometheOS home>/stores/...``). One rule on every transport
        (round 2, F8)."""
        text = str(value)
        if text.startswith(HOME_LABEL + "/"):
            parts = text[len(HOME_LABEL) + 1 :].split("/")
            if any(p in ("", ".", "..") or "\\" in p or ":" in p for p in parts):
                raise PrometheosError("validation_error", "malformed store label", {})
            return Path(os.path.abspath(self.home.joinpath(*parts)))
        if not text or not os.path.isabs(text):
            raise PrometheosError(
                "validation_error",
                "name a store by its absolute location or its <PrometheOS home> label",
                {},
            )
        return Path(os.path.abspath(text))

    def _quarantine_view(self, record: StoreQuarantine) -> dict[str, Any]:
        return record.model_dump(mode="json") | {
            "damaged_store_label": self.label(record.damaged_store_path),
            "replacement_store_label": self.label(record.replacement_store_path),
        }

    def probe(self, project_id: str) -> dict[str, Any]:
        """The registered store's health (fresh, from scratch copies)."""
        return probe_store(self._store(project_id), self._scratch(), project_id)

    # ----- inspect -------------------------------------------------------------------------

    def inspect(
        self, project_id: str, rank: int, quarantine_id: str | None = None, *, refresh: bool = False
    ) -> dict[str, Any]:
        """Integrity, schema version and lock state of the registered store (or of one
        quarantined store), its backup archives, the visible maintenance approvals and the
        quarantine records, plus the environment report of ``doctor``."""
        entry = self._entry(project_id)
        current = Path(str(entry["store_path"]))
        records = self.registry.quarantine_records(project_id)
        kind = "current"
        store = current
        if quarantine_id is not None:
            match = next((r for r in records if r.id == quarantine_id), None)
            if match is None:
                raise PrometheosError(
                    "not_found", "quarantine record not found", {"quarantine_id": quarantine_id}
                )
            store, kind = Path(match.damaged_store_path), "quarantined"
        probed = probe_cached(store, self._scratch(), project_id, refresh=refresh)
        return {
            "project_id": project_id,
            "title": entry.get("title"),
            "health": probed["health"],
            "opens": probed["opens"],
            "store": {
                "kind": kind,
                "quarantine_id": quarantine_id,
                "path": str(store),
                "label": self.label(store),
                "folder_present": store.is_dir(),
            },
            "database": probed["database"],
            "lock": {"maintenance": lock_state(store) if store.is_dir() else "free"},
            "backups": self.backup_candidates(project_id, current),
            "approvals": [self.view(r) for r in self._listing(project_id, rank)],
            "quarantine": [self._quarantine_view(r) for r in records],
            "environment": self.svc.doctor(None),
            "recovery": recovery_pointer(project_id),
        }

    def backup_candidates(self, project_id: str, store: Path) -> list[dict[str, Any]]:
        """Archive names only, from the store's ``exports/`` folder (read as a folder listing;
        the store's database is never opened) and the managed recovered backups."""
        recovered = self._managed_destination(project_id, self.managed_dir(project_id) / "backups")
        # A child archive may itself be redirected even when the backups directory is safe.
        for archive in recovered.glob("*.zip"):
            self._managed_destination(project_id, archive)
        return _listed(store / "exports", "store exports") + _listed(recovered, "recovered")

    # ----- verify and restore --------------------------------------------------------------

    def verify(self, project_id: str, backup: str) -> dict[str, Any]:
        path, sha, manifest = self._verify_full(project_id, backup)
        return self._verified(path, sha, manifest)

    def _verify_full(self, project_id: str, backup: str) -> tuple[Path, str, dict[str, Any]]:
        """Paths, sizes, digests, schema and database integrity in a scratch staging folder,
        and the project the archive belongs to (``store.backup.verify_into``)."""
        path = self.resolve_backup(project_id, backup)
        before = backup_mod._sha_file(path)
        manifest = backup_mod.verify_backup(path, self._scratch(), expect_project_id=project_id)[
            "manifest"
        ]
        if backup_mod._sha_file(path) != before:
            raise PrometheosError("busy", "the backup archive changed while it was verified", {})
        return path, before, manifest

    def _verified(self, path: Path, sha: str, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "verified": True,
            "project_id": manifest["project_id"],
            "backup": {
                "name": path.name,
                "path": str(path),
                "label": self.label(path),
                "sha256": sha,
                "bytes": path.stat().st_size,
            },
            "backup_manifest_sha256": canonical_sha256(manifest),
            "manifest": {
                "created_at": manifest["created_at"],
                "software_version": manifest["software_version"],
                "project_seq": manifest["project_seq"],
                "schema_version": manifest["schema_version"],
                "database_sha256": manifest["database"]["sha256"],
                "media_count": len(manifest["media"]),
                "media_bytes": sum(int(m["bytes"]) for m in manifest["media"]),
                "reference_only_count": len(manifest["reference_only_not_included"]),
            },
        }

    def created_view(self, made: dict[str, Any]) -> dict[str, Any]:
        """A backup ``Service.backup`` just wrote and verified, as the maintenance routes show
        it: the archive's name and label (the hub removes the path), digests and summary."""
        path = Path(str(made["backup"]))
        return {"created": True, **self._verified(path, str(made["sha256"]), made["manifest"])}

    def _known_stores(self) -> list[Path]:
        paths = []
        for row in self.registry.entries():
            paths.append(Path(str(row["store_path"])))
            for record in self.registry.quarantine_records(str(row["project_id"])):
                paths += [Path(record.damaged_store_path), Path(record.replacement_store_path)]
        return paths

    def _check_target(self, target: Path, declare_local: bool) -> Path:
        report = check_store_location(target, declare_local)
        if not report.accepted:
            raise PrometheosError("policy_denied", report.reason, {})
        absolute = Path(os.path.abspath(report.path))
        if absolute.exists():
            raise PrometheosError(
                "validation_error", "restore target already exists; restore never overwrites", {}
            )
        for store in self._known_stores():
            known = Path(os.path.abspath(store))
            if absolute == known or known in absolute.parents:
                raise PrometheosError(
                    "validation_error",
                    "the restore target is inside an existing or quarantined store; choose a new "
                    "folder",
                    {},
                )
        return absolute

    def default_target(self, project_id: str) -> Path:
        """A new folder PrometheOS creates: ``<home>/stores/<id>/restored-<stamp>-<random>``."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        return self.managed_dir(project_id) / f"restored-{stamp}-{uuid.uuid4().hex[:6]}"

    def restore(
        self,
        project_id: str,
        backup: str,
        into: Path | str | None = None,
        *,
        declare_local: bool = False,
        owner: object = None,
    ) -> dict[str, Any]:
        """Verify into a staging folder beside a new target, then rename it into place. The
        registry is not changed; the damaged store is never opened or written. Only an owner
        act names the location (``into``, ``declare_local``); any other caller's restore lands
        in a new folder PrometheOS creates under the home (round 2, F1)."""
        if (into is not None or declare_local) and not isinstance(owner, OwnerAct):
            raise PrometheosError(
                "policy_denied",
                "an agent restores only into a new store PrometheOS creates under the PrometheOS "
                "home (<PrometheOS home>/stores/...); naming a location is the owner's choice "
                "(an interactive terminal or the paired hub)",
                {},
            )
        damaged = self._store(project_id)
        path = self.resolve_backup(project_id, backup)
        chosen = self.default_target(project_id) if into is None else self.resolve_location(into)
        target = self._check_target(chosen, declare_local)
        managed = None if isinstance(owner, OwnerAct) else self._managed_root(project_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        sha = backup_mod._sha_file(path)
        if managed is not None:
            # Serialize the cap and restore across processes using the managed area's lock;
            # the damaged store is never locked or written.
            with lock_mod.maintenance_lock(managed):
                self._check_restore_capacity(managed)
                manifest, _ = backup_mod.restore_backup(path, target, expect_project_id=project_id)
        else:
            manifest, _ = backup_mod.restore_backup(path, target, expect_project_id=project_id)
        verified = self._verified(path, sha, manifest)
        return {
            "restored": True,
            "registry_switched": False,
            "project_id": project_id,
            "target_store_path": str(target),
            "target_store_label": self.label(target),
            "target_managed": self.is_managed_target(project_id, target),
            "damaged_store_label": self.label(damaged),
            "backup": verified["backup"],
            "backup_manifest_sha256": verified["backup_manifest_sha256"],
            "manifest": verified["manifest"],
            "next": (
                "switch the registry to this store: maintenance switch (an agent's request waits "
                "for the owner's grant on the recovery screen or in `prometheos maintenance "
                "review`)"
            ),
        }

    def journal_recover(self, project_id: str) -> dict[str, Any]:
        """Recover a hot rollback journal into a NEW store (round 2, F3): the store's database,
        journal and media are copied into staging in the managed area, SQLite rolls the journal
        back into the copy, the copy must pass its integrity check and hold this project, and
        it becomes a verified backup (``<home>/stores/<id>/backups/journal-recovered-*.zip``)
        that is restored like any other. The damaged store is never written."""
        store = self._store(project_id)
        probed = probe_store(store, self._scratch(), project_id)
        if not probed["database"]["journal"]["mode_supported"]:
            raise _mode_error(project_id)
        if probed["health"] != "needs_journal_recovery":
            raise PrometheosError(
                "validation_error",
                f"this store has no interrupted write to recover (its health is "
                f"{probed['health']})",
                {"store_health": probed["health"]},
            )
        managed = self.managed_dir(project_id)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        name = f"journal-recovered-{stamp}-{uuid.uuid4().hex[:6]}.zip"
        out = managed / "backups" / name
        staging = managed / f".journal-{uuid.uuid4().hex[:8]}"
        destinations = [
            staging,
            *(staging / sub for sub in SUBDIRS),
            out.parent,
            out,
            out.with_name(out.name + ".part"),
        ]
        for destination in destinations:
            self._managed_destination(project_id, destination)
        if staging.exists():
            raise PrometheosError("busy", "recovery staging already exists; retry", {})
        try:
            with admission.source_snapshot(store) as snapshot:
                if not snapshot.mode_supported():
                    raise _mode_error(project_id)
                if lock_state(store) != "free":
                    raise PrometheosError("busy", "source maintenance is unavailable; retry", {})
                current = probe_store(store, self._scratch(), project_id)
                if current["health"] != "needs_journal_recovery":
                    raise PrometheosError("busy", "the source journal changed; inspect again", {})
                try:
                    managed.mkdir(parents=True, exist_ok=True)
                    staging.mkdir()
                    for sub in SUBDIRS:
                        (staging / sub).mkdir(parents=True, exist_ok=True)
                    out.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise scratch_error(exc) from exc
                for destination in destinations:
                    self._managed_destination(project_id, destination)
                try:
                    _copy_database(store, staging, journal=True)
                except _SourceUnreadable as exc:
                    raise file_error(exc.cause) from exc
                if (store / "media").is_dir():
                    shutil.copytree(store / "media", staging / "media", dirs_exist_ok=True)
            conn = sqlite3.connect(staging / DB_NAME, isolation_level=None)
            try:  # the read-write open rolls the journal back into the COPY
                rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                state = conn.execute("SELECT project_id, project_seq FROM project_state").fetchone()
            except sqlite3.DatabaseError as exc:
                raise PrometheosError(
                    "validation_error",
                    f"the journal does not recover a sound store ({_first_line(str(exc))}); "
                    "restore a backup instead",
                    {},
                ) from exc
            finally:
                conn.close()
            if rows != ["ok"] or state is None or state[0] != project_id:
                raise PrometheosError(
                    "validation_error",
                    "the journal does not recover a sound store of this project; restore a "
                    "backup instead",
                    {},
                )
            if version > _supported():
                raise _schema_error(project_id, version)
            for destination in destinations:
                self._managed_destination(project_id, destination)
            backup_mod.create_backup(
                ProjectStore(staging, _scratch_copy=True), out, allow_pending=True
            )
            self._managed_destination(project_id, out)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        restored = self.restore(project_id, name)
        return restored | {
            "journal_recovered": True,
            "recovered_project_seq": int(state[1]),
            "recovered_schema_version": version,
        }

    # ----- switch: plan, verification and the registry transaction -----------------------------

    def switch_plan(
        self, project_id: str, backup: str, target: str, *, agent: bool
    ) -> dict[str, Any]:
        """The exact plan of a registry switch, computed only after verification: the registered
        store does not open (round 2, F2), the backup verifies (in staging) and belongs to the
        project, and the target's database is the backup's verified database byte for byte,
        with every media file matching its digest. An agent's target must be a store
        PrometheOS restored in the managed area (round 2, F1)."""
        entry = self._entry(project_id)
        damaged = Path(str(entry["store_path"]))
        target_dir = self.resolve_location(target)
        if target_dir == Path(os.path.abspath(damaged)):
            raise PrometheosError(
                "validation_error", "the target is the registered store; restore into a new one", {}
            )
        managed = self.is_managed_target(project_id, target_dir)
        if agent and not managed:
            raise PrometheosError(
                "policy_denied",
                "an agent's switch target is a store PrometheOS restored under "
                "<PrometheOS home>/stores/ (maintenance restore without a location)",
                {},
            )
        probed = probe_store(damaged, self._scratch(), project_id)
        if not probed["database"]["journal"]["mode_supported"]:
            raise _mode_error(project_id, hot=probed["database"]["journal"]["state"] == "hot")
        health = str(probed["health"])
        if health == "unreadable":
            raise recovery_error(
                project_id, health, "its database cannot be read to bind a recovery request"
            )
        if health not in DAMAGED:
            hint = {
                "needs_migration": "migrate it (maintenance migrate)",
                "newer_schema": "open it with the newer PrometheOS that wrote it",
            }.get(
                health,
                "a deliberate rollback to a backup is the owner's restore: `prometheos backup "
                "restore` (it asks to confirm the rollback)",
            )
            raise PrometheosError(
                "policy_denied",
                f"the registered store is not damaged (its health is {health}); a switch only "
                f"replaces a store that does not open. Instead, {hint}",
                {"store_health": health},
            )
        path, sha, manifest = self._verify_full(project_id, backup)
        verified = self._verified(path, sha, manifest)
        manifest_db = verified["manifest"]["database_sha256"]
        self._check_restored(target_dir, manifest, manifest_db)
        backup_view = verified["backup"]
        digest = db_digest(damaged)
        database = probed["database"]
        return {
            "action": "registry_switch",
            "project_id": project_id,
            "damaged_store_path": str(damaged),
            "damaged_store_label": self.label(damaged),
            "damaged_db_sha256": digest,
            "source_binding": database["source_binding"],
            "store_health": health,
            "store_integrity": _record_integrity(health, str(database["integrity"]), digest),
            "store_schema_version": database["schema_version"],
            "store_project_seq": database["project_seq"],
            "backup_path": backup_view["path"],
            "backup_name": backup_view["name"],
            "backup_sha256": backup_view["sha256"],
            "backup_manifest_sha256": verified["backup_manifest_sha256"],
            "backup_created_at": verified["manifest"]["created_at"],
            "project_seq": verified["manifest"]["project_seq"],
            "schema_version": verified["manifest"]["schema_version"],
            "target_store_path": str(target_dir),
            "target_store_label": self.label(target_dir),
            "target_managed": managed,
            "target_db_sha256": manifest_db,
            "target_resolved_path": str(target_dir.resolve()),
            "target_media": manifest["media"],
        }

    def _check_restored(self, target: Path, manifest: dict[str, Any], manifest_db: str) -> None:
        self._check_target_side_files(target)
        if not (target / DB_NAME).resolve().is_relative_to(target.resolve()):
            raise PrometheosError("validation_error", "restored database leaves its store", {})
        if not (target / DB_NAME).is_file():
            raise PrometheosError(
                "validation_error",
                "the target holds no restored store; restore the backup into it first",
                {},
            )
        if db_digest(target) != manifest_db:
            raise PrometheosError(
                "validation_error",
                "the target's database is not the verified backup's database; restore again",
                {},
            )
        self._check_media(target, manifest["media"])

    def _check_target_side_files(self, target: Path) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            side_file = target / (DB_NAME + suffix)
            if side_file.exists() or side_file.is_symlink():
                raise PrometheosError(
                    "validation_error",
                    "the restored target has unverified SQLite side files; restore again",
                    {},
                )

    def _check_media(self, target: Path, entries: list[dict[str, Any]]) -> None:
        for media in entries:
            file = target / media["path"]
            if not file.resolve().is_relative_to(target.resolve()):
                raise PrometheosError("validation_error", "restored media leaves its store", {})
            if not file.is_file() or backup_mod._sha_file(file) != media["sha256"]:
                raise PrometheosError(
                    "validation_error",
                    "a restored media file is missing or does not match its digest; restore again",
                    {},
                )

    def _summary(self, plan: dict[str, Any]) -> list[str]:
        if plan["action"] == "migrate":
            return [
                f"migrate the store {plan['store_label']} of project {plan['project_id']} from "
                f"schema {plan['from_schema']} to {plan['to_schema']}",
                "a verified backup of the old schema is written into the store's exports first",
            ]
        seq = plan.get("store_project_seq")
        lines = [
            f"becomes live: the restored store {plan['target_store_label']} (backup "
            f"{plan['backup_name']} of {plan.get('backup_created_at', 'unknown date')}, project "
            f"sequence {plan['project_seq']}, schema {plan['schema_version']}, manifest "
            f"{plan['backup_manifest_sha256'][:12]})",
            f"the current store {plan['damaged_store_label']} is "
            f"{plan.get('store_health', 'damaged')} (latest sequence "
            f"{'unknown' if seq is None else seq}, database {plan['damaged_db_sha256'][:12]}); "
            "it is kept unchanged and listed as quarantined",
        ]
        if not plan.get("target_managed", True):
            lines.append("the restored store is not in the PrometheOS managed stores area")
        return lines

    def switch_direct(
        self, project_id: str, backup: str, target: str, *, owner: object
    ) -> dict[str, Any]:
        """The owner switches at once (paired hub session, interactive terminal)."""
        act = require_owner(owner, "switching a project to a restored store")
        plan = self.switch_plan(project_id, backup, target, agent=False)
        return self._commit_switch(plan, None, act.actor, "owner switched to a verified restore")

    def _commit_switch(
        self,
        plan: dict[str, Any],
        approval: MaintenanceApproval | None,
        switched_by: str,
        reason: str,
    ) -> dict[str, Any]:
        project_id = str(plan["project_id"])
        damaged = Path(str(plan["damaged_store_path"]))
        target = Path(str(plan["target_store_path"]))
        if damaged.is_dir() and lock_state(damaged) == "held":
            raise PrometheosError(
                "busy",
                "a maintenance operation holds the damaged store's lock; retry when it ends",
                {},
            )
        now = approvals.now_ts()
        digest = str(plan["damaged_db_sha256"])
        # The quarantine row is built from the verified plan (round 2, F4): nothing is probed
        # here, so a scratch failure cannot break the transaction.
        record = StoreQuarantine(
            id=new_operational_id("quarantine"),
            project_id=project_id,
            damaged_store_path=str(damaged),
            damaged_db_sha256=digest,
            damaged_schema_version=plan.get("store_schema_version"),
            damaged_integrity=_record_integrity(  # type: ignore[arg-type]
                str(plan.get("store_health")), str(plan.get("store_integrity")), digest
            ),
            replacement_store_path=str(target),
            backup_sha256=str(plan["backup_sha256"]),
            backup_manifest_sha256=str(plan["backup_manifest_sha256"]),
            maintenance_approval_id=None if approval is None else approval.id,
            switched_at=now,
            switched_by=switched_by[:100],
            reason=reason[:2000],
        )
        # Protect against cooperating maintenance operations on the replacement, never by
        # taking/opening the damaged store. Revalidate its resolved location before the lock
        # file is opened; old grants without complete target bindings require a new request.
        self._check_bound_target(target, plan)
        with (
            lock_mod.maintenance_lock(target),
            admission.guarded_sources(damaged, target),
            self.registry.transaction() as conn,
        ):
            _expire_due(conn)
            row = conn.execute(
                "SELECT store_path FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise PrometheosError("not_found", "project is not registered", {})
            if row["store_path"] != str(damaged):
                raise _changed("the registered store")
            if db_digest(damaged) != digest:
                raise _changed("the damaged store's database")
            with admission.source_snapshot(damaged) as snapshot:
                if not snapshot.mode_supported():
                    raise _mode_error(project_id)
                binding = source_binding(snapshot)
                if binding != plan.get("source_binding"):
                    raise _changed("the damaged store's journal or physical identity")
            self._check_bound_target(target, plan)
            if not (target / DB_NAME).resolve().is_relative_to(target.resolve()):
                raise _changed("the restored store's database location")
            if db_digest(target) != plan["target_db_sha256"]:
                raise _changed("the restored store's database")
            try:
                self._check_target_side_files(target)
                self._check_media(target, plan["target_media"])
            except (PrometheosError, OSError) as exc:
                raise _changed("the restored store's contents") from exc
            if approval is not None:
                _use_grant(conn, approval, now)
            conn.execute(
                "UPDATE projects SET store_path = ?, previous_store_path = ?, registered_at = ?, "
                "location_note = ? WHERE project_id = ?",
                (str(target), str(damaged), now, "restored from a verified backup", project_id),
            )
            insert(conn, "store_quarantine", record)
        _forget(damaged)
        return self._switch_result(record, plan)

    def _check_bound_target(self, target: Path, plan: dict[str, Any]) -> None:
        if str(target.resolve()) != plan.get("target_resolved_path") or not isinstance(
            plan.get("target_media"), list
        ):
            raise _changed("the restored store's location or media binding")
        if not (target / "maintenance.lock").resolve().is_relative_to(target.resolve()):
            raise _changed("the restored store's maintenance lock location")

    def _switch_result(
        self, record: StoreQuarantine, plan: dict[str, Any] | None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "switched": True,
            "project_id": record.project_id,
            "store_path": record.replacement_store_path,
            "store_label": self.label(record.replacement_store_path),
            "previous_store_path": record.damaged_store_path,
            "previous_store_label": self.label(record.damaged_store_path),
            "quarantine": self._quarantine_view(record),
            "approval_request_id": record.maintenance_approval_id,
            "restart": "restart a hub or MCP server in maintenance mode to open the project",
        }
        if plan is not None:
            result["verification"] = {
                "backup_sha256": plan["backup_sha256"],
                "backup_manifest_sha256": plan["backup_manifest_sha256"],
                "target_db_sha256": plan["target_db_sha256"],
                "project_seq": plan["project_seq"],
                "schema_version": plan["schema_version"],
            }
        return result

    def check_owner_restore_source(self, project_id: str, target: Path) -> None:
        """Before ``backup restore`` writes anything (A04, R2-L2): the registered store that the
        owner's restore would quarantine must pass source admission and yield its digest,
        because no registry switch may rest on an unavailable observation or an unreadable
        marker (spec/04). A refusal keeps its typed code, carries the project and recovery
        pointer, and leaves no target, staging folder or registry change."""
        try:
            previous = self._store(project_id)
        except PrometheosError as exc:
            if exc.code == "not_found":
                return
            raise
        if Path(os.path.abspath(previous)) == Path(os.path.abspath(target)):
            return
        try:
            probe_store(previous, self._scratch(), project_id)
            db_digest(previous)
        except PrometheosError as exc:
            if exc.code in ("busy", "io_error", "unsupported") and "recovery" not in exc.details:
                details = dict(exc.details)
                details.update(project_id=project_id, recovery=recovery_pointer(project_id))
                raise PrometheosError(exc.code, exc.message, details) from exc
            raise

    def record_owner_restore(
        self,
        project_id: str,
        target: Path,
        *,
        backup: Path,
        backup_sha256: str,
        manifest: dict[str, Any],
        switched_by: str,
    ) -> dict[str, Any] | None:
        """``backup restore`` (an interactive owner act) registered a restored store for a
        project the registry already knew: the previous store is quarantined, never replaced
        silently (decision 0021). Returns the quarantine view, or ``None`` when the project was
        not registered (a restore on a new computer) or the store was restored into its own
        lost location (round 2, F7: the path was absent, so nothing was replaced)."""
        try:
            previous = self._store(project_id)
        except PrometheosError as exc:
            if exc.code == "not_found":
                return None
            raise
        if Path(os.path.abspath(previous)) == Path(os.path.abspath(target)):
            return None
        probed = probe_store(previous, self._scratch(), project_id)
        digest = db_digest(previous)
        now = approvals.now_ts()
        record = StoreQuarantine(
            id=new_operational_id("quarantine"),
            project_id=project_id,
            damaged_store_path=str(previous),
            damaged_db_sha256=digest,
            damaged_schema_version=probed["database"]["schema_version"],
            damaged_integrity=_record_integrity(  # type: ignore[arg-type]
                str(probed["health"]), str(probed["database"]["integrity"]), digest
            ),
            replacement_store_path=str(target),
            backup_sha256=backup_sha256,
            backup_manifest_sha256=canonical_sha256(manifest),
            maintenance_approval_id=None,
            switched_at=now,
            switched_by=switched_by[:100],
            reason=f"owner restored backup {backup.name} (backup restore)"[:2000],
        )
        with self.registry.transaction() as conn:
            row = conn.execute(
                "SELECT store_path FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None or row["store_path"] != str(previous):
                raise _changed("the registered store")
            conn.execute(
                "UPDATE projects SET store_path = ?, previous_store_path = ?, registered_at = ?, "
                "location_note = ? WHERE project_id = ?",
                (str(target), str(previous), now, "restored by the owner", project_id),
            )
            insert(conn, "store_quarantine", record)
        _forget(previous)
        return self._quarantine_view(record)

    # ----- maintenance approvals --------------------------------------------------------------

    def request(
        self,
        project_id: str,
        rank: int,
        actor: str,
        plan: dict[str, Any],
        request_id: str | None,
    ) -> PrometheosError:
        """Store the exact plan (or return an identical open request) and answer
        ``approval_required``. Nothing else changes."""
        if request_id is not None and not is_request_id(request_id):
            raise PrometheosError("validation_error", "request_id must be a canonical UUID4", {})
        plan_sha = canonical_sha256(plan)
        with self.registry.transaction() as conn:
            _expire_due(conn)
            if request_id is None:
                same = conn.execute(
                    "SELECT * FROM maintenance_approvals WHERE project_id = ? AND plan_sha256 = ? "
                    "AND caller_rank = ? AND state IN ('pending','granted') ORDER BY created_at",
                    (project_id, plan_sha, rank),
                ).fetchone()
                if same is not None:
                    return self.answer(from_row(MaintenanceApproval, same))
            else:
                prior = conn.execute(
                    "SELECT * FROM maintenance_approvals WHERE request_id = ?", (request_id,)
                ).fetchone()
                if prior is not None:
                    existing = from_row(MaintenanceApproval, prior)
                    if existing.caller_rank != rank or existing.plan_sha256 != plan_sha:
                        raise PrometheosError(
                            "request_key_conflict",
                            "request_id was already used for a different request",
                            {"request_id": request_id},
                        )
                    return self.answer(existing)
            pending = conn.execute(
                "SELECT COUNT(*) FROM maintenance_approvals WHERE state = 'pending' AND "
                "caller_rank = ?",
                (rank,),
            ).fetchone()[0]
            if pending >= MAX_PENDING_PER_RANK:
                raise PrometheosError(
                    "budget_exceeded",
                    f"{pending} maintenance requests are already pending at this scope",
                    {"pending": pending, "max_pending": MAX_PENDING_PER_RANK},
                )
            created = approvals._now()
            row = MaintenanceApproval(
                id=new_operational_id("mapreq"),
                project_id=project_id,
                damaged_store_path=str(plan["damaged_store_path"]),
                damaged_db_sha256=str(plan["damaged_db_sha256"]),
                backup_manifest_sha256=plan.get("backup_manifest_sha256"),
                target_store_path=plan.get("target_store_path"),
                caller_rank=rank,
                actor=actor[:100],
                request_id=request_id,
                plan=plan,
                plan_sha256=plan_sha,
                state="pending",
                created_at=approvals._ts(created),
                expires_at=approvals._ts(created + timedelta(seconds=DEFAULT_TTL_SECONDS)),
                decided_at=None,
                decided_by=None,
                grant_expires_at=None,
                used_at=None,
                closed_at=None,
            )
            insert(conn, "maintenance_approvals", row)
        return self.answer(row)

    def answer(self, row: MaintenanceApproval) -> PrometheosError:
        if row.state in ("pending", "granted"):
            return required_error(row)
        return PrometheosError(
            "policy_denied",
            f"maintenance request {row.id} is {row.state}; it will not run again. Request again "
            "if it is still needed",
            {"approval_request_id": row.id, "state": row.state},
        )

    def request_switch(
        self,
        project_id: str,
        rank: int,
        actor: str,
        backup: str,
        target: str,
        request_id: str | None,
    ) -> PrometheosError:
        plan = self.switch_plan(project_id, backup, target, agent=rank < 2)
        return self.request(project_id, rank, actor, plan, request_id)

    def migrate_plan(self, project_id: str) -> dict[str, Any]:
        store = self._store(project_id)
        probed = probe_store(store, self._scratch(), project_id)
        if not probed["database"]["journal"]["mode_supported"]:
            raise _mode_error(project_id)
        if probed["health"] != "needs_migration":
            raise PrometheosError(
                "validation_error",
                f"this store does not need a migration (its health is {probed['health']})",
                {"store_health": probed["health"]},
            )
        return {
            "action": "migrate",
            "project_id": project_id,
            "damaged_store_path": str(store),
            "store_label": self.label(store),
            "damaged_db_sha256": db_digest(store),
            "source_binding": probed["database"]["source_binding"],
            "backup_manifest_sha256": None,
            "target_store_path": None,
            "from_schema": probed["database"]["schema_version"],
            "to_schema": _supported(),
        }

    def require_migratable(self, project_id: str) -> None:
        """Before any migration (round 2, F6): the store must be sound and only behind in schema
        (or already current, a no-op). A damaged store, a hot journal or a newer schema is never
        opened read-write by a migration."""
        probed = self.probe(project_id)
        if not probed["database"]["journal"]["mode_supported"]:
            raise _mode_error(project_id)
        if probed["health"] not in ("needs_migration", "ok"):
            raise recovery_error(
                project_id,
                str(probed["health"]),
                f"its health is {probed['health']}, so it is not migrated",
            )

    def request_migrate(
        self, project_id: str, rank: int, actor: str, request_id: str | None
    ) -> PrometheosError:
        return self.request(project_id, rank, actor, self.migrate_plan(project_id), request_id)

    def _listing(self, project_id: str, rank: int) -> list[MaintenanceApproval]:
        if not self.registry.path.exists():
            return []
        with self.registry.transaction() as conn:
            _expire_due(conn)
            rows = conn.execute(
                "SELECT * FROM maintenance_approvals WHERE project_id = ? AND caller_rank <= ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 200",
                (project_id, rank),
            ).fetchall()
        return [from_row(MaintenanceApproval, r) for r in reversed(rows)]

    def _get(self, project_id: str, approval_id: str, rank: int) -> MaintenanceApproval:
        with self.registry.transaction() as conn:
            _expire_due(conn)
            row = _row(conn, approval_id)
        if row is None or row.project_id != project_id or row.caller_rank > rank:
            raise PrometheosError(
                "not_found", "maintenance request not found", {"approval_request_id": approval_id}
            )
        return row

    def view(self, row: MaintenanceApproval) -> dict[str, Any]:
        action = str(row.plan["action"])
        values = {
            "approval_request_id": row.id,
            "operation": OPERATIONS[action],
            "state": row.state,
            "project_id": row.project_id,
            "caller_rank": row.caller_rank,
            "actor": row.actor,
            "request_id": row.request_id,
            "plan": row.plan,
            "plan_sha256": row.plan_sha256,
            "summary": [_printable(s)[:600] for s in self._summary(row.plan)],
            "owner_route": owner_route(row),
            "damaged_store_path": row.damaged_store_path,
            "damaged_store_label": self.label(row.damaged_store_path),
            "damaged_db_sha256": row.damaged_db_sha256,
            "backup_manifest_sha256": row.backup_manifest_sha256,
            "target_store_path": row.target_store_path,
            "target_store_label": None
            if row.target_store_path is None
            else self.label(row.target_store_path),
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "decided_at": row.decided_at,
            "decided_by": row.decided_by,
            "grant_expires_at": row.grant_expires_at,
            "used_at": row.used_at,
            "closed_at": row.closed_at,
        }
        return MaintenanceApprovalView.model_validate(values, strict=True).model_dump(mode="json")

    def get(self, project_id: str, approval_id: str, rank: int) -> dict[str, Any]:
        return self.view(self._get(project_id, approval_id, rank))

    def queue(self, project_id: str) -> dict[str, Any]:
        """The owner's view: every maintenance request of the project."""
        return {"approvals": [self.view(r) for r in self._listing(project_id, 2)]}

    def withdraw(self, project_id: str, approval_id: str, rank: int) -> dict[str, Any]:
        self._get(project_id, approval_id, rank)
        with self.registry.transaction() as conn:
            _expire_due(conn)
            row = _row(conn, approval_id)
            assert row is not None
            if row.caller_rank != rank:
                raise PrometheosError(
                    "request_key_conflict",
                    "only the scope that made a maintenance request can withdraw it",
                    {"approval_request_id": approval_id},
                )
            updated = _transition(conn, row, "withdrawn", closed_at=approvals.now_ts())
        return self.view(updated)

    def decide(
        self,
        project_id: str,
        approval_id: str,
        *,
        grant: bool,
        plan_sha256: str,
        owner: object,
    ) -> dict[str, Any]:
        """Owner channels only (the recovery screen, an interactive maintenance session):
        grant or deny exactly the plan that was shown."""
        act = require_owner(owner, "deciding a maintenance request")
        self._get(project_id, approval_id, 2)
        with self.registry.transaction() as conn:
            _expire_due(conn)
            row = _row(conn, approval_id)
            assert row is not None
            if row.state != "pending":
                raise PrometheosError(
                    "revision_conflict",
                    f"this maintenance request is {row.state}; only a pending one can be decided",
                    {"approval_request_id": row.id, "state": row.state},
                )
            if plan_sha256 != row.plan_sha256:
                raise PrometheosError(
                    "revision_conflict",
                    "the plan differs from the one shown; review it again",
                    {"approval_request_id": row.id},
                )
            now = approvals._now()
            decided = {"decided_at": approvals._ts(now), "decided_by": act.actor[:100]}
            if not grant:
                updated = _transition(conn, row, "denied", closed_at=approvals._ts(now), **decided)
            else:
                expires = datetime.strptime(row.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=UTC
                )
                if (expires - now).total_seconds() < MIN_GRANT_SECONDS:
                    raise PrometheosError(
                        "validation_error",
                        "this maintenance request expires too soon to be granted; ask again",
                        {"approval_request_id": row.id},
                    )
                # The grant is the row's state, held in the registry, until the request's own
                # expiry; there is no secret to hand anyone.
                updated = _transition(
                    conn, row, "granted", grant_expires_at=row.expires_at, **decided
                )
        return self.view(updated)

    def resume(self, project_id: str, approval_id: str, rank: int, actor: str) -> dict[str, Any]:
        """Run a granted request once, as the rank that made it. A used switch answers its
        quarantine record (``replayed``); pending answers ``approval_required``; denied,
        withdrawn, expired and revoked never run."""
        row = self._get(project_id, approval_id, rank)
        if row.caller_rank != rank:
            raise PrometheosError(
                "request_key_conflict",
                "a maintenance request resumes only at the scope that made it",
                {"approval_request_id": approval_id},
            )
        operation = OPERATIONS[str(row.plan["action"])]
        if row.state == "used":
            result = None
            if row.plan["action"] == "registry_switch":
                record = next(
                    (
                        r
                        for r in self.registry.quarantine_records(project_id)
                        if r.maintenance_approval_id == row.id
                    ),
                    None,
                )
                result = None if record is None else self._switch_result(record, None)
            return {
                "approval_request_id": row.id,
                "operation": operation,
                "state": "used",
                "replayed": True,
                "result": result,
            }
        if row.state == "pending":
            raise required_error(row)
        if row.state != "granted":
            raise PrometheosError(
                "policy_denied",
                f"maintenance request {row.id} is {row.state}; it cannot be resumed (request "
                "again)",
                {"approval_request_id": row.id, "state": row.state},
            )
        if row.plan["action"] == "registry_switch":
            fresh = self.switch_plan(
                project_id,
                str(row.plan["backup_path"]),
                str(row.plan["target_store_path"]),
                agent=row.caller_rank < 2,
            )
            for key, what in (
                ("damaged_store_path", "the registered store"),
                ("damaged_db_sha256", "the damaged store's database"),
                ("store_health", "the damaged store's health"),
                ("backup_sha256", "the backup archive"),
                ("backup_manifest_sha256", "the backup's manifest"),
                ("target_store_path", "the target store"),
                ("target_db_sha256", "the restored store's database"),
                ("target_resolved_path", "the restored store's location"),
                ("target_media", "the restored store's media binding"),
            ):
                if fresh.get(key) != row.plan.get(key):
                    raise _changed(what)
            result = self._commit_switch(
                row.plan, row, f"{actor} with the grant of {row.decided_by}", "agent request "
                f"{row.id} granted by the owner",
            )  # fmt: skip
        else:
            result = self._run_migrate(project_id, row)
        return {
            "approval_request_id": row.id,
            "operation": operation,
            "state": "used",
            "replayed": False,
            "result": result,
        }

    def _run_migrate(self, project_id: str, row: MaintenanceApproval) -> dict[str, Any]:
        store = self._store(project_id)
        if str(store) != row.damaged_store_path:
            raise _changed("the registered store")
        if db_digest(store) != row.damaged_db_sha256:
            raise _changed("the store's database")
        if self.probe(project_id)["health"] != "needs_migration":
            raise _changed("the store's health")

        def authorize_source(admitted: Path) -> None:
            if admitted != store:
                raise _changed("the registered migration source")
            # Service.migrate owns maintenance then admission before this callback. It
            # retains that gate through upgrade, so supported writers cannot enter between
            # exact grant validation/use and the real source connection/mutation.
            with admission.source_snapshot(admitted) as snapshot:
                if not snapshot.mode_supported():
                    raise _mode_error(project_id)
                binding = source_binding(snapshot)
                if binding != row.plan.get("source_binding"):
                    raise _changed("the migration source files")
                with self.registry.transaction() as conn:
                    registered = conn.execute(
                        "SELECT store_path FROM projects WHERE project_id = ?", (project_id,)
                    ).fetchone()
                    if registered is None or registered["store_path"] != str(admitted):
                        raise _changed("the registered migration source")
                    _expire_due(conn)
                    current = _row(conn, row.id)
                    assert current is not None
                    _use_grant(conn, current, approvals.now_ts())

        # Grant use commits before migration (at most once), inside the admission gate that
        # remains held through the mutation. Failure after use remains a visible used grant.
        act = OwnerAct(
            channel="approval-grant", actor=f"{row.decided_by or 'owner'} via {row.id}"[:100]
        )
        result: dict[str, Any] = self.svc.migrate(
            project_id, authority=act, _authorize_source=authorize_source
        )
        return result


# ----- registry rows -----------------------------------------------------------------------


def _forget(store_dir: Path) -> None:
    """Drop cached probes of a store that just changed role (a switch)."""
    root = os.path.abspath(store_dir)
    with _CACHE_LOCK:
        for key in [k for k in _CACHE if k[0] == root]:
            _CACHE.pop(key, None)


def owner_route(row: MaintenanceApproval) -> dict[str, str]:
    return {
        "hub": RECOVERY_HUB,
        "cli": OWNER_ROUTE_CLI.format(id=row.id, project=row.project_id),
    }


def required_error(row: MaintenanceApproval) -> PrometheosError:
    """The typed ``approval_required`` answer for a stored maintenance request."""
    action = str(row.plan["action"])
    details = ApprovalRequiredDetails(
        approval_request_id=row.id,
        plan_sha256=row.plan_sha256,
        required_scope=SCOPES[action],
        owner_route=owner_route(row),  # type: ignore[arg-type]
        expires_at=row.expires_at,
    ).model_dump(mode="json")
    state = "" if row.state == "pending" else f" (it is {row.state})"
    return PrometheosError(
        "approval_required",
        f"this needs the owner's approval; nothing was changed. Maintenance request {row.id}"
        f"{state} waits for the owner on the hub's recovery screen or in `prometheos maintenance "
        f"review`; after a grant, resume it with `prometheos maintenance resume {row.id}`",
        details,
    )


def _printable(text: str) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in text).strip() or "maintenance"


def _changed(what: str) -> PrometheosError:
    return PrometheosError(
        "revision_conflict",
        f"{what} changed since the request was made; a changed store, backup or target needs a "
        "new request",
        {},
    )


def _row(conn: sqlite3.Connection, approval_id: str) -> MaintenanceApproval | None:
    found = conn.execute(
        "SELECT * FROM maintenance_approvals WHERE id = ?", (approval_id,)
    ).fetchone()
    return None if found is None else from_row(MaintenanceApproval, found)


def _transition(
    conn: sqlite3.Connection, row: MaintenanceApproval, state: str, **fields: Any
) -> MaintenanceApproval:
    if state not in _TRANSITIONS.get(row.state, frozenset()):
        raise PrometheosError(
            "validation_error",
            f"this maintenance request is {row.state}; it cannot become {state}",
            {"approval_request_id": row.id, "state": row.state},
        )
    values = row.model_dump(mode="json") | {"state": state, **fields}
    updated = MaintenanceApproval.model_validate(values, strict=True)  # lifecycle re-checked
    cols = to_row(updated)
    assignments = ", ".join(f"{name} = ?" for name in cols if name != "id")
    cur = conn.execute(
        f"UPDATE maintenance_approvals SET {assignments} WHERE id = ? AND state = ?",  # noqa: S608
        (*[v for k, v in cols.items() if k != "id"], row.id, row.state),
    )
    if cur.rowcount != 1:
        raise PrometheosError(
            "revision_conflict",
            "the maintenance request changed; read it again",
            {"approval_request_id": row.id},
        )
    return updated


def _expire_due(conn: sqlite3.Connection) -> None:
    """End every open request (or grant) whose time has passed, inside the caller's registry
    transaction (lazy expiry, as for project approval requests)."""
    now = approvals.now_ts()
    rows = conn.execute(
        "SELECT * FROM maintenance_approvals WHERE state IN ('pending','granted') AND "
        "(expires_at <= ? OR (grant_expires_at IS NOT NULL AND grant_expires_at <= ?))",
        (now, now),
    ).fetchall()
    for found in rows:
        _transition(conn, from_row(MaintenanceApproval, found), "expired", closed_at=now)


def _use_grant(conn: sqlite3.Connection, approval: MaintenanceApproval, now: str) -> None:
    """Use a granted request once, inside the transaction that performs its effect."""
    current = _row(conn, approval.id)
    if current is None or current.state != "granted":
        state = "missing" if current is None else current.state
        raise PrometheosError(
            "policy_denied",
            f"maintenance request {approval.id} is {state}; it cannot be used",
            {"approval_request_id": approval.id, "state": state},
        )
    if current.plan_sha256 != approval.plan_sha256:
        raise _changed("the request's plan")
    if current.grant_expires_at is not None and current.grant_expires_at <= now:
        raise PrometheosError(
            "policy_denied",
            f"the grant of maintenance request {approval.id} has expired",
            {"approval_request_id": approval.id, "state": "expired"},
        )
    _transition(conn, current, "used", used_at=now, closed_at=now)
