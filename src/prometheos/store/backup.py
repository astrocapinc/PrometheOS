"""Verified backup, verification and staged restore of one project store.

Backup holds the maintenance lock and a SQLite write lock (blocking concurrent
mutations for the bounded snapshot window), copies the database with the
SQLite backup API and adds every referenced retained media file with its
digest. Reference-only bytes are never included; they are listed so the
limitation is visible.

Verification never trusts the archive: member names must be exactly the
expected layout (no traversal, absolute, drive or duplicate names), sizes and
digests must match the manifest, compression ratios are bounded, the database
must pass ``integrity_check`` and every payload/media digest must match.
Restore verifies into a fresh staging directory beside the target and only
then renames it into place; it never overwrites an existing directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError

from prometheos import __version__
from prometheos.domain.canonical import CanonicalJSONError, dumps_canonical, loads_internal
from prometheos.domain.ids import ProjectId, RecordId
from prometheos.domain.models import (
    MAX_INT,
    PrometheosError,
    Sha256,
    StrictModel,
    Timestamp,
    parse_model,
    utc_now,
)
from prometheos.faults import fault_point
from prometheos.store import admission, db
from prometheos.store.lock import maintenance_lock
from prometheos.store.migrations import schema_version
from prometheos.store.project import DB_NAME, SUBDIRS, ProjectStore
from prometheos.store.schema import MIGRATIONS

FORMAT = "prometheos-backup-v1"


class _MediaEntry(StrictModel):
    path: Annotated[str, StringConstraints(pattern=r"^media/[0-9a-f]{2}/[0-9a-f]{64}$")]
    sha256: Sha256
    bytes: int = Field(ge=0, le=MAX_INT)


class _DatabaseEntry(StrictModel):
    path: Literal["record.sqlite"]
    sha256: Sha256
    bytes: int = Field(ge=1, le=MAX_INT)


class _ReferenceOnly(StrictModel):
    version_id: RecordId
    source_id: RecordId
    sha256: Sha256


class BackupManifest(StrictModel):
    format: Literal["prometheos-backup-v1"]
    created_at: Timestamp
    software_version: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    project_id: ProjectId
    project_seq: int = Field(ge=0, le=MAX_INT)
    schema_version: int = Field(ge=1, le=10000)
    database: _DatabaseEntry
    media: list[_MediaEntry] = Field(max_length=1_000_000)
    reference_only_not_included: list[_ReferenceOnly] = Field(max_length=1_000_000)


MEDIA_NAME = re.compile(r"^media/([0-9a-f]{2})/([0-9a-f]{64})$")
MAX_RATIO = 200
CHUNK = 1024 * 1024


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(
    store: ProjectStore, out: Path, *, allow_pending: bool = False, already_locked: bool = False
) -> dict[str, Any]:
    """``allow_pending``/``already_locked`` are used only by the pre-migration backup,
    whose caller already holds the maintenance lock."""
    if out.exists():
        raise PrometheosError("validation_error", "backup output already exists", {})
    staging = store.directory / "staging" / f"backup-{uuid.uuid4()}"
    try:
        with nullcontext() if already_locked else maintenance_lock(store.directory):
            # SQLite's backup step reports LOCKED forever if the *source* connection holds a
            # write transaction, so writers are blocked by a second connection's RESERVED
            # lock only while the reader connection copies the database snapshot. Media are
            # immutable and content-addressed, and the maintenance lock keeps orphan
            # collection away, so they are hashed after writers are released.
            with admission.operation(store.db_path):
                blocker = store.connect(allow_pending=allow_pending)
                conn = None
                try:
                    conn = store.connect(readonly=True, allow_pending=allow_pending)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    staging.mkdir(parents=True)
                    snapshot = staging / DB_NAME
                    with db.write_transaction(blocker):
                        target = sqlite3.connect(snapshot)
                        try:
                            conn.backup(target)
                        finally:
                            target.close()
                        seq = int(
                            conn.execute("SELECT project_seq FROM project_state").fetchone()[0]
                        )
                        project_id = str(
                            conn.execute("SELECT project_id FROM project_state").fetchone()[0]
                        )
                        media_rows = [
                            dict(r)
                            for r in conn.execute(
                                "SELECT sha256, byte_length, storage_key FROM media ORDER BY sha256"
                            )
                        ]
                        external = _reference_only(conn)
                        version = schema_version(conn)
                        fault_point("backup_in_snapshot")
                finally:
                    if conn is not None:
                        conn.close()
                    blocker.close()
            media = []
            for row in media_rows:
                path = store.directory / "media" / row["storage_key"]
                if not path.is_file() or _sha_file(path) != row["sha256"]:
                    raise PrometheosError(
                        "io_error",
                        "retained media is missing or corrupt; backup refused",
                        {"sha256": row["sha256"]},
                    )
                media.append(
                    {
                        "path": f"media/{row['storage_key']}",
                        "sha256": row["sha256"],
                        "bytes": row["byte_length"],
                    }
                )
            manifest = {
                "format": FORMAT,
                "created_at": utc_now(),
                "software_version": __version__,
                "project_id": project_id,
                "project_seq": seq,
                "schema_version": version,
                "database": {
                    "path": DB_NAME,
                    "sha256": _sha_file(snapshot),
                    "bytes": snapshot.stat().st_size,
                },
                "media": media,
                "reference_only_not_included": external,
            }
            tmp = out.with_name(out.name + ".part")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
                for item in media:
                    archive.write(store.directory / item["path"], item["path"])
                archive.write(snapshot, DB_NAME)
                archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            with open(tmp, "r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, out)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"backup": str(out), "sha256": _sha_file(out), "manifest": manifest}


def _reference_only(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.id, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id AND "
        "v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.kind = 'version' "
        "ORDER BY r.id"
    ).fetchall()
    out = []
    for row in rows:
        if row["encoded"] is None:  # purged tombstone history
            continue
        body = loads_internal(row["encoded"])
        if body.get("storage") == "external":
            out.append(
                {"version_id": row["id"], "source_id": body["source_id"], "sha256": body["sha256"]}
            )
    return out


def _member_names(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    names: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name = info.filename
        if name in names:
            raise PrometheosError("validation_error", "duplicate archive member", {"name": name})
        if name in ("manifest.json", DB_NAME) or MEDIA_NAME.fullmatch(name):
            names[name] = info
            continue
        raise PrometheosError(
            "policy_denied", "unexpected or unsafe archive member", {"name": name[:200]}
        )
    return names


def _extract(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, expect: dict[str, Any]
) -> None:
    if info.file_size != expect["bytes"]:
        raise PrometheosError(
            "validation_error", "archive member size mismatch", {"name": info.filename}
        )
    if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_RATIO:
        raise PrometheosError("policy_denied", "archive member compression ratio too high", {})
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    with archive.open(info) as src, open(dest, "xb") as out:
        while chunk := src.read(CHUNK):
            written += len(chunk)
            if written > expect["bytes"]:
                raise PrometheosError("validation_error", "archive member larger than declared", {})
            digest.update(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    if digest.hexdigest() != expect["sha256"]:
        raise PrometheosError(
            "validation_error", "archive member digest mismatch", {"name": info.filename}
        )


def _open_archive(backup: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(backup)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PrometheosError("validation_error", "backup is not a readable archive", {}) from exc


def _archive_manifest(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, zipfile.ZipInfo], dict[str, Any]]:
    """The archive's member table and its parsed manifest (nothing is extracted)."""
    names = _member_names(archive)
    if "manifest.json" not in names or DB_NAME not in names:
        raise PrometheosError("validation_error", "backup lacks manifest or database", {})
    if names["manifest.json"].file_size > 64 * 1024 * 1024:
        raise PrometheosError("policy_denied", "manifest too large", {})
    try:
        parsed = parse_model(BackupManifest, archive.read("manifest.json"))
    except (CanonicalJSONError, ValidationError) as exc:
        raise PrometheosError(
            "validation_error", "backup manifest is malformed or of an unknown format", {}
        ) from exc
    return names, parsed.model_dump(mode="json")


