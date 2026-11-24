"""Resumable local transcription (spec/05 "Audio and bounded AI"; decision 0013).

A transcription is an operation run (``operation_runs``, decision 0017) that persists what
is needed to resume: the input digest, the configuration digest, every finished chunk (stage
detail in ``transcription_chunks``) and a lease. Completion merges the chunks
deterministically, creates the transcript as a new source and version (``transcript_v1``
payload, transcription provenance, locators in milliseconds) in one commit under a request
key derived from the run, and then drops the chunks. Cancelling stops between chunks. A
killed process leaves a ``running`` row whose lease expires; the next ``execute`` resumes
after the last stored chunk, but only against the same input and the same configuration.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

from prometheos.app import reads, runs
from prometheos.audio import engines, pcm, wav
from prometheos.audio import transcript as tr
from prometheos.domain.bodies import SourceBody, Transformation, VersionBody
from prometheos.domain.canonical import canonical_sha256, dumps_canonical
from prometheos.domain.ids import derived_request_id, is_record_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.faults import fault_point
from prometheos.store import db, derived, pending
from prometheos.store import operational as op
from prometheos.store.project import ProjectStore, Txn, live

CHUNK_MS = 60_000
OVERLAP_MS = 2_000
INLINE_LIMIT = 8 * 1024 * 1024
RANK_NAME = {rank: name for name, rank in SENSITIVITY_ORDER.items()}
LOG = logging.getLogger(__name__)
STALE_LIST = pending.LIST_NAME  # the one pending-deletion list (see store/pending.py)
LANGUAGE = re.compile(r"^[a-z]{2,3}$")


def _stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class RunConfig:
    engine: str
    model: str | None = None
    language: str | None = None
    chunk_ms: int = CHUNK_MS
    overlap_ms: int = OVERLAP_MS
    script: tuple[str, ...] = field(default_factory=tuple)

    def validate(self) -> None:
        if self.engine not in engines.ENGINES:
            raise PrometheosError(
                "validation_error", "unknown transcription engine", {"known": list(engines.ENGINES)}
            )
        if self.language is not None and not LANGUAGE.fullmatch(self.language):
            raise PrometheosError("validation_error", "language is a 2-3 letter code", {})
        if not (4_000 <= self.chunk_ms <= 600_000) or self.chunk_ms % 1000:
            raise PrometheosError("validation_error", "chunk size is 4-600 s in whole seconds", {})
        if not (0 <= self.overlap_ms <= self.chunk_ms // 2) or self.overlap_ms % 1000:
            raise PrometheosError(
                "validation_error", "overlap is whole seconds, at most half a chunk", {}
            )
        if len(self.script) > 100 or any(len(line) > 500 for line in self.script):
            raise PrometheosError("validation_error", "script is at most 100 lines of 500", {})

    def document(self, engine_version: str) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "engine_version": engine_version,
            "model": self.model if self.engine == "faster-whisper" else None,
            "language": self.language,
            "chunk_ms": self.chunk_ms,
            "overlap_ms": self.overlap_ms,
            "script": list(self.script),
        }


class Interrupted(BaseException):
    """Raised by test hooks; leaves the lease held, exactly like a killed process."""


# ----- access to audio -----------------------------------------------------------------------


def audio_version(
    conn: sqlite3.Connection, store_dir: Path, caller: reads.Caller, version_id: str
) -> dict[str, Any]:
    if not is_record_id(version_id, "version"):
        raise PrometheosError("validation_error", "malformed version id", {})
    record = reads.get_record(conn, store_dir, caller, version_id)
    body = record["body"]
    if record["kind"] != "version" or body.get("purged"):
        raise PrometheosError("not_found", "version not found", {"id": version_id})
    if not str(body["mime"]).lower().startswith("audio/"):
        raise PrometheosError(
            "validation_error", "this version is not audio", {"mime": body["mime"]}
        )
    if body["availability"] != "retained":
        raise PrometheosError(
            "source_unavailable", "the audio bytes are not retained in this store", {}
        )
    return record


def _gone() -> PrometheosError:
    return PrometheosError("not_found", "the recording was purged", {})


def _require_alive(alive: Callable[[], bool] | None) -> None:
    if alive is not None and not alive():
        raise _gone()


@contextlib.contextmanager
def materialized(conn: sqlite3.Connection, store_dir: Path, body: dict[str, Any]) -> Iterator[Path]:
    """A readable file with the version's verified bytes, valid for the ``with`` block.

    Media is used in place. A small body is read through the disposable cache entry
    ``audio-<sha>.bin``, which this block HOLDS OPEN so that a run of the same recording that
    finishes meanwhile cannot remove it (Windows refuses to remove an open file). When the
    entry cannot be had (another run holds one that is not these bytes, so it cannot be
    replaced), the block gets a private copy instead. Anything private is removed when the
    block ends, whatever happens in it: this is the only way to obtain the path, so no caller
    can leave behind a copy of a possibly restricted recording outside the media store.

    A purge cannot commit while the copy is written: the payload is read through ``conn``, so
    the caller holds the project's admission gate until the file is in place. A purge that
    lands later, while the block uses the file, cannot remove it while it is held and lists it;
    when the block ends (inline or media), the pending-deletion list is swept (decision 0031)."""
    if body["storage"] == "media":
        path = store_dir / "media" / str(body["storage_key"])
        if not path.is_file():
            raise PrometheosError("source_unavailable", "retained audio is missing", {})
        try:
            yield path
        finally:
            pending.sweep(store_dir)
        return
    row = conn.execute("SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)).fetchone()
    if row is None:
        raise PrometheosError("source_unavailable", "retained audio is missing", {})
    data = bytes(row["data"])
    if hashlib.sha256(data).hexdigest() != body["sha256"]:
        raise PrometheosError("source_unavailable", "retained audio is corrupt", {})
    cache = store_dir / "cache"
    cache.mkdir(exist_ok=True)
    try:
        with contextlib.ExitStack() as owned:
            path = _audio_source(owned, cache, body["sha256"], data)
            fault_point("transcribe_after_audio_publish")
            yield path
    finally:
        pending.sweep(store_dir)


def _audio_source(owned: contextlib.ExitStack, cache: Path, sha: str, data: bytes) -> Path:
    """The shared entry (held open until ``owned`` closes) or, failing that, a private copy
    (removed when ``owned`` closes)."""
    entry = cache / f"audio-{sha}.bin"
    held = _hold_audio(entry, data)
    if held is not None:  # a complete, verified entry (an earlier run's or a peer's)
        owned.callback(held.close)
        return entry
    tmp = _write_private(cache, sha, data)
    try:
        outcome = _publish(tmp, entry, lambda: _holds(entry, data))
    except BaseException:
        _discard(tmp)
        raise
    if outcome != "private":
        held = _hold_audio(entry, data)
        if held is not None:
            if outcome == "equivalent":
                _discard(tmp)  # the peer's equal entry is ours to read; our copy is surplus
            owned.callback(held.close)
            return entry
        if outcome == "published":  # published, then removed before it could be opened
            tmp = _write_private(cache, sha, data)
    owned.callback(_discard, tmp)
    return tmp


def _write_private(cache: Path, sha: str, data: bytes) -> Path:
    tmp = cache / pending.private_name("audio", sha)
    try:
        tmp.write_bytes(data)
    except BaseException:
        _discard(tmp)
        raise
    return tmp


def _hold_audio(entry: Path, data: bytes) -> BinaryIO | None:
    """Open ``entry`` and keep it open if (and only if) it holds exactly these verified bytes;
    what was read through the open handle is what is held, so there is no gap to exploit."""
    try:
        handle = open(entry, "rb")
    except OSError:
        return None
    try:
        if handle.read() == data:
            return handle
    except OSError:
        pass
    handle.close()
    return None


def _holds(path: Path, data: bytes) -> bool:
    """Whether ``path`` is a readable file with exactly these (already verified) bytes."""
    try:
        return path.read_bytes() == data
    except OSError:
        return False


def _same_content(a: Path, b: Path) -> bool:
    """Byte-for-byte equality of two files; anything unreadable is "not equal"."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as left, open(b, "rb") as right:
            while True:
                block = left.read(1024 * 1024)
                if block != right.read(1024 * 1024):
                    return False
                if not block:
                    return True
    except OSError:
        return False


