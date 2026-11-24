"""Explicit source import (plan -> commit) and source observations.

A plan names exact files under an approved research root, previews retention,
sensitivity and limits, and is persisted with its digest and expiry. Commit
re-reads every byte; any change requires a new plan. Retained bytes are stored
in the database (``payloads``) when small enough, otherwise staged, hashed,
flushed and renamed into content-addressed ``media/`` before the database
reference commits. Reference-only versions keep identity metadata only and are
never reported as retained.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import sqlite3
import sys
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from prometheos.app.reads import current_record
from prometheos.domain.bodies import ProjectSettings, SourceBody, VersionBody
from prometheos.domain.canonical import canonical_sha256, dumps_canonical, loads_internal
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError, utc_now
from prometheos.faults import fault_point
from prometheos.sources.paths import open_verified, relative_to_root, resolve_in_root
from prometheos.store import derived
from prometheos.store.lock import maintenance_lock
from prometheos.store.project import MAX_RANK, ProjectStore, Txn

MAX_PLAN_FILES = 100
INLINE_LIMIT = 8 * 1024 * 1024
PLAN_TTL = timedelta(hours=1)
CHUNK = 1024 * 1024


def _settings(conn: sqlite3.Connection, project_id: str) -> tuple[ProjectSettings, int]:
    row = conn.execute(
        "SELECT r.current_rev, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id "
        "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.id = ?",
        (project_id,),
    ).fetchone()
    return ProjectSettings.model_validate(loads_internal(row["encoded"])), int(row["current_rev"])


def root_path(settings: ProjectSettings, alias: str) -> Path:
    for root in settings.allowed_roots:
        if root.alias == alias:
            return Path(root.path)
    raise PrometheosError("policy_denied", "research root alias is not approved", {"alias": alias})


def hash_file(
    handle: Any, limit: int, progress: Callable[[], None] | None = None
) -> tuple[str, int, bytes | None]:
    """Stream-hash a file; keep the bytes only if they fit the inline limit. ``progress`` is
    called after every chunk (jobs pass their cancellation/lease checkpoint)."""
    digest = hashlib.sha256()
    size = 0
    kept: list[bytes] | None = []
    while chunk := handle.read(CHUNK):
        size += len(chunk)
        if size > limit:
            raise PrometheosError(
                "budget_exceeded", "file exceeds the configured media limit", {"limit": limit}
            )
        digest.update(chunk)
        if kept is not None:
            kept.append(chunk)
            if size > INLINE_LIMIT:
                kept = None
        if progress is not None:
            progress()
    return digest.hexdigest(), size, (b"".join(kept) if kept is not None else None)


def _is_text(data: bytes | None) -> bool:
    if data is None or b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# Research file types take precedence over the platform registry, which differs between
# machines (Windows maps .csv to application/vnd.ms-excel when Office is installed;
# decision 0009 item 4).
RESEARCH_MIME = {
    ".csv": "text/csv",
    ".m": "text/x-matlab",
    ".json": "application/json",
    ".md": "text/markdown",
    ".tex": "application/x-tex",
    ".bib": "application/x-bibtex",
    ".slx": "application/vnd.mathworks.simulink",
    ".mat": "application/x-matlab-data",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}


def guess_mime(relative: str, data: bytes | None) -> str:
    mime = RESEARCH_MIME.get(Path(relative).suffix.lower()) or mimetypes.guess_type(relative)[0]
    if mime is None:
        return "text/plain" if _is_text(data) else "application/octet-stream"
    if mime.startswith("text/") and data is not None and not _is_text(data):
        return "application/octet-stream"  # a .csv or .m that is not UTF-8 text
    return mime


def _same_path(a: str, b: str) -> bool:
    """Root-relative identity; Windows file names are case-insensitive."""
    if sys.platform == "win32":
        return a.casefold() == b.casefold()
    return a == b


OWNER_SCOPE_REQUIRED = (
    "this path is already bound to a source outside your scope: importing a new version "
    "needs owner scope (--scope owner --capability-file)"
)


def _source_for_binding(conn: sqlite3.Connection, alias: str, path: str) -> dict[str, Any] | None:
    """The source bound to ``path`` (any sensitivity: callers decide what may be named)."""
    rows = conn.execute(
        "SELECT r.id, r.current_rev, r.sensitivity, b.encoded FROM records r JOIN revisions v "
        "ON v.record_id = r.id AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = 'source'"
    ).fetchall()
    for row in rows:
        body = loads_internal(row["encoded"])
        binding = body.get("root_binding")
        if binding and binding["alias"] == alias and _same_path(binding["path"], path):
            return {
                "id": row["id"],
                "rev": row["current_rev"],
                "sensitivity": row["sensitivity"],
                "body": body,
            }
    return None


RECEIPT_SCOPE_REQUIRED = (
    "a listed file is bound to a source outside your scope: recording a run that names it needs "
    "owner scope (--scope owner --capability-file)"
)


def recorded_artifacts(
    store: ProjectStore, files: list[dict[str, str]], max_rank: int
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Each ``{"root_alias", "path", "role"}`` as a receipt artifact pinned to its newest recorded
    version, when the file on disk still has that version's digest (decision 0112). Returns the
    artifacts and the files that must be imported first: not recorded yet, or changed on disk
    since. A path bound to a source above ``max_rank`` is refused without naming it. Reads the
    files and the store; writes nothing."""
    conn = store.connect(readonly=True)
    try:
        settings, _ = _settings(conn, store.project_id)
    finally:
        conn.close()
    artifacts: list[dict[str, Any]] = []
    needs_import: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in files:
        root = root_path(settings, item["root_alias"])
        resolved = resolve_in_root(root, item["path"])
        # Paths differ only in case on case-sensitive systems (review frame-rerun-a R2-A1).
        relative = resolved.relative.casefold() if os.name == "nt" else resolved.relative
        key = (item["root_alias"], relative)
        if key in seen:  # review frame-rerun-a F3, as create_plan refuses it
            raise PrometheosError("validation_error", "file listed twice", {"path": item["path"]})
        seen.add(key)
        with open_verified(resolved) as handle:
            sha, _, _ = hash_file(handle, settings.retention.max_media_bytes)
        conn = store.connect(readonly=True)
        try:
            source = _source_for_binding(conn, item["root_alias"], resolved.relative)
            if source and SENSITIVITY_ORDER[source["sensitivity"]] > max_rank:
                raise PrometheosError("policy_denied", RECEIPT_SCOPE_REQUIRED, {})
            latest = _latest_version(conn, source["id"]) if source else None
            if latest and SENSITIVITY_ORDER[latest[2]] > max_rank:
                raise PrometheosError("policy_denied", RECEIPT_SCOPE_REQUIRED, {})
        finally:
            conn.close()
        where = {"root_alias": item["root_alias"], "path": resolved.relative}
        if latest is None:
            needs_import.append(where | {"reason": "not_recorded"})
        elif latest[1] != sha:
            needs_import.append(where | {"reason": "changed_on_disk"})
        else:
            artifacts.append(
                where
                | {
                    "source_id": str(source["id"]) if source else "",
                    "version_id": latest[0],
                    "sha256": sha,
                    "role": item["role"],
                }
            )
    return artifacts, needs_import