def read_manifest(backup: Path) -> dict[str, Any]:
    """The backup's parsed manifest, read without extracting or writing anything. It is not
    a verification: :func:`verify_into` still checks every member (A04, R2-L2: the owner's
    restore uses it to check the registered store before writing the target)."""
    with _open_archive(backup) as archive:
        return _archive_manifest(archive)[1]


def verify_into(
    backup: Path, dest: Path, *, expect_project_id: str | None = None
) -> dict[str, Any]:
    """Validate ``backup`` completely while extracting it into empty ``dest``. With
    ``expect_project_id`` (the maintenance service, decision 0021) an archive of another
    project is refused before anything is extracted."""
    archive = _open_archive(backup)
    with archive:
        names, manifest = _archive_manifest(archive)
        if expect_project_id is not None and manifest["project_id"] != expect_project_id:
            raise PrometheosError(
                "validation_error",
                "this backup belongs to another project",
                {"project_id": expect_project_id},
            )
        declared_media = {m["path"]: m for m in manifest.get("media", [])}
        actual_media = {n for n in names if n.startswith("media/")}
        if set(declared_media) != actual_media:
            raise PrometheosError("validation_error", "archive media differ from manifest", {})
        for sub in SUBDIRS:
            (dest / sub).mkdir(parents=True, exist_ok=True)
        _extract(archive, names[DB_NAME], dest / DB_NAME, manifest["database"])
        for name, expect in declared_media.items():
            match = MEDIA_NAME.fullmatch(name)
            assert match is not None
            if match.group(2) != expect["sha256"] or not expect["sha256"].startswith(
                match.group(1)
            ):
                raise PrometheosError("validation_error", "media name does not match digest", {})
            _extract(archive, names[name], dest / name, expect)
    conn = db.connect(dest / DB_NAME, readonly=True)
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise PrometheosError(
                "validation_error", "database integrity check failed", {"result": check}
            )
        version = schema_version(conn)
        if version > len(MIGRATIONS) or version != manifest.get("schema_version"):
            raise PrometheosError("unsupported", "backup schema version is not supported", {})
        state = conn.execute("SELECT project_id, project_seq FROM project_state").fetchone()
        if state["project_id"] != manifest.get("project_id") or state[
            "project_seq"
        ] != manifest.get("project_seq"):
            raise PrometheosError("validation_error", "database does not match manifest", {})
        recorded = {r["sha256"]: r["storage_key"] for r in conn.execute("SELECT * FROM media")}
        if {f"media/{k}" for k in recorded.values()} != set(declared_media):
            raise PrometheosError("validation_error", "media table does not match archive", {})
        for row in conn.execute("SELECT sha256, data FROM payloads"):
            if hashlib.sha256(bytes(row["data"])).hexdigest() != row["sha256"]:
                raise PrometheosError("validation_error", "retained payload digest mismatch", {})
    finally:
        conn.close()
    return manifest