def _publish(tmp: Path, entry: Path, equivalent: Callable[[], bool]) -> str:
    """Publish the completed private file ``tmp`` as the shared cache entry ``entry``.

    Entries are keyed by the recording's digest, so concurrent runs (two processes, two
    versions of one recording, a resume) share their names. An entry therefore only ever
    appears whole, by an atomic replace of a file its writer has finished, and a run that
    cannot publish has not failed. Returns ``"published"`` (``tmp`` is now the entry),
    ``"equivalent"`` (another run published an entry with the same content first and has it
    open, so Windows refused the replace; ``tmp`` is still there, surplus) or ``"private"``
    (the existing entry is open elsewhere and is not the same; ``tmp`` is this run's own
    completed output, to be used privately and removed by the caller)."""
    try:
        os.replace(tmp, entry)
    except PermissionError:  # Windows: access denied or sharing violation on a file in use
        return "equivalent" if equivalent() else "private"
    return "published"


def _discard(path: Path) -> None:
    """Remove a private cache file; one that cannot be removed now stays on the pending-deletion
    list until a sweep can."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        LOG.warning("could not remove cache file %s yet (%s); it is listed", path.name, exc)
        pending.add(path.parent.parent, [path.name])


# ----- runs ----------------------------------------------------------------------------------
# A transcription is an operation run (decision 0017): ``operation_runs`` is the run of record
# and ``transcription_chunks`` (keyed by the oprun_ id) is its stage detail. The chunk loop,
# leases, cancellation between chunks, budget pause and recovery are the run service's.

WORKFLOW_VERSION = "transcribe-v1"
OPERATION = "audio_transcribe"
UNFINISHED = ("queued", "running", "failed", "paused_budget")


def _params(version_id: str, document: dict[str, Any]) -> dict[str, Any]:
    return {"version_id": version_id, "config": document}


def _stage_rows(conn: sqlite3.Connection, run_id: str) -> dict[str, dict[str, Any]]:
    return {s.name: s.output for s in runs.stage_rows(conn, run_id)}


def _row_out(run: op.OperationRun, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """The CP6 view of a transcription run (``TranscriptionRunOut``), read from its run."""
    config = run.params["config"]
    assert isinstance(config, dict)
    decode: dict[str, Any] | None = None
    if conn is not None:
        decode = runs.stage_output(conn, run.id, "decode")
    progress = run.progress
    transcript = next((a.id for a in run.artifacts if a.role == "transcript"), None)
    return {
        "run_id": run.id,
        "version_id": run.params["version_id"],
        "state": run.state,
        "engine": config["engine"],
        "engine_version": config["engine_version"],
        "model": config["model"],
        "language": config["language"],
        "input_sha256": run.input_digests[0].sha256,
        "config_digest": run.config_digest,
        "total_ms": decode["total_ms"] if decode else None,
        "chunks_done": progress.done if progress else 0,
        "chunk_count": progress.total if progress else None,
        "cancel_requested": run.cancel_requested_at is not None,
        "attempts": run.attempts,
        "error": run.error.message if run.error else None,
        "result_version_id": transcript,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _read_out(store: ProjectStore, run_id: str) -> dict[str, Any]:
    conn = store.connect(readonly=True)
    try:
        run = runs._get(conn, run_id)
        return _row_out(run, conn)
    finally:
        conn.close()


class _HomeOnly:
    """The run environment's service, for callers that only have a store and a home."""

    def __init__(self, home: Path) -> None:
        self.home = home


def _env(store: ProjectStore, home: Path, service: Any = None) -> runs.Env:
    return runs.Env(service=service or _HomeOnly(home), project_id=store.project_id, store=store)