def _latest_version(conn: sqlite3.Connection, source_id: str) -> tuple[str, str, str] | None:
    """(version ID, sha256, sensitivity) of a source's newest version."""
    rows = conn.execute(
        "SELECT r.id, r.sensitivity, b.encoded FROM records r JOIN revisions v ON v.record_id = "
        "r.id AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.kind = "
        "'version' AND json_extract(b.encoded, '$.source_id') = ? "
        "ORDER BY r.created_seq DESC LIMIT 1",
        (source_id,),
    ).fetchall()
    for row in rows:
        body = loads_internal(row["encoded"])
        return str(row["id"]), str(body["sha256"]), str(row["sensitivity"])
    return None


def _latest_version_sha(conn: sqlite3.Connection, source_id: str) -> str | None:
    rows = conn.execute(
        "SELECT r.id, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id AND "
        "v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.kind = 'version' "
        "ORDER BY r.created_seq DESC"
    ).fetchall()
    for row in rows:
        body = loads_internal(row["encoded"])
        if body["source_id"] == source_id:
            return str(body["sha256"])
    return None


def create_plan(
    store: ProjectStore,
    *,
    root_alias: str,
    paths: list[str],
    retention: str | None,
    sensitivity: str,
    titles: dict[str, str] | None = None,
    description: str = "",
    max_rank: int,
    plan_id: str | None = None,
    request_id: str | None = None,
    progress: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """``max_rank`` is the caller's visibility ceiling (agent scope 0, owner scope 2). It is
    required: a plan remembers the rank it was made at, and only that rank or a higher one
    may commit it (decision 0014 item 1).

    An operation run passes ``plan_id`` and ``request_id`` derived from its run ID: a resumed
    run then finds the plan it already stored instead of creating a second one, and
    ``progress`` is its cancellation checkpoint while files are hashed (decision 0017)."""
    if plan_id is not None:
        stored = stored_plan(store, plan_id, max_rank)
        if stored is not None:
            return stored
    if not paths or len(paths) > MAX_PLAN_FILES:
        raise PrometheosError(
            "budget_exceeded", f"select between 1 and {MAX_PLAN_FILES} files", {"count": len(paths)}
        )
    if sensitivity not in SENSITIVITY_ORDER:
        raise PrometheosError("validation_error", "unknown sensitivity", {})
    # No store connection (and so no project admission gate) is held while a file is hashed
    # or ``progress`` checkpoints the run: each file's checks read on a short connection of
    # their own (decision 0075).
    conn = store.connect(readonly=True)
    try:
        settings, _ = _settings(conn, store.project_id)
        # The sequence before the checks read anything: a lower bound when writes interleave.
        expected_seq = store.project_seq(conn)
    finally:
        conn.close()
    root = root_path(settings, root_alias)
    mode = retention or settings.retention.default_retention
    if mode not in ("copy", "reference"):
        raise PrometheosError("validation_error", "retention must be copy or reference", {})
    items = []
    seen: set[str] = set()
    for given in paths:
        rel = relative_to_root(root, Path(given)) if os.path.isabs(given) else given
        resolved = resolve_in_root(root, rel)
        if resolved.relative in seen:
            raise PrometheosError("validation_error", "file selected twice", {"path": rel})
        seen.add(resolved.relative)
        with open_verified(resolved) as handle:
            sha, size, data = hash_file(handle, settings.retention.max_media_bytes, progress)
        conn = store.connect(readonly=True)
        try:
            existing = _source_for_binding(conn, root_alias, resolved.relative)
            if existing and SENSITIVITY_ORDER[existing["sensitivity"]] > max_rank:
                # Nothing about the hidden source (ID, revision, mode) is named, and the
                # refusal says what to do instead of "create a new plan", which cannot help.
                raise PrometheosError("policy_denied", OWNER_SCOPE_REQUIRED, {})
            if existing and existing["body"]["retention"] != mode:
                raise PrometheosError(
                    "validation_error",
                    "an existing source keeps its original retention mode",
                    {"source_id": existing["id"], "retention": existing["body"]["retention"]},
                )
            if (
                existing
                and SENSITIVITY_ORDER[sensitivity]
                < SENSITIVITY_ORDER[
                    conn.execute(
                        "SELECT sensitivity FROM records WHERE id = ?", (existing["id"],)
                    ).fetchone()[0]
                ]
            ):
                raise PrometheosError(
                    "policy_denied", "a new version cannot lower its source's sensitivity", {}
                )
            latest = _latest_version_sha(conn, existing["id"]) if existing else None
        finally:
            conn.close()
        if mode == "reference":
            storage = "external"
        else:
            storage = "body" if data is not None else "media"
        items.append(
            {
                "relative_path": resolved.relative,
                "action": "unchanged"
                if latest == sha
                else ("new_version" if existing else "create_source"),
                "source_id": existing["id"] if existing else new_id("source"),
                "source_base_rev": existing["rev"] if existing else 0,
                "version_id": new_id("version"),
                "sha256": sha,
                "byte_length": size,
                "mime": guess_mime(resolved.relative, data),
                "storage": storage,
                "title": (titles or {}).get(given) or Path(resolved.relative).name,
            }
        )
    conn = store.connect()
    try:
        now = datetime.now(UTC)
        plan = {
            "plan_id": plan_id or str(uuid.uuid4()),
            "project_id": store.project_id,
            "request_id": request_id or new_request_id(),
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (now + PLAN_TTL).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_seq": expected_seq,
            "root_alias": root_alias,
            "retention": mode,
            "sensitivity": sensitivity,
            "description": description,
            "items": items,
            "retained": [i["relative_path"] for i in items if i["storage"] != "external"],
            "reference_only": [i["relative_path"] for i in items if i["storage"] == "external"],
            "transmitted": [],
        }
        plan["plan_sha256"] = canonical_sha256(plan)
        # The creating rank is stored with the plan but is not part of the plan the caller
        # sees or of its digest.
        stored = dict(plan, created_rank=max_rank)
        conn.execute(
            "INSERT INTO import_plans (plan_id, plan_sha256, plan, created_at, expires_at, status) "
            "VALUES (?,?,?,?,?,'open')",
            (
                plan["plan_id"],
                plan["plan_sha256"],
                dumps_canonical(stored).decode("utf-8"),
                plan["created_at"],
                plan["expires_at"],
            ),
        )
        return plan
    finally:
        conn.close()


def stored_plan(store: ProjectStore, plan_id: str, max_rank: int) -> dict[str, Any] | None:
    """The plan as its creator saw it (without the stored creating rank), or ``None``."""
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT plan FROM import_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    plan: dict[str, Any] = dict(loads_internal(row["plan"]))
    if plan.pop("created_rank", MAX_RANK) > max_rank:
        raise PrometheosError("not_found", "import plan does not exist", {"plan_id": plan_id})
    return plan


def _stage_media(store: ProjectStore, source: Path, sha: str) -> str:
    """Copy to staging, verify, flush and rename into content-addressed media."""
    key = f"{sha[:2]}/{sha}"
    final = store.directory / "media" / key
    if final.exists():
        if hashlib.sha256(final.read_bytes()).hexdigest() == sha:
            return key
        raise PrometheosError("io_error", "existing media blob is corrupt", {"sha256": sha})
    staged = store.directory / "staging" / f"{uuid.uuid4()}.part"
    digest = hashlib.sha256()
    with open(source, "rb") as src, open(staged, "xb") as dst:
        while chunk := src.read(CHUNK):
            digest.update(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    if digest.hexdigest() != sha:
        staged.unlink(missing_ok=True)
        raise PrometheosError(
            "revision_conflict", "source bytes changed while staging; create a new plan", {}
        )
    fault_point("import_after_stage")
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, final)
    fault_point("import_after_blob")
    return key


def commit_plan(
    store: ProjectStore,
    plan_id: str,
    actor: str,
    *,
    max_rank: int,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Commit (or replay) a plan at the caller's rank ``max_rank``.

    A plan made at a higher rank than the caller's reads as absent: a lower scope can
    neither commit it nor replay its response (IDs of restricted records). Plans stored
    before the creating rank was recorded count as owner-rank plans (fail safe).
    ``checkpoint(stage)`` is called between files and before the commit (an operation run's
    cancellation point, decision 0017)."""
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM import_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        plan = loads_internal(row["plan"]) if row is not None else None
        if plan is None or plan.get("created_rank", MAX_RANK) > max_rank:
            raise PrometheosError("not_found", "import plan does not exist", {"plan_id": plan_id})
        settings, _ = _settings(conn, store.project_id)
    finally:
        conn.close()
    request_sha = canonical_sha256(
        {"op": "source_import_commit", "plan_sha256": plan["plan_sha256"]}
    )
    if row["status"] == "committed":
        # Identical retry: the commit engine replays the stored response.
        return store.commit(
            request_id=plan["request_id"],
            request_sha256=request_sha,
            expected_seq=None,
            actor=actor,
            handler=lambda txn: None,
            max_rank=max_rank,
        )
    if datetime.now(UTC) > datetime.fromisoformat(plan["expires_at"].replace("Z", "+00:00")):
        raise PrometheosError("stale_task", "import plan expired; create a new plan", {})
    root = root_path(settings, plan["root_alias"])
    # Staged media must not be collected as an orphan between staging and commit, so
    # imports that touch media hold the maintenance lock until the reference commits.
    needs_lock = any(i["storage"] == "media" and i["action"] != "unchanged" for i in plan["items"])
    with maintenance_lock(store.directory) if needs_lock else nullcontext():
        return _commit_plan(
            store, plan, plan_id, settings, root, request_sha, actor, max_rank, checkpoint
        )


def _commit_plan(
    store: ProjectStore,
    plan: dict[str, Any],
    plan_id: str,
    settings: ProjectSettings,
    root: Path,
    request_sha: str,
    actor: str,
    max_rank: int,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    prepared: list[dict[str, Any]] = []
    for item in plan["items"]:
        if item["action"] == "unchanged":
            continue
        if checkpoint is not None:
            checkpoint(f"verify {item['relative_path']}"[:100])
        resolved = resolve_in_root(root, item["relative_path"])
        with open_verified(resolved) as handle:
            sha, size, data = hash_file(handle, settings.retention.max_media_bytes)
        if sha != item["sha256"] or size != item["byte_length"]:
            raise PrometheosError(
                "revision_conflict",
                "source bytes changed since the plan; create a new plan",
                {"path": item["relative_path"]},
            )
        entry = dict(item, data=None, storage_key=None)
        if item["storage"] == "body":
            entry["data"] = data
            entry["storage_key"] = f"payload:{sha}"
        elif item["storage"] == "media":
            entry["storage_key"] = _stage_media(store, resolved.path, sha)
        prepared.append(entry)
    if not prepared:
        raise PrometheosError(
            "validation_error", "nothing to import: every selected file is unchanged", {}
        )
    if checkpoint is not None:
        checkpoint("commit")

    def handler(txn: Txn) -> None:
        for index, item in enumerate(prepared):
            txn.op_index = index
            source = txn.get(item["source_id"])
            if item["source_base_rev"] == 0:
                if source is not None:
                    raise PrometheosError("revision_conflict", "source id already used", {})
                existing = _source_for_binding(txn.conn, plan["root_alias"], item["relative_path"])
                if existing is not None:
                    if txn.get(existing["id"]) is None:  # above the caller's rank: name nothing
                        raise PrometheosError("policy_denied", OWNER_SCOPE_REQUIRED, {})
                    raise PrometheosError(
                        "revision_conflict",
                        "this file was registered after the plan was made; create a new plan",
                        {"source_id": existing["id"]},
                    )
                body = SourceBody(
                    title=item["title"],
                    origin="file",
                    origin_uri=None,
                    root_binding={"alias": plan["root_alias"], "path": item["relative_path"]},  # type: ignore[arg-type]
                    retention=plan["retention"],
                    description=plan["description"],
                )
                source_body = body.model_dump(mode="json")
                body_id = txn.insert_body(source_body)
                txn.write_revision(
                    item["source_id"],
                    "source",
                    1,
                    body_id,
                    plan["sensitivity"],
                    "active",
                    None,
                    "source_import",
                    body=source_body,
                )
                txn.result(item["source_id"], 1)
            elif source is None or source.rev != item["source_base_rev"]:
                raise PrometheosError(
                    "revision_conflict", "source changed since the plan; create a new plan", {}
                )
            if txn.get(item["version_id"]) is not None:
                raise PrometheosError("revision_conflict", "version id already used", {})
            if item["storage"] == "body":
                txn.conn.execute(
                    "INSERT OR IGNORE INTO payloads (sha256, data, byte_length, created_seq) "
                    "VALUES (?,?,?,?)",
                    (item["sha256"], item["data"], item["byte_length"], txn.seq),
                )
            elif item["storage"] == "media":
                txn.conn.execute(
                    "INSERT OR IGNORE INTO media (sha256, byte_length, storage_key, created_seq) "
                    "VALUES (?,?,?,?)",
                    (item["sha256"], item["byte_length"], item["storage_key"], txn.seq),
                )
            version = VersionBody(
                source_id=item["source_id"],
                sha256=item["sha256"],
                byte_length=item["byte_length"],
                mime=item["mime"],
                captured_at=txn.recorded_at,
                storage=item["storage"],
                storage_key=item["storage_key"],
                availability="external" if item["storage"] == "external" else "retained",
                extraction="none",
                derived_from=None,
                payload_schema="text" if item["mime"].startswith("text/") else None,
                transformation=None,
            )
            version_body = version.model_dump(mode="json")
            body_id = txn.insert_body(version_body)
            txn.write_revision(
                item["version_id"],
                "version",
                1,
                body_id,
                plan["sensitivity"],
                "active",
                None,
                "source_import",
                body=version_body,
            )
            if item["storage"] == "body" and version_body["payload_schema"] == "text":
                derived.index_text(
                    txn.conn,
                    item["version_id"],
                    "version",
                    "",
                    bytes(item["data"]).decode("utf-8", "replace"),
                )
            txn.result(item["version_id"], 1)
            if item["source_base_rev"] != 0:
                # A new manuscript version rebinds exact anchors and flags the rest
                # in this same commit (decision 0007 item 7).
                from prometheos.app.paper_rules import on_manuscript_version

                on_manuscript_version(txn, store.directory, item["source_id"], item["version_id"])
        updated = txn.conn.execute(
            "UPDATE import_plans SET status = 'committed', commit_request_id = ? "
            "WHERE plan_id = ? AND status = 'open'",
            (txn.request_id, plan_id),
        )
        if updated.rowcount != 1:
            raise PrometheosError("revision_conflict", "plan was committed concurrently", {})
        fault_point("import_before_commit")

    return store.commit(
        request_id=plan["request_id"],
        request_sha256=request_sha,
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=max_rank,
    )


# ----- a baseline file cited by its scan digest (A06.1, CP20.4 6c; decision 0106) -----------


def cite_baseline_file(
    store: ProjectStore,
    baseline_id: str,
    path: str,
    actor: str,
    max_rank: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """A file of an accepted adoption baseline becomes a reference-only version bound to the
    digest the scan recorded, without copying or re-reading the file. It reads as unverified
    (``confirmed: false``) until a saved source check finds the same bytes at the origin. An
    already registered file is never rebound: its existing version with the same digest is
    returned, whether it was copied in or referenced, and a file registered only with other
    bytes is refused. A file whose name marks a credential is never cited (review cp20-a-r2
    A3/A9). The citation is recorded at normal sensitivity, as an agent's import is."""
    from prometheos.adoption import plan as adoption_plan
    from prometheos.adoption import tables as adoption_tables
    from prometheos.adoption.assist import HELD_BACK, secret_name

    if secret_name(path):
        raise PrometheosError("policy_denied", HELD_BACK, {})

    conn = store.connect(readonly=True)
    try:
        baseline = adoption_plan.baseline(conn, baseline_id)
        scan = adoption_tables.require_scan(conn, baseline["scan_gen"])
        row = conn.execute(
            "SELECT path, sha256, size, status, entry FROM adoption_files "
            "WHERE scan_gen = ? AND path = ?",
            (scan.scan_gen, path),
        ).fetchone()
        if row is None or row["entry"] != "file" or row["status"] != "read" or not row["sha256"]:
            raise PrometheosError(
                "not_found", "no file read with a digest at this path in the baseline's scan", {}
            )
        alias = scan.root_alias
        existing = _source_for_binding(conn, alias, path)
        if existing is not None and SENSITIVITY_ORDER[existing["sensitivity"]] > max_rank:
            raise PrometheosError("policy_denied", OWNER_SCOPE_REQUIRED, {})
        if existing is not None:
            for (vid,) in conn.execute(
                "SELECT id FROM records WHERE kind = 'version' AND lifecycle = 'active' "
                "ORDER BY created_seq DESC"
            ):
                version = current_record(conn, str(vid))
                if (
                    version is not None
                    and version["body"]["source_id"] == existing["id"]
                    and version["body"]["sha256"] == row["sha256"]
                ):
                    return {
                        "source_id": existing["id"], "version_id": version["id"],
                        "sha256": row["sha256"], "created": False,
                        **reference_state(conn, version["id"], version["body"]),
                    }  # fmt: skip
            raise PrometheosError(
                "revision_conflict",
                "this file is already registered with other bytes; import it to record the "
                "baseline's bytes as a new version",
                {"source_id": existing["id"]},
            )
    finally:
        conn.close()
    source_id, version_id = new_id("source"), new_id("version")
    request_id = request_id or new_request_id()
    digest = canonical_sha256({"cite": baseline_id, "path": path, "sha256": row["sha256"]})

    def handler(txn: Txn) -> None:
        if _source_for_binding(txn.conn, alias, path) is not None:
            raise PrometheosError("revision_conflict", "the file was registered meanwhile", {})
        source_body = SourceBody(
            title=Path(path).name,
            origin="file",
            origin_uri=None,
            root_binding={"alias": alias, "path": path},  # type: ignore[arg-type]
            retention="reference",
            description=f"Cited from adoption baseline {baseline_id} (scan {scan.scan_gen}) by "
            "its recorded digest; not copied.",
        ).model_dump(mode="json")
        txn.write_revision(source_id, "source", 1, txn.insert_body(source_body), "normal",
                           "active", None, "source_import", body=source_body)  # fmt: skip
        txn.result(source_id, 1)
        version_body = VersionBody(
            source_id=source_id,
            sha256=row["sha256"],
            byte_length=int(row["size"] or 0),
            mime=guess_mime(path, None),
            captured_at=txn.recorded_at,
            storage="external",
            storage_key=None,
            availability="external",
            extraction="none",
            derived_from=None,
            payload_schema=None,
            transformation=None,
        ).model_dump(mode="json")
        txn.write_revision(version_id, "version", 1, txn.insert_body(version_body), "normal",
                           "active", None, "source_import", body=version_body)  # fmt: skip
        txn.result(version_id, 1)
        txn.audit(f"adoption_cite:{baseline_id}", version_id, 1)

    store.commit(request_id=request_id, request_sha256=digest, expected_seq=None, actor=actor,
                 handler=handler, max_rank=max_rank)  # fmt: skip
    return {"source_id": source_id, "version_id": version_id, "sha256": row["sha256"],
            "created": True, "confirmed": False, "checked_at": None}  # fmt: skip


def reference_state(
    conn: sqlite3.Connection, version_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Whether the latest saved source check found this reference-only version's bytes at its
    origin (``confirmed``), and when."""
    latest = conn.execute(
        "SELECT origin_reachable, origin_current_digest, checked_at FROM source_checks "
        "WHERE version_id = ? ORDER BY commit_seq DESC, id DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    confirmed = bool(
        latest is not None
        and latest["origin_reachable"]
        and latest["origin_current_digest"] == body.get("sha256")
    )
    return {"confirmed": confirmed, "checked_at": latest["checked_at"] if latest else None}


# ----- observations ----------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    """What observing one version needs from the store. A scheduled refresh reads it in one
    short read and then hashes with no store connection open (decision 0027)."""

    version_id: str
    source_id: str
    body: dict[str, Any]
    binding: dict[str, Any] | None
    payload: bytes | None  # retained bytes of a ``body`` version; None when missing


def _observation_target(conn: sqlite3.Connection, version_id: str) -> _Target:
    from prometheos.app.reads import current_record  # local import avoids a cycle

    version = current_record(conn, version_id)
    if version is None or version["kind"] != "version":
        raise PrometheosError("not_found", "version does not exist", {"id": version_id})
    body = version["body"]
    source = current_record(conn, body["source_id"])
    assert source is not None
    payload: bytes | None = None
    if body["storage"] == "body":
        row = conn.execute(
            "SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)
        ).fetchone()
        payload = None if row is None else bytes(row["data"])
    return _Target(version_id, body["source_id"], body, source["body"].get("root_binding"), payload)


def _observe(
    store: ProjectStore,
    settings: ProjectSettings,
    target: _Target,
    progress: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Hash the retained bytes and the origin file of ``target``; uses no store connection."""
    body = target.body
    retained_ok: bool | None = None
    if body["storage"] == "body":
        retained_ok = (
            target.payload is not None
            and hashlib.sha256(target.payload).hexdigest() == body["sha256"]
        )
    elif body["storage"] == "media":
        path = store.directory / "media" / str(body["storage_key"])
        try:
            retained_ok = hashlib.sha256(path.read_bytes()).hexdigest() == body["sha256"]
        except OSError:
            retained_ok = False
    current_digest = None
    reachable = False
    error = None
    binding = target.binding
    progress_failure: list[PrometheosError] = []

    def guarded_progress() -> None:
        # A failure of the caller's own checkpoint (a busy store while a job renews its
        # lease) says nothing about the origin file and must not be recorded as an
        # observation of it: it propagates and fails the attempt for a retry.
        assert progress is not None
        try:
            progress()
        except PrometheosError as exc:
            progress_failure.append(exc)
            raise

    if binding:
        try:
            root = root_path(settings, binding["alias"])
            resolved = resolve_in_root(root, binding["path"])
            with open_verified(resolved) as handle:
                current_digest, _, _ = hash_file(
                    handle,
                    settings.retention.max_media_bytes,
                    guarded_progress if progress is not None else None,
                )
            reachable = True
        except PrometheosError as exc:
            if progress_failure and exc is progress_failure[0]:
                raise
            error = exc.code
    else:
        error = "no_root_binding"
    return {
        "version_id": target.version_id,
        "source_id": target.source_id,
        "checked_at": utc_now(),
        "retained_digest_ok": retained_ok,
        "origin_current_digest": current_digest,
        "origin_reachable": reachable,
        "origin_matches_version": current_digest == body["sha256"] if reachable else None,
        "error": error,
    }


def observe_version(
    store: ProjectStore,
    conn: sqlite3.Connection,
    settings: ProjectSettings,
    version_id: str,
    progress: Callable[[], None] | None = None,
) -> dict[str, Any]:
    return _observe(store, settings, _observation_target(conn, version_id), progress)


def check_sources(
    store: ProjectStore,
    ids: list[str],
    *,
    save: bool,
    actor: str,
    caller: Any,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Observe bytes; ``--save`` records the observation set as one commit.

    Hidden (above-scope) records are reported exactly like absent ones. With a
    caller-chosen ``request_id`` a retried save replays the original commit; the
    returned observations are then fresh but unsaved.
    """

    def visible(record_id: str) -> str | None:
        row = conn.execute(
            "SELECT kind, sensitivity FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None or not caller.may_see(row["sensitivity"]):
            return None
        return str(row["kind"])

    conn = store.connect(readonly=not save)
    try:
        settings, _ = _settings(conn, store.project_id)
        targets = []
        for record_id in ids:
            kind = visible(record_id)
            if kind not in ("source", "version"):
                raise PrometheosError("not_found", "source or version not found", {"id": record_id})
            if kind == "source":
                rows = conn.execute(
                    "SELECT r.id, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id "
                    "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
                    "WHERE r.kind = 'version' ORDER BY r.created_seq"
                ).fetchall()
                targets += [
                    r["id"]
                    for r in rows
                    if loads_internal(r["encoded"])["source_id"] == record_id and visible(r["id"])
                ]
            else:
                targets.append(record_id)
        inputs = [_observation_target(conn, vid) for vid in targets]
    finally:
        conn.close()
    # Retained bytes and origin files are hashed with no store connection open, so a long
    # check never holds the project's admission against other callers (decision 0027).
    observations = [_observe(store, settings, target) for target in inputs]
    result: dict[str, Any] = {"observations": observations, "saved": False}
    if save:
        request_sha = canonical_sha256({"op": "source_check", "ids": sorted(set(targets))})

        def handler(txn: Txn) -> None:
            for index, obs in enumerate(observations):
                txn.op_index = index
                txn.conn.execute(
                    "INSERT INTO source_checks (version_id, checked_at, retained_digest_ok, "
                    "origin_current_digest, origin_reachable, error, commit_seq) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        obs["version_id"],
                        obs["checked_at"],
                        obs["retained_digest_ok"],
                        obs["origin_current_digest"],
                        obs["origin_reachable"],
                        obs["error"],
                        txn.seq,
                    ),
                )
                txn.audit("source_check", obs["version_id"], None)
                txn.result(obs["version_id"], None)

        result["commit"] = store.commit(
            request_id=request_id or new_request_id(),
            request_sha256=request_sha,
            expected_seq=None,
            actor=actor,
            handler=handler,
            max_rank=caller.rank,
        )
        result["saved"] = True
    return result
