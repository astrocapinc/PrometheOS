"""Live recording sessions with recoverable segments (decision 0013 item 12).

A session reads up to two sources (a microphone and/or system loopback), converts each to
16 kHz mono, mixes them and appends raw PCM to segment files under ``staging/rec-<id>/``.
A segment is flushed and fsynced when it closes and at every heartbeat, so a crash, a
device loss or a cancel loses at most the last second. Session state lives in the store
(``recording_sessions``) so any process can show it and request pause, stop or cancel.
``finalize_session`` assembles the segments into one WAV and registers it; ``recover`` does
the same for an interrupted, cancelled or abandoned session and labels the result partial.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
import uuid
from array import array
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from prometheos.app.reads import Caller
from prometheos.audio import capture, importing, pcm, wav
from prometheos.domain.ids import derived_request_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.faults import fault_point
from prometheos.store import db, pending
from prometheos.store.project import ProjectStore

SEGMENT_SECONDS = 30
HEARTBEAT_SECONDS = 1.0
ABANDONED_AFTER = timedelta(seconds=20)
STALL_SECONDS = 0.25
MAX_BUFFER_SAMPLES = pcm.TARGET_RATE * 10
EMIT_MIN = pcm.TARGET_RATE // 20  # 50 ms
EMIT_MAX = pcm.TARGET_RATE // 5  # 200 ms
OPEN_STATES = ("recording", "paused", "interrupted", "stopped", "cancelled")
LIVE_STATES = ("recording", "paused")
CONSENT_REMINDER = (
    "Before recording, make sure every participant knows and has agreed, under the rules that "
    "apply to you. PrometheOS records only while this session shows as recording."
)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class SessionRequest:
    title: str
    device_ids: tuple[str, ...]
    sensitivity: str = importing.DEFAULT_AUDIO_SENSITIVITY
    segment_seconds: int = SEGMENT_SECONDS
    consent_acknowledged: bool = False


def session_dir(store: ProjectStore, session_id: str) -> Path:
    return store.directory / "staging" / f"rec-{session_id}"


def _out(row: sqlite3.Row, now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    heartbeat = row["heartbeat_at"]
    stale = False
    if row["state"] in LIVE_STATES and heartbeat:
        stale = moment - datetime.fromisoformat(heartbeat.replace("Z", "+00:00")) > ABANDONED_AFTER
    level = float(row["peak_level"])
    return {
        "session_id": row["id"],
        "state": row["state"],
        "title": row["title"],
        "sensitivity": row["sensitivity"],
        "backend": row["backend"],
        "sources": json.loads(row["sources"]),
        "elapsed_ms": row["frames"] * 1000 // pcm.TARGET_RATE,
        "level_dbfs": round(pcm.dbfs(level), 1),
        "level_fraction": round(level, 4),
        "pause_requested": bool(row["pause_requested"]),
        "stop_requested": bool(row["stop_requested"]),
        "cancel_requested": bool(row["cancel_requested"]),
        "abandoned": stale,
        "recoverable": row["state"] in ("interrupted", "stopped", "cancelled") or stale,
        "reason": row["reason"],
        "version_id": row["version_id"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "heartbeat_at": heartbeat,
        "consent_acknowledged": bool(row["consent_acknowledged"]),
    }


def _check_visible(row: sqlite3.Row, caller: Caller | None) -> None:
    """A session of a sensitivity above the caller's rank reads as absent (restricted
    recordings are the owner's; an agent cannot list, control, recover or even detect them)."""
    if caller is not None and SENSITIVITY_ORDER[row["sensitivity"]] > caller.rank:
        raise PrometheosError("not_found", "recording session not found", {"id": row["id"]})


def _authorize(row: sqlite3.Row, caller: Caller) -> None:
    """Visibility is not authority: stopping, cancelling, recovering or discarding a session
    needs the rank it was started at (an owner's recording is the owner's to control), on top
    of being able to see it (review cp6-a M4, L1)."""
    _check_visible(row, caller)
    if caller.rank < int(row["max_rank"]):
        raise PrometheosError(
            "policy_denied",
            "this recording was started by the owner; only the owner can control it",
            {"session_id": row["id"]},
        )