def _drop_chunks(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("DELETE FROM transcription_chunks WHERE run_id = ?", (run_id,))
    conn.execute(
        "DELETE FROM operation_run_stages WHERE run_id = ? AND (name LIKE 'chunk:%' "
        "OR name = 'decode')",
        (run_id,),
    )


def _current_document(config: dict[str, Any]) -> dict[str, Any]:
    """The run's configuration document with the engine version installed now."""
    current = dict(config)
    current["engine_version"] = engines.engine_version_without_loading(str(config["engine"]))
    return current


class TranscriptionHandler(runs.Handler):
    operation = OPERATION
    version = WORKFLOW_VERSION
    retry_failed_on_execute = True  # CP6: executing a failed run resumes it after its checks

    def fingerprint(
        self,
        env: runs.Env,
        params: dict[str, Any],
        caller: reads.Caller,
        run: Any = None,
    ) -> runs.Fingerprint:
        conn = env.store.connect(readonly=True)
        try:
            record = reads.current_record(conn, str(params["version_id"]))
        finally:
            conn.close()
        body = record["body"] if record is not None else {}
        sha = str(body.get("sha256")) if record is not None and not body.get("purged") else "0" * 64
        return runs.Fingerprint(
            inputs=(("audio", sha),), config=_current_document(dict(params["config"]))
        )

    def model_of(self, params: dict[str, Any], fingerprint: runs.Fingerprint) -> str | None:
        config = params["config"]
        return str(config.get("model") or config.get("engine")) or None

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        return _execute_run(ctx)

    def finish_txn(self, ctx: runs.RunContext, conn: Any, state: str) -> None:
        if state in ("completed", "cancelled"):
            conn.execute("DELETE FROM transcription_chunks WHERE run_id = ?", (ctx.run.id,))

    def after_finish(self, env: runs.Env, run: op.OperationRun, state: str) -> None:
        if state in ("completed", "cancelled"):
            # After the commit: a cleanup problem never fails a cancel or a completed run.
            _drop_caches(env.store, run.input_digests[0].sha256)

    def after_recover(self, env: runs.Env, run: op.OperationRun) -> None:
        # A worker that died released its handles with its process: what a purge could not
        # remove while it held them can go now, and a recovered cancellation drops its caches
        # like a confirmed one does.
        sweep_stale_caches(env.store)
        if run.state == "cancelled":
            _drop_caches(env.store, run.input_digests[0].sha256)


HANDLER = runs.register(TranscriptionHandler())


def start(
    store: ProjectStore,
    home: Path,
    caller: reads.Caller,
    version_id: str,
    config: RunConfig,
    *,
    now: datetime | None = None,
    budget_seconds: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Create a run for an audio version, or return the unfinished one with the same settings.

    The engine must be usable now (model installed, extra present): a run that cannot start
    is refused here rather than failing after the user walked away. ``budget_seconds`` is an
    optional reservation in seconds of audio: the run pauses (``paused_budget``) before a chunk
    that would exceed it and resumes later without transcribing finished chunks again."""
    config.validate()
    moment = now or datetime.now(UTC)
    version = engines.engine_version_without_loading(config.engine)
    if config.engine == "faster-whisper":
        from prometheos.audio import models

        models.installed_path(home, config.model or models.DEFAULT_MODEL)
        config = RunConfig(
            config.engine,
            config.model or models.DEFAULT_MODEL,
            config.language,
            config.chunk_ms,
            config.overlap_ms,
            config.script,
        )
        if version == "not installed":
            raise PrometheosError(
                "unsupported", "local transcription needs the optional audio extra", {}
            )
    elif config.engine == "synthetic":
        engines.require_synthetic_enabled()
    if budget_seconds is not None and not 1 <= budget_seconds <= 10**8:
        raise PrometheosError("validation_error", "budget_seconds must be positive", {})
    document = config.document(version)
    digest = canonical_sha256(document)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            record = audio_version(conn, store.directory, caller, version_id)
            sha = str(record["body"]["sha256"])
            rows = conn.execute(
                "SELECT * FROM operation_runs WHERE operation = ? AND state IN "
                "('queued','running','failed','paused_budget') "
                "AND json_extract(params, '$.version_id') = ? ORDER BY created_at DESC",
                (OPERATION, version_id),
            ).fetchall()
            for row in rows:
                existing = op.from_row(op.OperationRun, row)
                if existing.input_digests[0].sha256 != sha:
                    continue  # stale run for other bytes cannot happen (immutable), be safe
                if existing.config_digest != digest:
                    raise PrometheosError(
                        "validation_error",
                        "an unfinished transcription of this recording uses different settings; "
                        "cancel it before starting another",
                        {"run_id": existing.id},
                    )
                if existing.state == "failed":
                    existing = runs.with_update(
                        existing,
                        {
                            "state": "queued",
                            "error": None,
                            "finished_at": None,
                            "updated_at": _stamp(moment),
                        },
                    )
                    runs._save(conn, existing)
                    runs._log(conn, existing.id, "info", "restarted after a failure")
                return _row_out(existing, conn) | {"resumed": True}
            fp = runs.Fingerprint(inputs=(("audio", sha),), config=document)
            params = _params(version_id, document)
            key = runs.start_key(OPERATION, params, caller.rank) if request_id else None
            budget = {"unit": "seconds", "amount": str(budget_seconds)} if budget_seconds else None
            run = runs.build(
                HANDLER, caller, params, fp, request_id=request_id, key=key, budget=budget,
                now=_stamp(moment),
            )  # fmt: skip
            op.insert_operation_run(conn, run)
            runs._log(conn, run.id, "info", f"created by {caller.actor} ({OPERATION})")
            return _row_out(run, conn) | {"resumed": False}
    finally:
        conn.close()


def _fail(store: ProjectStore, run_id: str, message: str, *, drop_chunks: bool = False) -> None:
    """Mark a run failed with ``message`` (never a completed one)."""
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            run = runs._get(conn, runs.normalise_id(run_id))
            if run.state == "completed":
                return
            now = _stamp()
            runs._save(
                conn,
                runs.with_update(
                    run,
                    {
                        "state": "failed",
                        "error": {
                            "code": "io_error",
                            "message": message[:500] or "failed",
                            "retryable": True,
                            "details": {},
                        },
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "finished_at": now,
                        "updated_at": now,
                    },
                ),
            )
            if drop_chunks:
                _drop_chunks(conn, run.id)
    finally:
        conn.close()


def _chunk_plan(total_ms: int, config: dict[str, Any]) -> list[tuple[int, int]]:
    step = config["chunk_ms"] - config["overlap_ms"]
    if total_ms <= config["chunk_ms"]:
        return [(0, total_ms)]
    count = 1 + math.ceil((total_ms - config["chunk_ms"]) / step)
    return [(i * step, min(total_ms, i * step + config["chunk_ms"])) for i in range(count)]


def execute(
    store: ProjectStore,
    home: Path,
    run_id: str,
    *,
    owner: str,
    actor: str = "transcribe",
    progress: Callable[[int, int], None] | None = None,
    engine: engines.Engine | None = None,
    now: Callable[[], datetime] | None = None,
    after_chunk: Callable[[int], None] | None = None,
    service: Any = None,
) -> dict[str, Any]:
    """Run (or resume) a transcription until it completes, fails or is cancelled. One run
    executes once per process at a time (a duplicate call is refused as busy) and the lease
    refuses another process while it is live (review cp6-a L7)."""
    final = runs.execute(
        _env(store, home, service),
        run_id,
        actor=actor,
        owner=owner,
        options={
            "engine": engine,
            "progress": progress,
            "after_chunk": after_chunk,
            "actor": actor,
        },
        clock=now,
        raise_errors=True,
    )
    out = _read_out(store, final.id)
    if final.state == "completed":
        conn = store.connect(readonly=True)
        try:
            result = runs.stage_output(conn, final.id, "result") or {}
        finally:
            conn.close()
        out |= {"source_id": result.get("source_id"), "segments": result.get("segments")}
    return out


def _execute_run(ctx: runs.RunContext) -> dict[str, Any]:
    store = ctx.store
    run_id = ctx.run.id
    engine: engines.Engine | None = ctx.options.get("engine")
    progress: Callable[[int, int], None] | None = ctx.options.get("progress")
    after_chunk: Callable[[int], None] | None = ctx.options.get("after_chunk")
    config: dict[str, Any] = dict(ctx.params["config"])
    version_id = str(ctx.params["version_id"])
    input_sha = ctx.run.input_digests[0].sha256
    sweep_stale_caches(store)
    decoded: _DecodedPcm | None = None
    try:
        decoded, chunk_rows = _prepare(ctx, version_id, input_sha)
        own_engine = engine is None
        total_ms = decoded.samples * 1000 // pcm.TARGET_RATE
        plan = _chunk_plan(total_ms, config)
        eng = engine or engines.create(
            config["engine"], home=ctx.env.home, model=config["model"], script=config["script"]
        )
        try:
            _check_engine_matches(eng, config, ctx)
            ctx.complete("decode", {"total_ms": total_ms, "chunk_count": len(plan)})
            ctx.progress(min(chunk_rows, len(plan)), len(plan), "chunks")
            for index in range(chunk_rows, len(plan)):
                ctx.checkpoint(f"chunk {index + 1} of {len(plan)}")  # stops here when cancelled
                start_ms, end_ms = plan[index]
                charged = ctx.charge(Decimal(end_ms - start_ms) / 1000)
                decoded.handle.seek(start_ms * pcm.BYTES_PER_MS)
                data = decoded.handle.read((end_ms - start_ms) * pcm.BYTES_PER_MS)
                result = eng.transcribe(data, start_ms, config["language"])
                _store_chunk(ctx, index, start_ms, end_ms, result, charged, len(plan))
                fault_point("transcribe_after_chunk")
                if after_chunk:
                    after_chunk(index)
                if progress:
                    progress(index + 1, len(plan))
            ctx.checkpoint("merge")
            return _complete(ctx, config, eng)
        finally:
            if own_engine:
                eng.close()
    except (runs.RunStop, PrometheosError):
        raise
    except Exception as exc:
        # The exception's text often names a file under the user's home: every absolute path in
        # it is replaced by its label, as in run views (round 2, F7; review R6-F3).
        raise PrometheosError(
            "io_error",
            runs.scrub(
                f"transcription failed ({type(exc).__name__}: {str(exc)[:200]}); start it again "
                "to resume from the last finished chunk"
            ),
            {"run_id": run_id, "error": type(exc).__name__},
        ) from exc
    finally:
        # Every way out of the run (done, failed, cancelled, budget pause, lease lost, record
        # removed) lets go of the decoded audio first. A run that ends because its recording is
        # gone (purged) takes the recording's caches with it, like a cancel does, whatever it
        # was doing when it found out (R5-F1); then whatever a purge could not remove while
        # this run held it is swept (decision 0031).
        try:
            if decoded is not None:
                decoded.close()
        finally:
            if not _recording_live(store, version_id, input_sha):
                _drop_caches(store, input_sha)
            sweep_stale_caches(store)


def _recording_live(store: ProjectStore, version_id: str, input_sha: str) -> bool:
    """Whether the recording this run was created for still exists unchanged. When that cannot
    be read just now, it is taken to exist (a purge cleans up after itself)."""
    try:
        conn = store.connect(readonly=True)
    except PrometheosError:
        return True
    try:
        record = reads.current_record(conn, version_id)
    except (PrometheosError, sqlite3.Error):
        return True
    finally:
        conn.close()
    return (
        record is not None
        and not record["body"].get("purged")
        and record["body"].get("sha256") == input_sha
    )


def _prepare(ctx: runs.RunContext, version_id: str, input_sha: str) -> tuple[_DecodedPcm, int]:
    """Check that the recording is what the run was created for, decode it (or reuse a usable
    decode) and hold the decoded audio open. The audio SOURCE is held only while it is being
    decoded: it is released here, with any private copy of it, before the first chunk."""
    store = ctx.store
    run_id = ctx.run.id

    def alive() -> bool:
        return _recording_live(store, version_id, input_sha)

    fault_point("transcribe_before_materialize")
    with contextlib.ExitStack() as source:
        # ---- input must be exactly what the run was created for ------------------------------
        conn = store.connect(readonly=True)
        try:
            record = reads.current_record(conn, version_id)
            if (
                record is None
                or record["body"].get("purged")
                or record["body"].get("sha256") != input_sha
            ):
                with ctx.txn() as wconn:
                    _drop_chunks(wconn, run_id)
                raise PrometheosError(
                    "stale_task",
                    "the recording changed or was removed (restart required): start a new "
                    "transcription",
                    {"run_id": run_id},
                )
            owned = reads.Caller(actor=ctx.run.actor, scope="owner")
            record = audio_version(conn, store.directory, owned, version_id)
            source_path = source.enter_context(materialized(conn, store.directory, record["body"]))
            recorded = runs.stage_output(conn, run_id, "decode") or {}  # an earlier attempt's
            recorded_ms = int(recorded["total_ms"]) if "total_ms" in recorded else None
            chunk_rows = int(
                conn.execute(
                    "SELECT COALESCE(MAX(idx), -1) + 1 FROM transcription_chunks WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        cache = store.directory / "cache"
        cache.mkdir(exist_ok=True)
        decoded = _hold_pcm(
            source_path, cache, cache / f"pcm-{input_sha}.raw", recorded_ms, alive=alive
        )
    return decoded, chunk_rows


@dataclass
class _DecodedPcm:
    """The run's decoded 16 kHz audio, held open for the whole run.

    Holding it is what keeps the run safe from another run of the same recording that finishes
    meanwhile and removes the shared entry (``_drop_caches``): Windows refuses to remove an
    open file, and elsewhere an open file outlives its name. ``private`` is this run's own
    completed output when it could not publish one (it is removed with the run)."""

    handle: BinaryIO
    samples: int
    private: Path | None = None

    def close(self) -> None:
        self.handle.close()
        if self.private is not None:
            _discard(self.private)


def _expected_samples(source: Path) -> int | None:
    """How many 16 kHz samples decoding ``source`` yields, when its header alone says: an
    integer-PCM WAV whose rate is the target rate or a whole multiple of it (the converter then
    keeps every frame, or averages each whole group). ``None`` otherwise: other rates are
    interpolated and compressed audio is only measured by decoding it."""
    try:
        info = wav.inspect_wav(source)
    except PrometheosError:
        return None
    if info.rate % pcm.TARGET_RATE:
        return None
    return int(info.frames // (info.rate // pcm.TARGET_RATE))


def _open_entry(
    entry: Path, expected_samples: int | None, recorded_ms: int | None
) -> _DecodedPcm | None:
    """Open an existing decoded entry for reuse, or ``None`` (and the caller decodes again and
    replaces it).

    What is checked, exactly: the entry opens now; its length is a whole number of 16-bit
    samples; its sample count equals ``expected_samples`` when the source's header determines
    it; and ``total_ms`` (sample count at 16 kHz, in whole milliseconds) equals the value this
    run's own earlier decode recorded, when there is one. What is NOT checked: the content of
    an entry of the right length (a file written whole by the decoder is not re-verified)."""
    try:
        handle = open(entry, "rb")
    except (FileNotFoundError, PermissionError):
        return None
    size = os.fstat(handle.fileno()).st_size
    samples = size // 2
    if (
        size % 2
        or (expected_samples is not None and samples != expected_samples)
        or (recorded_ms is not None and samples * 1000 // pcm.TARGET_RATE != recorded_ms)
    ):
        handle.close()
        return None
    return _DecodedPcm(handle, samples)


def _decode_private(source: Path, cache: Path, sha: str) -> tuple[Path, int]:
    tmp = cache / pending.private_name("pcm", sha)
    try:
        return tmp, wav.normalize_to_raw16k(source, tmp)
    except BaseException:
        _discard(tmp)
        raise


def _hold_pcm(
    source: Path,
    cache: Path,
    entry: Path,
    recorded_ms: int | None = None,
    *,
    alive: Callable[[], bool] | None = None,
) -> _DecodedPcm:
    """The decoded audio of ``source``, held open: the shared entry when a usable one exists or
    can be published (see ``_open_entry`` for what makes one usable), otherwise this run's own
    completed decode used privately. A concurrent run publishing or holding the same entry
    never fails this one (see ``_publish``).

    ``alive()`` says whether the recording still exists. It is asked after the decode, before
    anything is published, and again once the entry is in place (publish, then re-check): a
    purge can land during the decode or between the two (no database connection is held then),
    and it cleared the cache before these files existed, so the writer removes its own output
    and stops with ``not_found``. Its private file names the recording, so even a writer killed
    in between leaves nothing the next sweep does not remove (``pending.collect_unretained``)."""
    sha = entry.name.removeprefix("pcm-").removesuffix(".raw")
    expected = _expected_samples(source)
    existing = _open_entry(entry, expected, recorded_ms)
    if existing is not None:
        return existing
    tmp, samples = _decode_private(source, cache, sha)
    try:
        _require_alive(alive)  # the recording went while it was being decoded
        outcome = _publish(tmp, entry, lambda: _same_content(tmp, entry))
    except BaseException:
        _discard(tmp)
        raise
    fault_point("transcribe_after_pcm_publish")
    try:
        _require_alive(alive)
    except BaseException:
        _discard(tmp)  # ours, or surplus beside a peer's equal entry
        if outcome != "private":
            _discard(entry)  # the published (or the peer's equal) entry is the recording's too
        raise
    if outcome != "private":
        held = _open_entry(entry, expected, recorded_ms)
        if held is not None:
            if outcome == "equivalent":
                _discard(tmp)  # the peer's equal entry is ours to read; our copy is surplus
            return held
        if outcome == "published":  # published, then removed by a run that finished first
            tmp, samples = _decode_private(source, cache, sha)
    return _DecodedPcm(open(tmp, "rb"), samples, private=tmp)


def _check_engine_matches(
    eng: engines.Engine, config: dict[str, Any], ctx: runs.RunContext
) -> None:
    if eng.name != config["engine"] and not (
        config["engine"] == "synthetic" and eng.name == "synthetic-engine"
    ):
        raise PrometheosError(
            "stale_task", "engine differs from the run's configuration (restart required)", {}
        )
    if eng.version != config["engine_version"]:
        with ctx.txn() as conn:
            _drop_chunks(conn, ctx.run.id)
        raise PrometheosError(
            "stale_task",
            "the engine version changed since this run started (restart required): cancel it "
            "and start again",
            {"run_id": ctx.run.id},
        )


def _store_chunk(
    ctx: runs.RunContext,
    index: int,
    start_ms: int,
    end_ms: int,
    result: engines.ChunkResult,
    charged: str | None,
    count: int,
) -> None:
    payload = json.dumps(
        {
            "language": result.language,
            "segments": [
                {
                    "start_ms": s.start_ms,
                    "end_ms": s.end_ms,
                    "text": s.text,
                    "confidence": s.confidence,
                    "uncertain": s.uncertain,
                    "speaker_label": s.speaker_label,
                }
                for s in result.segments
            ],
        }
    )

    def within(conn: Any) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO transcription_chunks (run_id, idx, start_ms, end_ms, "
            "segments) VALUES (?,?,?,?,?)",
            (ctx.run.id, index, start_ms, end_ms, payload),
        )
        conn.execute(
            "UPDATE operation_runs SET progress = ? WHERE id = ?",
            (
                dumps_canonical({"done": index + 1, "total": count, "unit": "chunks"}).decode(),
                ctx.run.id,
            ),
        )

    ctx.complete(
        f"chunk:{index:05d}", {"start_ms": start_ms, "end_ms": end_ms}, charged, within=within
    )


def _drop_caches(store: ProjectStore, sha: str) -> None:
    """Decoded audio (16 kHz PCM, or a body copied to the cache) of a finished or cancelled run
    is removed: it holds restricted audio outside the media store. An unfinished run keeps it
    so a resume does not decode again (review cp6-a L5).

    Best effort, never inside a write transaction and never fatal: on Windows a file another
    handle holds open (a span clip, a second run, a virus scan) cannot be removed, and that
    must not undo a committed transcript or block a cancel. What cannot be removed now stays on
    the pending-deletion list (``store/pending.py``) and a later sweep removes it (review
    cp6-a R2-M1)."""
    pending.remove_or_list(store.directory, [f"pcm-{sha}.raw", f"audio-{sha}.bin"])


def clear_cache(store: ProjectStore, purged: Iterable[str] = ()) -> int:
    """Remove from the project's cache folder what a purge must not leave there (derived audio
    must not outlive its recording): every file except the derived files of recordings that
    are still retained, so another recording's running transcription keeps its unfinished
    output (review R6-F3). ``purged`` are the digests the purge removed. Returns how many files
    could not be removed yet.

    On Windows a file another handle holds open (a transcription still reading its decoded
    audio) cannot be removed, and a purge that has committed must still finish: each such file
    goes on the pending-deletion list, which every project open, every recovery pass, every run
    start and end, the end of every ``materialized`` block and every purge sweep."""
    return len(pending.clear_cache(store.directory, store, purged))


def cancel_active(env: runs.Env, caller: reads.Caller, record_ids: Iterable[str]) -> list[str]:
    """Ask every queued or running transcription of one of these records to stop, through the
    run service's own cancellation (a purge of a recording calls this first, so a worker stops at
    its next stage boundary instead of carrying on with audio that is being deleted). A run
    that ended meanwhile is left alone. Returns the runs asked."""
    wanted = set(record_ids)
    conn = env.store.connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, params FROM operation_runs WHERE operation = ? "
            "AND state IN ('queued', 'running')",
            (OPERATION,),
        ).fetchall()
    finally:
        conn.close()
    asked: list[str] = []
    for row in rows:
        try:
            version_id = json.loads(row["params"]).get("version_id")
        except (ValueError, AttributeError):
            continue
        if version_id not in wanted:
            continue
        try:
            runs.cancel(env, caller, row["id"])
        except PrometheosError:
            continue  # it finished or was cancelled in the meantime
        asked.append(str(row["id"]))
    return asked


def _sweep_orphans(cache: Path) -> None:
    """Remove private temporary files (``.pcm-*.tmp``, ``.audio-*.tmp``) that nobody has
    touched for a whole run lease: a process killed while decoding, or while using a private
    copy, leaves them and cannot list them. A file a live run still holds open cannot be
    removed on Windows, and elsewhere removing it leaves the holder's handle valid."""
    cutoff = time.time() - runs.LEASE_SECONDS
    for pattern in (".pcm-*.tmp", ".audio-*.tmp"):
        for orphan in cache.glob(pattern):
            try:
                if orphan.stat().st_mtime < cutoff:
                    orphan.unlink()
            except OSError:
                continue  # in use, or already gone