def verify_backup(
    backup: Path, scratch: Path, *, expect_project_id: str | None = None
) -> dict[str, Any]:
    dest = scratch / f"verify-{uuid.uuid4()}"
    try:
        manifest = verify_into(backup, dest, expect_project_id=expect_project_id)
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    return {"verified": True, "manifest": manifest}


def restore_backup(
    backup: Path,
    into: Path,
    prepare: Callable[[Path], Any] | None = None,
    *,
    expect_project_id: str | None = None,
) -> tuple[dict[str, Any], Any]:
    """Verify into staging, run ``prepare`` on the staged store, then rename into place."""
    if into.exists():
        raise PrometheosError(
            "validation_error", "restore target already exists; restore never overwrites", {}
        )
    staging = into.with_name(f".{into.name}.restore-{uuid.uuid4().hex[:8]}")
    try:
        manifest = verify_into(backup, staging, expect_project_id=expect_project_id)
        prepared = prepare(staging) if prepare is not None else None
        fault_point("restore_before_switch")
        os.replace(staging, into)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest, prepared


def semantic_snapshot(store_dir: Path) -> dict[str, Any]:
    """Record-level content for equivalence checks (excludes physical row ids)."""
    conn = db.connect(store_dir / DB_NAME, readonly=True)
    try:

        def rows(sql: str) -> list[list[Any]]:
            return [list(r) for r in conn.execute(sql).fetchall()]

        snapshot: dict[str, Any] = json.loads(
            dumps_canonical(
                {
                    "project": rows("SELECT project_id, project_seq FROM project_state"),
                    "records": rows(
                        "SELECT id, kind, current_rev, sensitivity, lifecycle, created_seq, "
                        "updated_seq FROM records ORDER BY id"
                    ),
                    "revisions": rows(
                        "SELECT v.record_id, v.rev, v.commit_seq, v.op_index, v.recorded_at, "
                        "v.occurred_at, v.sensitivity, v.lifecycle, b.sha256, b.encoded "
                        "FROM revisions v JOIN bodies b ON b.id = v.body_id "
                        "ORDER BY v.record_id, v.rev"
                    ),
                    "relations": rows(
                        "SELECT source_id, source_rev, target_id, target_rev, relation, "
                        "created_seq, retired_seq FROM relations ORDER BY source_id, target_id, "
                        "relation, created_seq"
                    ),
                    "audit": rows(
                        "SELECT seq, op_index, request_id, operation, record_id, rev, actor "
                        "FROM audit ORDER BY seq, op_index"
                    ),
                    "requests": rows(
                        "SELECT request_id, request_sha256, project_seq, response FROM requests "
                        "ORDER BY request_id"
                    ),
                    "payloads": rows("SELECT sha256, byte_length FROM payloads ORDER BY sha256"),
                    "media": rows(
                        "SELECT sha256, byte_length, storage_key FROM media ORDER BY sha256"
                    ),
                    "source_checks": rows(
                        "SELECT version_id, checked_at, retained_digest_ok, origin_current_digest, "
                        "origin_reachable, error, commit_seq FROM source_checks "
                        "ORDER BY commit_seq, version_id"
                    ),
                }
            )
        )
        return snapshot
    finally:
        conn.close()