def _row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise PrometheosError("not_found", "recording session not found", {"id": session_id})
    return row


def _update(store: ProjectStore, session_id: str, **fields: Any) -> None:
    names = ", ".join(f"{k} = ?" for k in fields)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            conn.execute(
                f"UPDATE recording_sessions SET {names}, updated_at = ? WHERE id = ? "  # noqa: S608
                "AND state NOT IN ('imported','discarded')",  # terminal states never change (L2)
                (*fields.values(), _stamp(), session_id),
            )
    finally:
        conn.close()


# ----- creating and controlling ----------------------------------------------------------------


def create_session(
    store: ProjectStore,
    backend: capture.Backend,
    request: SessionRequest,
    owner: str,
    caller: Caller,
) -> dict[str, Any]:
    """``caller`` is who is recording: its rank binds the commit that registers the audio."""
    if not request.consent_acknowledged:
        raise PrometheosError(
            "policy_denied",
            "acknowledge the participant-permission reminder before recording",
            {"reminder": CONSENT_REMINDER},
        )
    if request.sensitivity not in SENSITIVITY_ORDER:
        raise PrometheosError("validation_error", "unknown sensitivity", {})
    if not request.title.strip() or len(request.title) > 300:
        raise PrometheosError("validation_error", "give the recording a title (max 300)", {})
    if not 5 <= request.segment_seconds <= 300:
        raise PrometheosError("validation_error", "segments are 5-300 seconds", {})
    ids = list(request.device_ids)
    if not 1 <= len(ids) <= 2 or len(set(ids)) != len(ids):
        raise PrometheosError("validation_error", "choose one or two different capture devices", {})
    known = {d.id: d for d in backend.devices()}
    chosen = []
    for device_id in ids:
        if device_id not in known:
            raise PrometheosError("not_found", "unknown capture device", {"device": device_id})
        chosen.append(known[device_id])
    if len(chosen) == 2 and chosen[0].kind == chosen[1].kind:
        raise PrometheosError(
            "validation_error", "choose at most one microphone and one system-audio source", {}
        )
    session_id = str(uuid.uuid4())
    now = _stamp()
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO recording_sessions (id, state, title, sensitivity, backend, sources, "
                "sample_rate, segment_seconds, lease_owner, heartbeat_at, consent_acknowledged, "
                "max_rank, started_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (
                    session_id,
                    "recording",
                    request.title.strip(),
                    request.sensitivity,
                    backend.name,
                    json.dumps([d.as_json() for d in chosen]),
                    pcm.TARGET_RATE,
                    request.segment_seconds,
                    owner,
                    now,
                    caller.rank,
                    now,
                    now,
                ),
            )
            row = _row(conn, session_id)
    finally:
        conn.close()
    session_dir(store, session_id).mkdir(parents=True, exist_ok=True)
    return _out(row)


