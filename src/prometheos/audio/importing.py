"""Registering audio in a project: files from an approved root and finished recordings.

A file import reuses the explicit plan -> commit machinery (bytes re-read and re-hashed at
commit, retained copy by default). A finished recording is a new ``recording`` source whose
single version holds a PCM WAV: small files inline, larger ones staged, hashed, flushed and
renamed into content-addressed media before the database reference commits.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from prometheos.app import sources
from prometheos.app.reads import Caller
from prometheos.audio import wav
from prometheos.domain.bodies import SourceBody, VersionBody
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.ids import new_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.sources.paths import resolve_in_root
from prometheos.store.lock import maintenance_lock
from prometheos.store.project import ProjectStore, Txn

AUDIO_SUFFIXES = (".wav", ".wave", ".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".flac", ".aac")
DEFAULT_AUDIO_SENSITIVITY = "restricted"
INLINE_LIMIT = sources.INLINE_LIMIT


def import_file(
    store: ProjectStore,
    *,
    caller: Caller,
    root_alias: str,
    path: str,
    sensitivity: str = DEFAULT_AUDIO_SENSITIVITY,
    title: str | None = None,
    retention: str = "copy",
    actor: str,
    plan_id: str | None = None,
    request_id: str | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Plan and commit one audio file from an approved research root.

    ``retention="reference"`` is allowed but such audio cannot be transcribed (the bytes are
    not in the store). An operation run passes a ``plan_id`` and ``request_id`` derived from
    its run, so a resumed import finds its plan and replays its commit instead of importing
    twice, and ``checkpoint`` between the plan and the commit (decision 0017)."""
    if not path.lower().endswith(AUDIO_SUFFIXES):
        raise PrometheosError(
            "validation_error",
            "not a recognised audio file name",
            {"accepted": list(AUDIO_SUFFIXES)},
        )
    plan = sources.create_plan(
        store,
        root_alias=root_alias,
        paths=[path],
        retention=retention,
        sensitivity=sensitivity,
        titles={path: title} if title else None,
        description="Audio recording import",
        max_rank=caller.rank,
        plan_id=plan_id,
        request_id=request_id,
    )
    item = plan["items"][0]
    info: dict[str, Any] | None = None
    if path.lower().endswith((".wav", ".wave")):
        # Fail now, with a clear reason, for a WAV that nothing here can read.
        conn = store.connect(readonly=True)
        try:
            settings, _ = sources._settings(conn, store.project_id)
        finally:
            conn.close()
        resolved = resolve_in_root(sources.root_path(settings, root_alias), item["relative_path"])
        try:
            header = wav.inspect_wav(resolved.path)
            info = {
                "duration_ms": header.duration_ms,
                "sample_rate": header.rate,
                "channels": header.channels,
            }
        except PrometheosError as exc:
            if exc.code != "unsupported":
                raise  # a header outside the accepted bounds is refused before anything commits
            info = None  # compressed or float WAV: PyAV decodes it at transcription time
    if item["action"] == "unchanged":
        raise PrometheosError(
            "validation_error", "this file is already imported and unchanged", {"path": path}
        )
    if checkpoint is not None:
        checkpoint("planned")
    committed = sources.commit_plan(
        store, plan["plan_id"], actor, max_rank=caller.rank, checkpoint=checkpoint
    )
    return {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "sha256": item["sha256"],
        "byte_length": item["byte_length"],
        "retention": retention,
        "sensitivity": sensitivity,
        "audio": info,
        "commit": committed,
    }


def import_recording(
    store: ProjectStore,
    wav_path: Path,
    *,
    title: str,
    sensitivity: str,
    description: str,
    actor: str,
    request_id: str,
    max_rank: int,
) -> dict[str, Any]:
    """Register a finished WAV (from the recorder) as a ``recording`` source and version."""
    if sensitivity not in SENSITIVITY_ORDER:
        raise PrometheosError("validation_error", "unknown sensitivity", {})
    info = wav.inspect_wav(wav_path)
    digest = hashlib.sha256()
    size = 0
    with open(wav_path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    sha = digest.hexdigest()
    inline = size <= INLINE_LIMIT
    data = wav_path.read_bytes() if inline else None
    key: str | None = None  # the media file's key, staged under the maintenance lock below
    source_id, version_id = new_id("source"), new_id("version")
    request_sha = canonical_sha256(
        {"op": "recording_import", "sha256": sha, "title": title, "sensitivity": sensitivity}
    )

    def handler(txn: Txn) -> None:
        source = SourceBody(
            title=title[:300],
            origin="recording",
            origin_uri=None,
            root_binding=None,
            retention="copy",
            description=description[:2000],
        ).model_dump(mode="json")
        txn.write_revision(
            source_id,
            "source",
            1,
            txn.insert_body(source),
            sensitivity,
            "active",
            None,
            "recording_import",
            body=source,
        )
        txn.result(source_id, 1)
        txn.op_index = 1
        if inline:
            txn.conn.execute(
                "INSERT OR IGNORE INTO payloads (sha256, data, byte_length, created_seq) "
                "VALUES (?,?,?,?)",
                (sha, data, size, txn.seq),
            )
        else:
            txn.conn.execute(
                "INSERT OR IGNORE INTO media (sha256, byte_length, storage_key, created_seq) "
                "VALUES (?,?,?,?)",
                (sha, size, key, txn.seq),
            )
        version = VersionBody(
            source_id=source_id,
            sha256=sha,
            byte_length=size,
            mime="audio/wav",
            captured_at=txn.recorded_at,
            storage="body" if inline else "media",
            storage_key=f"payload:{sha}" if inline else key,
            availability="retained",
            extraction="none",
            derived_from=None,
            payload_schema=None,
            transformation=None,
        ).model_dump(mode="json")
        txn.write_revision(
            version_id,
            "version",
            1,
            txn.insert_body(version),
            sensitivity,
            "active",
            None,
            "recording_import",
            body=version,
        )
        txn.result(version_id, 1)

    # Media is staged under the maintenance lock and the lock is held until the reference
    # commits, as for source imports: a content-addressed file this import reuses (the same
    # bytes as a purged recording whose file is still listed for deletion) can then never be
    # swept or collected in between (decision 0031).
    with nullcontext() if inline else maintenance_lock(store.directory):
        if not inline:
            key = sources._stage_media(store, wav_path, sha)
        response = store.commit(
            request_id=request_id,
            request_sha256=request_sha,
            expected_seq=None,
            actor=actor,
            handler=handler,
            max_rank=max_rank,
        )
    committed = [r["id"] for r in response["results"]]
    return {
        "source_id": committed[0],
        "version_id": committed[1],
        "sha256": sha,
        "byte_length": size,
        "duration_ms": info.duration_ms,
        "commit": response,
    }