def sweep_stale_caches(store: ProjectStore) -> int:
    """Remove abandoned private temporary files, everything on the pending-deletion list that
    nothing holds any more, and derived files of recordings that are no longer retained;
    returns how many listed files remain."""
    _sweep_orphans(store.directory / "cache")
    fault_point("transcribe_during_sweep")
    return pending.sweep(store.directory, store)


def _complete(ctx: runs.RunContext, config: dict[str, Any], eng: engines.Engine) -> dict[str, Any]:
    store = ctx.store
    run_id = ctx.run.id
    conn = store.connect(readonly=True)
    try:
        decode = runs.stage_output(conn, run_id, "decode") or {}
        chunk_rows = conn.execute(
            "SELECT idx, start_ms, end_ms, segments FROM transcription_chunks WHERE run_id = ? "
            "ORDER BY idx",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    if len(chunk_rows) != decode.get("chunk_count"):
        raise PrometheosError("io_error", "chunk results are incomplete", {"run_id": run_id})
    merged_input = []
    languages: dict[str, int] = {}
    for chunk in chunk_rows:
        document = json.loads(chunk["segments"])
        if document["language"]:
            languages[document["language"]] = languages.get(document["language"], 0) + 1
        segs = [
            tr.RawSegment(
                i["start_ms"],
                i["end_ms"],
                i["text"],
                i["confidence"],
                i["uncertain"],
                i["speaker_label"],
            )
            for i in document["segments"]
        ]
        merged_input.append((chunk["idx"], chunk["start_ms"], chunk["end_ms"], segs))
    merged = tr.merge_chunks(merged_input)
    payload = tr.build_payload(merged)
    language = config["language"] or (
        max(languages.items(), key=lambda kv: (kv[1], kv[0]))[0] if languages else None
    )
    fault_point("transcribe_before_commit")
    result = commit_transcript(
        store,
        run_id=run_id,
        audio_version_id=str(ctx.params["version_id"]),
        payload=payload,
        total_ms=int(decode["total_ms"]),
        engine=eng.name,
        engine_version=eng.version,
        model=eng.model,
        language=language,
        parameters_digest=ctx.run.config_digest,
        actor=ctx.run.actor if ctx.options.get("actor") is None else str(ctx.options["actor"]),
        max_rank=ctx.run.caller_rank,
    )
    fault_point("transcribe_after_commit")
    ctx.artifact(result["source_id"], 1, "transcript_source")
    ctx.artifact(result["version_id"], 1, "transcript")
    ctx.result_seq = int(result["commit"]["project_seq"])
    return {
        "source_id": result["source_id"],
        "version_id": result["version_id"],
        "segments": len(payload.segments),
        "total_ms": int(decode["total_ms"]),
        "commit": result["commit"],
    }


def commit_transcript(
    store: ProjectStore,
    *,
    run_id: str,
    audio_version_id: str,
    payload: tr.TranscriptV1,
    total_ms: int,
    engine: str,
    engine_version: str,
    model: str | None,
    language: str | None,
    parameters_digest: str,
    actor: str,
    max_rank: int,
) -> dict[str, Any]:
    """``max_rank`` is the rank of whoever started the run (stored with it): the commit and
    any replay are bound to it."""
    data = tr.dump_payload(payload)
    if len(data) > INLINE_LIMIT:
        raise PrometheosError("budget_exceeded", "the transcript is larger than 8 MiB", {})
    sha = hashlib.sha256(data).hexdigest()
    # Derived from the run's UUID, the same before and after the CP6 run became an oprun_
    # row (store schema 8): a commit made before the migration still replays.
    request_id = derived_request_id("transcript", store.project_id, run_id.removeprefix("oprun_"))
    request_sha = canonical_sha256({"op": "transcript_commit", "run": run_id, "sha256": sha})

    def handler(txn: Txn) -> None:
        audio = live(txn.get(audio_version_id))
        if audio is None or audio.kind != "version":
            raise PrometheosError("not_found", "the recording no longer exists", {})
        audio_source = live(txn.get(audio.body["source_id"]))
        if audio_source is None:
            raise PrometheosError("not_found", "the recording's source no longer exists", {})
        rank = max(
            SENSITIVITY_ORDER[audio.sensitivity], SENSITIVITY_ORDER[audio_source.sensitivity]
        )
        sensitivity = RANK_NAME[rank]  # derived output inherits its highest input
        title = f"Transcript: {audio_source.body['title']}"[:300]
        source_body = SourceBody(
            title=title,
            origin="recording",
            origin_uri=None,
            root_binding=None,
            retention="copy",
            description=f"Transcription made with {engine} {engine_version}",
        ).model_dump(mode="json")
        source_id = f"source_{uuid.uuid4()}"
        version_id = f"version_{uuid.uuid4()}"
        txn.write_revision(
            source_id, "source", 1, txn.insert_body(source_body), sensitivity, "active", None,
            "transcript_commit", body=source_body,
        )  # fmt: skip
        txn.result(source_id, 1)
        txn.conn.execute(
            "INSERT OR IGNORE INTO payloads (sha256, data, byte_length, created_seq) "
            "VALUES (?,?,?,?)",
            (sha, data, len(data), txn.seq),
        )
        txn.op_index = 1
        version_body = VersionBody(
            source_id=source_id,
            sha256=sha,
            byte_length=len(data),
            mime="application/json",
            captured_at=txn.recorded_at,
            storage="body",
            storage_key=f"payload:{sha}",
            availability="retained",
            extraction="complete",
            derived_from={  # type: ignore[arg-type]
                "source_id": audio.body["source_id"],
                "version_id": audio_version_id,
                "locator": {"type": "audio", "start_ms": 0, "end_ms": total_ms},
                "quote_sha256": None,
            },
            payload_schema="transcript_v1",
            transformation=Transformation(
                type="transcription",
                engine=engine,
                engine_version=engine_version,
                model=model,
                language=language,
                parameters_digest=parameters_digest,
                input_version_refs=[audio_version_id],
                run_id=None,
            ),
        ).model_dump(mode="json")
        txn.write_revision(
            version_id, "version", 1, txn.insert_body(version_body), sensitivity, "active", None,
            "transcript_commit", body=version_body,
        )  # fmt: skip
        derived.index_text(txn.conn, version_id, "version", title, tr.quote_text(payload.segments))
        txn.result(version_id, 1)

    response = store.commit(
        request_id=request_id,
        request_sha256=request_sha,
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=max_rank,
    )
    return {
        "source_id": response["results"][0]["id"],
        "version_id": response["results"][1]["id"],
        "commit": response,
    }


# ----- reads and control ---------------------------------------------------------------------


def _transcription(conn: sqlite3.Connection, run_id: str) -> op.OperationRun:
    run = op.get_operation_run(conn, runs.normalise_id(run_id))
    if run is None or run.operation != OPERATION:
        raise PrometheosError("not_found", "transcription run not found", {"run_id": run_id})
    return run


def run_status(
    conn: sqlite3.Connection, store_dir: Path, caller: reads.Caller, run_id: str
) -> dict[str, Any]:
    run = _transcription(conn, run_id)
    reads.get_record(conn, store_dir, caller, str(run.params["version_id"]))  # hidden: absent
    return _row_out(run, conn)


def list_runs(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: reads.Caller,
    version_id: str | None = None,
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM operation_runs WHERE operation = ? AND "
        "(? IS NULL OR json_extract(params, '$.version_id') = ?) "
        "ORDER BY created_at DESC, rowid DESC LIMIT 100",
        (OPERATION, version_id, version_id),
    ).fetchall()
    out = []
    for row in rows:
        run = op.from_row(op.OperationRun, row)
        try:
            reads.get_record(conn, store_dir, caller, str(run.params["version_id"]))
        except PrometheosError:
            continue
        out.append(_row_out(run, conn))
    return {"runs": out}


def cancel(
    store: ProjectStore, caller: reads.Caller, run_id: str, now: datetime | None = None
) -> dict[str, Any]:
    """Cancel: an idle run is cancelled at once; a running one stops before its next chunk
    (``cancel_requested`` until the worker confirms). Visibility follows the recording, as in
    CP6 (decision 0013)."""
    conn = store.connect(readonly=True)
    try:
        run = _transcription(conn, run_id)
        reads.get_record(conn, store.directory, caller, str(run.params["version_id"]))
    finally:
        conn.close()
    env = _env(store, store.directory)
    done = runs.cancel(env, reads.Caller(actor=caller.actor, scope="owner"), run.id)
    return {"run_id": done["run_id"], "outcome": done["outcome"]}


# ----- transcript reads and corrections -------------------------------------------------------


def transcript_version(
    conn: sqlite3.Connection, store_dir: Path, caller: reads.Caller, version_id: str
) -> tuple[dict[str, Any], tr.TranscriptV1]:
    record = reads.get_record(conn, store_dir, caller, version_id)
    body = record["body"]
    if record["kind"] != "version" or body.get("payload_schema") != "transcript_v1":
        raise PrometheosError("validation_error", "this version is not a transcript", {})
    if body["availability"] != "retained":
        raise PrometheosError("source_unavailable", "the transcript bytes are not retained", {})
    row = conn.execute("SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)).fetchone()
    if row is None:
        raise PrometheosError("source_unavailable", "the transcript bytes are missing", {})
    return record, tr.parse_payload(bytes(row["data"]))


def latest_transcript_version(
    conn: sqlite3.Connection, source_id: str, max_rank: int = 2
) -> str | None:
    """The newest active version of a transcript source the caller may see, found through the
    reference index (no scan of every version body); deleted, archived or hidden versions never
    count as "latest" (review cp6-a L6)."""
    ranks = tuple(name for name, rank in SENSITIVITY_ORDER.items() if rank <= max_rank)
    marks = ",".join("?" for _ in ranks)
    row = conn.execute(
        "SELECT r.id FROM refs f JOIN records r ON r.id = f.from_id WHERE f.to_id = ? "  # noqa: S608
        "AND f.field = 'source_id' AND r.kind = 'version' AND r.lifecycle = 'active' "
        f"AND r.sensitivity IN ({marks}) ORDER BY r.created_seq DESC, r.rowid DESC LIMIT 1",
        (source_id, *ranks),
    ).fetchone()
    return str(row["id"]) if row else None


def transcript_view(
    conn: sqlite3.Connection, store_dir: Path, caller: reads.Caller, version_id: str
) -> dict[str, Any]:
    """Segments, provenance, the audio version it was made from and later corrections."""
    record, payload = transcript_version(conn, store_dir, caller, version_id)
    body = record["body"]
    transformation = body["transformation"]
    audio_id = (transformation["input_version_refs"] or [None])[0] if transformation else None
    if transformation and transformation["type"] == "conversion" and body.get("derived_from"):
        audio_id = body["derived_from"]["version_id"]
    audio_available = False
    if audio_id:
        try:
            audio_available = (
                reads.get_record(conn, store_dir, caller, audio_id)["body"]["availability"]
                == "retained"
            )
        except PrometheosError:
            audio_id = None
    latest = latest_transcript_version(conn, body["source_id"], caller.rank)
    return {
        "version_id": version_id,
        "source_id": body["source_id"],
        "sensitivity": record["sensitivity"],
        "payload": tr.as_json(payload),
        "duration_ms": payload.duration_ms,
        "transformation": transformation,
        "audio_version_id": audio_id,
        "audio_available": audio_available,
        "latest_version_id": latest,
        "superseded_by_correction": latest is not None and latest != version_id,
    }


@dataclass(frozen=True)
class Edit:
    segment_id: str
    text: str | None = None
    speaker_label: str | None = None
    uncertain: bool | None = None


def correct(
    store: ProjectStore,
    caller: reads.Caller,
    version_id: str,
    edits: list[Edit],
    *,
    actor: str,
    request_id: str,
) -> dict[str, Any]:
    """A correction is a new version of the same transcript source; nothing is overwritten."""
    if not edits or len(edits) > 5000:
        raise PrometheosError("validation_error", "give between 1 and 5000 edits", {})
    conn = store.connect(readonly=True)
    try:
        record, payload = transcript_version(conn, store.directory, caller, version_id)
    finally:
        conn.close()
    by_id = {s.id: s for s in payload.segments}
    changed = dict(by_id)
    for edit in edits:
        if edit.segment_id not in by_id:
            raise PrometheosError(
                "validation_error", "unknown segment id", {"segment_id": edit.segment_id}
            )
        current = changed[edit.segment_id]
        update: dict[str, Any] = {}
        if edit.text is not None:
            update["text"] = edit.text
        if edit.speaker_label is not None:
            update["speaker_label"] = edit.speaker_label or None
        if edit.uncertain is not None:
            update["uncertain"] = edit.uncertain
        changed[edit.segment_id] = tr.Segment.model_validate(
            {**current.model_dump(mode="json"), **update}
        )
    new_payload = tr.TranscriptV1(
        schema_version=1, segments=[changed[s.id] for s in payload.segments]
    )
    if tr.dump_payload(new_payload) == tr.dump_payload(payload):
        raise PrometheosError("validation_error", "the edits change nothing", {})
    data = tr.dump_payload(new_payload)
    if len(data) > INLINE_LIMIT:
        raise PrometheosError("budget_exceeded", "the transcript is larger than 8 MiB", {})
    sha = hashlib.sha256(data).hexdigest()
    old_body = record["body"]
    parameters = canonical_sha256(
        {
            "edits": [
                {"segment_id": e.segment_id, "text": e.text, "speaker_label": e.speaker_label,
                 "uncertain": e.uncertain}
                for e in edits
            ]
        }
    )  # fmt: skip
    rank = caller.rank
    # An agent's correction is not the researcher's: the provenance says who made it (L3).
    engine_label = "manual-correction" if caller.scope == "owner" else "agent-correction"

    def handler(txn: Txn) -> None:
        old = live(txn.get(version_id))
        if old is None:
            raise PrometheosError("not_found", "transcript version not found", {})
        latest = latest_transcript_version(txn.conn, old_body["source_id"])
        if latest != version_id:
            raise PrometheosError(
                "revision_conflict",
                "a newer version of this transcript exists; correct that one",
                {"latest_version_id": latest},
            )
        txn.conn.execute(
            "INSERT OR IGNORE INTO payloads (sha256, data, byte_length, created_seq) "
            "VALUES (?,?,?,?)",
            (sha, data, len(data), txn.seq),
        )
        body = VersionBody(
            source_id=old_body["source_id"],
            sha256=sha,
            byte_length=len(data),
            mime="application/json",
            captured_at=txn.recorded_at,
            storage="body",
            storage_key=f"payload:{sha}",
            availability="retained",
            extraction="complete",
            derived_from=old_body["derived_from"],
            payload_schema="transcript_v1",
            transformation=Transformation(
                type="conversion",
                engine=engine_label,
                engine_version="1",
                model=None,
                language=(old_body["transformation"] or {}).get("language"),
                parameters_digest=parameters,
                input_version_refs=[version_id],
                run_id=None,
            ),
        ).model_dump(mode="json")
        new_id = f"version_{uuid.uuid4()}"
        txn.write_revision(
            new_id, "version", 1, txn.insert_body(body), old.sensitivity, "active", None,
            "transcript_correct", body=body,
        )  # fmt: skip
        title = str(txn.get(old_body["source_id"]).body.get("title", ""))  # type: ignore[union-attr]
        derived.index_text(txn.conn, new_id, "version", title, tr.quote_text(new_payload.segments))
        txn.result(new_id, 1)

    response = store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256(
            {"op": "transcript_correct", "version": version_id, "sha256": sha}
        ),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=rank,
    )
    return {
        "version_id": response["results"][0]["id"],
        "previous_version_id": version_id,
        "segments_changed": sum(1 for s in new_payload.segments if s != by_id[s.id]),
        "commit": response,
    }


def audio_evidence_detail(
    conn: sqlite3.Connection, store_dir: Path, caller: reads.Caller, evidence: dict[str, Any]
) -> dict[str, Any] | None:
    """What an audio-locator evidence reference says now: the cited text, its clock, and
    whether a later correction of the transcript changed the cited segments. ``None`` when
    the cited version is not a transcript this caller may read."""
    locator = evidence["locator"]
    try:
        record, payload = transcript_version(conn, store_dir, caller, evidence["version_id"])
    except PrometheosError:
        return None
    segments = tr.span_segments(payload, locator["start_ms"], locator["end_ms"])
    latest = latest_transcript_version(conn, record["body"]["source_id"], caller.rank)
    newer: str | None = None
    changed = False
    if latest is not None and latest != evidence["version_id"]:
        try:
            _record, newest = transcript_version(conn, store_dir, caller, latest)
            newer = latest
            current = {s.id: s.text for s in newest.segments}
            changed = any(current.get(s.id, s.text) != s.text for s in segments)
        except PrometheosError:
            newer = None
    return {
        "version_id": evidence["version_id"],
        "start_ms": locator["start_ms"],
        "end_ms": locator["end_ms"],
        "clock": f"{tr.clock(locator['start_ms'])} - {tr.clock(locator['end_ms'])}",
        "segment_ids": [s.id for s in segments],
        "quote": tr.quote_text(segments),
        "uncertain": any(s.uncertain for s in segments),
        "newer_version_id": newer,
        "text_changed_since": changed,
    }


def span_report(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: reads.Caller,
    version_id: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    """The transcript text a locator would cite, for sample-span verification (E05)."""
    record, payload = transcript_version(conn, store_dir, caller, version_id)
    segments = tr.validate_locator(payload, start_ms, end_ms)
    return {
        "version_id": version_id,
        "source_id": record["body"]["source_id"],
        "locator": {"type": "audio", "start_ms": start_ms, "end_ms": end_ms},
        "segments": [s.model_dump(mode="json") for s in segments],
        "quote": tr.quote_text(segments),
        "quote_sha256": tr.quote_sha256(segments),
        "clock": f"{tr.clock(start_ms)} - {tr.clock(end_ms)}",
    }