def status(
    conn: sqlite3.Connection, session_id: str, caller: Caller | None = None
) -> dict[str, Any]:
    row = _row(conn, session_id)
    _check_visible(row, caller)
    out = _out(row)
    out["segments"] = conn.execute(
        "SELECT COUNT(*) FROM recording_segments WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    return out


def list_sessions(
    conn: sqlite3.Connection, include_done: bool = False, caller: Caller | None = None
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM recording_sessions ORDER BY started_at DESC LIMIT 100"
    ).fetchall()
    sessions = [
        _out(r)
        for r in rows
        if (include_done or r["state"] not in ("imported", "discarded"))
        and (caller is None or SENSITIVITY_ORDER[r["sensitivity"]] <= caller.rank)
    ]
    return {"sessions": sessions, "consent_reminder": CONSENT_REMINDER}


def request_flag(
    store: ProjectStore, session_id: str, flag: str, caller: Caller, value: bool = True
) -> dict[str, Any]:
    """Ask the running capture loop (any process) to pause, resume, stop or cancel."""
    column = {
        "pause": "pause_requested",
        "resume": "pause_requested",
        "stop": "stop_requested",
        "cancel": "cancel_requested",
    }[flag]
    flag_value = 0 if flag == "resume" else int(value)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            row = _row(conn, session_id)
            _authorize(row, caller)
            if row["state"] not in LIVE_STATES:
                raise PrometheosError(
                    "validation_error", f"the session is {row['state']}, not recording", {}
                )
            conn.execute(
                f"UPDATE recording_sessions SET {column} = ?, updated_at = ? WHERE id = ?",  # noqa: S608
                (flag_value, _stamp(), session_id),
            )
            return _out(_row(conn, session_id))
    finally:
        conn.close()


# ----- the capture loop ----------------------------------------------------------------------


class _Reader(threading.Thread):
    """Reads one device, converts to 16 kHz mono and buffers; never blocks the device."""

    def __init__(self, source: capture.CaptureSource) -> None:
        super().__init__(daemon=True)
        self.source = source
        self.lock = threading.Lock()
        self.buffer: array[int] = array("h")
        self.resampler = pcm.Resampler(source.info.sample_rate, pcm.TARGET_RATE)
        self.error: str | None = None
        self.ended = False
        self.last_data = time.monotonic()
        self.halt = threading.Event()

    def run(self) -> None:
        info = self.source.info
        frames = info.sample_rate * capture.BLOCK_MS // 1000
        while not self.halt.is_set():
            try:
                raw = self.source.read(frames)
            except capture.DeviceLost as exc:
                self.error = str(exc)
                return
            except Exception as exc:  # a vendor library may raise anything
                self.error = f"{type(exc).__name__}: {exc}"[:300]
                return
            if raw is None:
                self.ended = True
                return
            mono = pcm.to_mono_int16(raw, 2, info.channels)
            out = self.resampler.process(mono)
            with self.lock:
                self.buffer.extend(out)
                self.last_data = time.monotonic()
            while len(self.buffer) > MAX_BUFFER_SAMPLES and not self.halt.is_set():
                time.sleep(0.01)  # only fast synthetic sources reach this

    def take(self, count: int) -> array[int]:
        with self.lock:
            out = self.buffer[:count]
            del self.buffer[:count]
        if len(out) < count:
            out.extend(array("h", bytes(2 * (count - len(out)))))
        return out

    def available(self) -> int:
        with self.lock:
            return len(self.buffer)


class _SegmentWriter:
    def __init__(self, store: ProjectStore, session_id: str, seconds: int) -> None:
        self.store, self.session_id = store, session_id
        self.limit = seconds * pcm.TARGET_RATE
        self.directory = session_dir(store, session_id)
        self.index = 0
        self.handle: Any = None
        self.digest = hashlib.sha256()
        self.frames = 0
        self._next_index()

    def _next_index(self) -> None:
        conn = self.store.connect(readonly=True)
        try:
            last = conn.execute(
                "SELECT COALESCE(MAX(idx), 0) FROM recording_segments WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.index = int(last)

    def write(self, samples: array[int]) -> None:
        position = 0
        while position < len(samples):
            if self.handle is None:
                self.index += 1
                self.handle = open(self.directory / f"seg-{self.index:05d}.pcm", "xb")
                self.digest = hashlib.sha256()
                self.frames = 0
            take = min(len(samples) - position, self.limit - self.frames)
            data = pcm.samples_to_bytes(samples[position : position + take])
            self.handle.write(data)
            self.digest.update(data)
            self.frames += take
            position += take
            if self.frames >= self.limit:
                self.close()

    def sync(self) -> None:
        if self.handle is not None:
            self.handle.flush()
            import os

            os.fsync(self.handle.fileno())

    def close(self) -> None:
        if self.handle is None:
            return
        self.sync()
        self.handle.close()
        self.handle = None
        conn = self.store.connect()
        try:
            with db.write_transaction(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO recording_segments (session_id, idx, file, frames, "
                    "sha256) VALUES (?,?,?,?,?)",
                    (
                        self.session_id,
                        self.index,
                        f"seg-{self.index:05d}.pcm",
                        self.frames,
                        self.digest.hexdigest(),
                    ),
                )
        finally:
            conn.close()
        fault_point("recording_after_segment")


def run_session(
    store: ProjectStore,
    backend: capture.Backend,
    session_id: str,
    *,
    owner: str,
    actor: str = "recorder",
    max_seconds: float | None = None,
    stall_seconds: float = STALL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_tick: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Capture until stop, cancel, device loss, end of stream or ``max_seconds`` of audio.

    Returns the final status; a stopped session is finalized (assembled and registered)."""
    conn = store.connect(readonly=True)
    try:
        row = _row(conn, session_id)
    finally:
        conn.close()
    if row["state"] not in LIVE_STATES:
        raise PrometheosError("validation_error", f"the session is already {row['state']}", {})
    if row["lease_owner"] != owner and not _out(row)["abandoned"] and row["lease_owner"]:
        raise PrometheosError("busy", "another process is capturing this session", {})
    _update(store, session_id, lease_owner=owner, heartbeat_at=_stamp())
    infos = json.loads(row["sources"])
    readers: list[_Reader] = []
    writer = _SegmentWriter(store, session_id, row["segment_seconds"])
    outcome = "stopped"
    reason: str | None = None
    try:
        try:
            for info in infos:
                readers.append(_Reader(backend.open(info["id"])))
        except (capture.DeviceLost, PrometheosError) as exc:
            raise _Ended("interrupted", f"could not open the device: {exc}"[:300]) from exc
        for reader in readers:
            reader.start()
        _loop(store, session_id, readers, writer, owner, max_seconds, stall_seconds, sleep, on_tick)
    except _Ended as ended:
        outcome, reason = ended.state, ended.reason
    except KeyboardInterrupt:
        outcome = "stopped"
    except BaseException:
        writer.sync()  # leave the segments as safe as possible, then let it propagate
        raise
    finally:
        for reader in readers:
            reader.halt.set()
        for reader in readers:
            try:
                reader.source.close()
            except Exception:  # noqa: S110 - closing a lost device may fail
                pass
        writer.close()
    return _conclude(store, session_id, outcome, reason, actor)


class _Ended(Exception):
    def __init__(self, state: str, reason: str | None) -> None:
        super().__init__(state)
        self.state, self.reason = state, reason


def _flags(store: ProjectStore, session_id: str) -> sqlite3.Row:
    conn = store.connect(readonly=True)
    try:
        return _row(conn, session_id)
    finally:
        conn.close()


def _loop(
    store: ProjectStore,
    session_id: str,
    readers: list[_Reader],
    writer: _SegmentWriter,
    owner: str,
    max_seconds: float | None,
    stall_seconds: float,
    sleep: Callable[[float], None],
    on_tick: Callable[[dict[str, Any]], None] | None,
) -> None:
    recorded = int(_flags(store, session_id)["frames"])
    started = time.monotonic()
    last_beat = 0.0
    last_flags = 0.0
    peak = 0.0
    paused = False
    stalled_since_empty = started
    while True:
        now = time.monotonic()
        if now - last_flags >= 0.2:
            last_flags = now
            row = _flags(store, session_id)
            if row["cancel_requested"]:
                raise _Ended("cancelled", "cancelled by the researcher")
            if row["stop_requested"]:
                raise _Ended("stopped", None)
            was_paused, paused = paused, bool(row["pause_requested"])
            if paused != was_paused:
                _update(store, session_id, state="paused" if paused else "recording")
        failed = next((r for r in readers if r.error), None)
        active = [r for r in readers if not r.ended and now - r.last_data <= stall_seconds]
        if failed is not None and failed.available() < EMIT_MIN:
            raise _Ended("interrupted", f"device lost: {failed.error}"[:300])
        count = 0
        if active:
            count = min(min(r.available() for r in active), EMIT_MAX)
            if count < EMIT_MIN:
                count = 0
        elif all(r.ended or r.error for r in readers):
            left = max((r.available() for r in readers), default=0)
            if left == 0:
                if failed is not None:
                    raise _Ended("interrupted", f"device lost: {failed.error}"[:300])
                raise _Ended("stopped", None)  # a finite source ended: finish normally
            count = min(left, EMIT_MAX)
        else:
            # Every live source is quiet (for instance system audio during silence): keep
            # the timeline aligned with the clock by recording silence.
            owed = int((now - stalled_since_empty) * pcm.TARGET_RATE)
            count = min(owed, EMIT_MAX)
            if count < EMIT_MIN:
                count = 0
            else:
                stalled_since_empty = now
        if active:
            stalled_since_empty = now
        if count:
            blocks = [r.take(count) for r in readers]
            mixed = blocks[0]
            for other in blocks[1:]:
                mixed = pcm.mix(mixed, other)
            if not paused:
                writer.write(mixed)
                recorded += len(mixed)
                peak = max(peak, pcm.peak_level(mixed))
        else:
            sleep(0.01)
        if now - last_beat >= HEARTBEAT_SECONDS:
            last_beat = now
            writer.sync()
            _update(
                store, session_id, frames=recorded, peak_level=peak, heartbeat_at=_stamp(),
                lease_owner=owner,
            )  # fmt: skip
            peak = 0.0
            if on_tick:
                conn = store.connect(readonly=True)
                try:
                    on_tick(status(conn, session_id))
                finally:
                    conn.close()
        if max_seconds is not None and recorded >= max_seconds * pcm.TARGET_RATE:
            _update(store, session_id, frames=recorded)
            raise _Ended("stopped", None)


def _conclude(
    store: ProjectStore, session_id: str, outcome: str, reason: str | None, actor: str
) -> dict[str, Any]:
    conn = store.connect(readonly=True)
    try:
        frames = int(
            conn.execute(
                "SELECT COALESCE(SUM(frames), 0) FROM recording_segments WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    _update(store, session_id, state=outcome, reason=reason, frames=frames, lease_owner=None)
    if outcome == "stopped":
        return finalize_session(store, session_id, actor=actor)
    conn = store.connect(readonly=True)
    try:
        return status(conn, session_id)
    finally:
        conn.close()


# ----- assembling and recovering -------------------------------------------------------------


def _segment_files(store: ProjectStore, session_id: str) -> Iterator[tuple[Path, int, str | None]]:
    """(path, frames, registered sha or None) in order; the unregistered trailing file of a
    crashed session is truncated to whole samples by the caller."""
    directory = session_dir(store, session_id)
    conn = store.connect(readonly=True)
    try:
        registered = {
            r["file"]: (r["frames"], r["sha256"])
            for r in conn.execute(
                "SELECT file, frames, sha256 FROM recording_segments WHERE session_id = ?",
                (session_id,),
            )
        }
    finally:
        conn.close()
    for path in sorted(directory.glob("seg-*.pcm")):
        frames, sha = registered.get(path.name, (path.stat().st_size // 2, None))
        yield path, frames, sha


def finalize_session(store: ProjectStore, session_id: str, *, actor: str) -> dict[str, Any]:
    """Assemble segments into one WAV and register it as the session's recording."""
    conn = store.connect(readonly=True)
    try:
        row = _row(conn, session_id)
    finally:
        conn.close()
    if row["state"] in ("imported",):
        return _final_status(store, session_id)
    if row["state"] == "discarded":
        raise PrometheosError("validation_error", "the session was discarded", {})
    directory = session_dir(store, session_id)
    pieces = list(_segment_files(store, session_id))
    notes: list[str] = []
    for path, _frames, sha in pieces:
        if sha is not None and hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            notes.append(f"{path.name} does not match its recorded digest and was kept as found")
        if sha is None and path.stat().st_size % 2:
            with open(path, "r+b") as handle:  # an interrupted write: whole samples only
                handle.truncate(path.stat().st_size - 1)
    total = sum(path.stat().st_size // 2 for path, _f, _s in pieces)
    if total == 0:
        _update(store, session_id, state="discarded", reason="nothing was recorded")
        shutil.rmtree(directory, ignore_errors=True)
        pending.list_leftovers(store.directory, directory)
        raise PrometheosError("validation_error", "nothing was recorded in this session", {})

    def blocks() -> Iterator[bytes]:
        for path, _frames, _sha in pieces:
            with open(path, "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk[: len(chunk) - (len(chunk) % 2)]

    final = directory / "final.wav"
    wav.write_wav(final, blocks(), pcm.TARGET_RATE)
    devices = ", ".join(s["name"] for s in json.loads(row["sources"]))
    partial = row["state"] in ("interrupted", "cancelled", "recording", "paused")
    elapsed = total * 1000 // pcm.TARGET_RATE
    description = f"Recorded with {row['backend']} from {devices}; {elapsed / 1000:.1f} s."
    if partial:
        description = (
            f"Partial recording ({row['reason'] or 'recovered after an interruption'}). "
            + description
        )
    if notes:
        description += " " + "; ".join(notes)
    result = importing.import_recording(
        store,
        final,
        title=row["title"],
        sensitivity=row["sensitivity"],
        description=description,
        actor=actor,
        request_id=derived_request_id("recording", store.project_id, session_id),
        max_rank=int(row["max_rank"]),
    )
    _update(
        store, session_id, state="imported", version_id=result["version_id"], frames=total,
        lease_owner=None,
    )  # fmt: skip
    shutil.rmtree(directory, ignore_errors=True)
    # The registered copy is no longer needed here. A file another handle held just now (a
    # scan, an indexer) was not removed: it is listed, and a later sweep removes it (0031).
    pending.list_leftovers(store.directory, directory)
    return _final_status(store, session_id) | {
        "source_id": result["source_id"],
        "duration_ms": result["duration_ms"],
        "partial": partial,
    }


def _final_status(store: ProjectStore, session_id: str) -> dict[str, Any]:
    conn = store.connect(readonly=True)
    try:
        return status(conn, session_id)
    finally:
        conn.close()


def recover(store: ProjectStore, session_id: str, *, actor: str, caller: Caller) -> dict[str, Any]:
    """Register what an interrupted, cancelled or abandoned session managed to record."""
    conn = store.connect(readonly=True)
    try:
        row = _row(conn, session_id)
        _authorize(row, caller)
        info = _out(row)
    finally:
        conn.close()
    if not info["recoverable"]:
        raise PrometheosError(
            "validation_error",
            f"the session is {info['state']} and has nothing to recover",
            {"session_id": session_id},
        )
    if info["state"] in LIVE_STATES:
        _update(
            store, session_id, state="interrupted", reason="abandoned: the recorder stopped "
            "reporting", lease_owner=None,
        )  # fmt: skip
    return finalize_session(store, session_id, actor=actor)


def discard(
    store: ProjectStore, session_id: str, caller: Caller, *, confirm: bool = False
) -> dict[str, Any]:
    """Delete the segments of a session that is not registered. This cannot be undone, so it
    needs the creator's rank and an explicit confirmation."""
    conn = store.connect(readonly=True)
    try:
        row = _row(conn, session_id)
        _authorize(row, caller)
        info = _out(row)
    finally:
        conn.close()
    if not confirm:
        raise PrometheosError(
            "validation_error",
            "discarding deletes the recorded segments for good; confirm it explicitly",
            {"session_id": session_id, "confirm": "required"},
        )
    if info["state"] in LIVE_STATES and not info["abandoned"]:
        raise PrometheosError("validation_error", "stop or cancel the recording first", {})
    if info["state"] in ("imported", "discarded"):
        raise PrometheosError("validation_error", f"the session is already {info['state']}", {})
    shutil.rmtree(session_dir(store, session_id), ignore_errors=True)
    pending.list_leftovers(store.directory, session_dir(store, session_id))
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            conn.execute("DELETE FROM recording_segments WHERE session_id = ?", (session_id,))
            conn.execute(
                "UPDATE recording_sessions SET state = 'discarded', frames = 0, "
                "lease_owner = NULL, updated_at = ? WHERE id = ?",
                (_stamp(), session_id),
            )
    finally:
        conn.close()
    return {"session_id": session_id, "state": "discarded"}


def protected_directories(conn: sqlite3.Connection) -> set[str]:
    """Names under ``staging/`` the orphan collector must leave alone."""
    return {
        f"rec-{r['id']}"
        for r in conn.execute(
            "SELECT id FROM recording_sessions WHERE state IN (?,?,?,?,?)", OPEN_STATES
        )
    }
