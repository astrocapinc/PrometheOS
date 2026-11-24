"""The durable pending-deletion list (``store/pending.py``; CP10 fixtures round 6, findings
R5-F2 and R5-F5 of the round-5 review; decision 0031).

A media or cache file that must go but cannot be removed now (Windows: it is open) is listed;
the list is swept when a project is opened, in every recovery pass, when a ``materialized``
block ends, at every run start and end, and by every purge. Changes are merged, never
overwritten: no writer loses another's entries. A listed media file that a ``media`` row refers
to again is live data and is never removed. Synthetic data only."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from audio_support import MIC, AudioEnv, ScriptedBackend, ScriptedSource
from cp1_support import AGENT, OWNER, OWNER_ACT
from cp7_support import create, paper_body
from test_cp10_decode_cache import LONG_PLAN, Competitor, competitor  # noqa: F401
from test_cp10_purge_running import purge
from test_cp10_runs_kill import (  # noqa: F401
    _reap_owned_workers,
    kill,
    start_worker,
    wait_for_progress,
)

from prometheos import faults
from prometheos.app import runs, sources, texcompile
from prometheos.audio import engines, importing, recorder, transcribe
from prometheos.domain.ids import new_id, new_request_id
from prometheos.hub.app import create_app
from prometheos.hub.security import Gate as HubGate
from prometheos.store import pending
from prometheos.store.lock import maintenance_lock
from prometheos.store.project import ProjectStore

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="relies on Windows refusing to remove an open file"
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AudioEnv:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    return AudioEnv(tmp_path)


def listed(env: AudioEnv) -> list[str]:
    path = env.store_dir / "cache" / pending.LIST_NAME
    return path.read_text("utf-8").split() if path.exists() else []


def media_recording(env: AudioEnv, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], Path]:
    monkeypatch.setattr(sources, "INLINE_LIMIT", 64 * 1024)
    imported = env.import_audio("m.wav", sensitivity="normal", plan=LONG_PLAN)
    body = env.svc.get(env.project_id, OWNER, imported["version_id"], None)["body"]
    assert body["storage"] == "media"
    return imported, env.store_dir / "media" / str(body["storage_key"])


# ----- the list: grammar and merging -------------------------------------------------------------


def test_an_entry_that_names_nothing_removable_is_never_listed_or_acted_on(env: AudioEnv) -> None:
    store_dir = env.store_dir
    keep = [store_dir / "record.sqlite", store_dir / "cache" / "a.bin"]
    keep[1].parent.mkdir(exist_ok=True)
    keep[1].write_bytes(b"x")
    pending.add(store_dir, ["../record.sqlite", "media/../record.sqlite", "cache/a/b", "C:/x"])
    pending.add(store_dir, ["", "media/", "media/a/b/c", "/etc/passwd", "cache/..\\x"])
    assert listed(env) == []
    assert all(path.exists() for path in keep)


def test_concurrent_writers_in_other_processes_lose_no_entry(env: AudioEnv) -> None:
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from prometheos.store import pending
        store_dir, who = Path(sys.argv[1]), sys.argv[2]
        for index in range(25):
            pending.add(store_dir, [f"pcm-{who}-{index}.raw"])
        """
    )
    (env.store_dir / "cache").mkdir(exist_ok=True)
    workers = [
        subprocess.Popen([sys.executable, "-c", script, str(env.store_dir), str(n)])
        for n in range(4)
    ]
    assert [w.wait(120) for w in workers] == [0, 0, 0, 0]
    assert sorted(listed(env)) == sorted(f"pcm-{n}-{i}.raw" for n in range(4) for i in range(25))


@WINDOWS_ONLY
def test_an_entry_is_not_lost_when_another_opener_locked_the_new_lock_file_first(
    env: AudioEnv,
) -> None:
    """Regression (int6b): two openers of a new, empty lock file both see size 0. The first one
    locks byte 0; the second's initial write then fails on Windows (a locked range). That write
    used to escape ``_locked`` and make ``add`` drop its entries; it now waits for the lock."""
    from prometheos.store.lock import _try_lock, _unlock

    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "pcm-raced.raw").write_bytes(b"x")
    held = open(cache / "pcm-raced.raw", "rb")  # still held: it must stay listed
    lock = env.store_dir / pending.LOCK_NAME
    lock.unlink(missing_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)  # the first opener: no write yet
    assert os.fstat(fd).st_size == 0 and _try_lock(fd)

    def release() -> None:
        time.sleep(0.5)
        _unlock(fd)
        os.close(fd)

    releaser = threading.Thread(target=release)
    releaser.start()
    try:
        pending.add(env.store_dir, ["pcm-raced.raw"])
        releaser.join(10)
        assert listed(env) == ["pcm-raced.raw"]
    finally:
        held.close()


def test_the_maintenance_lock_waits_when_another_opener_locked_its_new_file_first(
    env: AudioEnv,
) -> None:
    """Regression (decision 0048, D38-F1): the maintenance lock had decision 0038's first-write
    race. A second opener of a new, empty lock file raised PermissionError on Windows instead of
    waiting (a backup or purge failing with an I/O error rather than ``busy`` or success)."""
    from prometheos.store.lock import _try_lock, _unlock

    path = env.store_dir / "maintenance.lock"
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)  # the first opener: no write yet
    assert os.fstat(fd).st_size == 0 and _try_lock(fd)

    def release() -> None:
        time.sleep(0.5)
        _unlock(fd)
        os.close(fd)

    releaser = threading.Thread(target=release)
    releaser.start()
    started = time.monotonic()
    try:
        with maintenance_lock(env.store_dir, timeout=10):
            waited = time.monotonic() - started
    finally:
        releaser.join(10)
    assert waited >= 0.4  # it waited for the first opener instead of failing


@WINDOWS_ONLY
def test_a_sweep_running_beside_writers_loses_no_entry_for_a_file_still_held(env: AudioEnv) -> None:
    """Held files stay listed however writers and sweeps interleave; released ones go."""
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    names = [f"pcm-held-{n}.raw" for n in range(40)]
    handles = []
    for name in names:
        (cache / name).write_bytes(b"x")
        handles.append(open(cache / name, "rb"))
    stop = threading.Event()

    def sweeper() -> None:
        while not stop.is_set():
            pending.sweep(env.store_dir)

    sweeping = threading.Thread(target=sweeper)
    sweeping.start()
    writers = [
        threading.Thread(target=pending.add, args=(env.store_dir, names[n::4])) for n in range(4)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(60)
    stop.set()
    sweeping.join(60)
    try:
        assert sorted(listed(env)) == sorted(names)  # every held file is still listed
    finally:
        for handle in handles:
            handle.close()
    assert pending.sweep(env.store_dir) == 0
    assert listed(env) == [] and not any((cache / name).exists() for name in names)


# ----- media: purge always finishes, and a file that is live again is never removed --------------


@WINDOWS_ONLY
def test_a_purge_while_the_media_file_is_open_finishes_and_the_file_goes_when_released(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    imported, media = media_recording(env, monkeypatch)
    with open(media, "rb"):  # a decoder, a playback slice or a backup has it open
        receipt = purge(env, imported["source_id"])["purge_receipt"]
        assert receipt["media_files_pending_deletion"] == 1
        assert receipt["media_files_removed_now"] == 0 and receipt["database_compacted"] is True
        assert any("retained media" in text for text in receipt["limitations"])
        assert media.exists() and len(listed(env)) == 1
    env.svc.open(env.project_id)  # opening the project sweeps the list
    assert not media.exists() and listed(env) == []


@WINDOWS_ONLY
def test_a_recording_imported_again_while_its_purged_file_is_pending_is_not_removed(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Media files are content-addressed: importing the same recording again reuses the file the
    purge could not remove. The sweep must see the new reference and keep the file."""
    imported, media = media_recording(env, monkeypatch)
    with open(media, "rb"):
        purge(env, imported["source_id"])
        again = env.import_audio("m2.wav", sensitivity="normal", plan=LONG_PLAN)
        assert media.exists()
    again_body = env.svc.get(env.project_id, OWNER, again["version_id"], None)["body"]
    assert again_body["storage"] == "media"
    env.svc.open(env.project_id)  # the sweep
    assert media.exists() and listed(env) == []  # live data again: the entry is dropped
    assert env.svc.get(env.project_id, OWNER, again["version_id"], None)["body"][
        "availability"
    ] == ("retained")


def sweep_right_after_staging(
    env: AudioEnv, media: Path, monkeypatch: pytest.MonkeyPatch
) -> list[int]:
    """From now on every media staging is followed at once, before the import can commit its
    reference, by a sweep from another thread; returns what those sweeps left listed. Staging
    must reuse ``media`` (the same bytes), and the sweep must leave it in place."""
    before = media.stat().st_mtime_ns
    real_stage = sources._stage_media
    swept: list[int] = []

    def stage_then_sweep_elsewhere(store: Any, path: Path, sha: str) -> str:
        staged = real_stage(store, path, sha)
        assert media.stat().st_mtime_ns == before  # the listed file itself was reused
        sweeper = threading.Thread(
            target=lambda: swept.append(pending.sweep(env.store_dir, ProjectStore(env.store_dir)))
        )
        sweeper.start()
        sweeper.join(30)
        assert media.exists()  # the sweep could not take the file from under this import
        return staged

    monkeypatch.setattr(sources, "_stage_media", stage_then_sweep_elsewhere)
    return swept


@WINDOWS_ONLY
def test_a_listed_media_file_an_import_reuses_is_not_swept_before_its_reference_commits(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorder's import of bytes identical to a purged recording whose file is still listed
    REUSES that file (media is content-addressed). Every media import stages under the
    maintenance lock and holds it until its reference commits, so a sweep from elsewhere in
    between leaves the file alone; afterwards the entry is dropped and the file kept (round 6:
    the recorder and compile imports used to stage before taking the lock)."""
    imported, media = media_recording(env, monkeypatch)
    with open(media, "rb"):  # a reader holds it: the purge lists it
        purge(env, imported["source_id"])
    key = media.relative_to(env.store_dir / "media").as_posix()
    assert media.exists() and listed(env) == [pending.media_entry(key)]
    monkeypatch.setattr(importing, "INLINE_LIMIT", 64 * 1024)
    swept = sweep_right_after_staging(env, media, monkeypatch)
    again = importing.import_recording(
        ProjectStore(env.store_dir),  # not Service.open: that would sweep the file first
        env.research / "m.wav",
        title="the same bytes again",
        sensitivity="normal",
        description="",
        actor="test",
        request_id=new_request_id(),
        max_rank=2,
    )
    assert swept == [1]  # the sweep ran in between and left the entry for later
    env.svc.open(env.project_id)  # the next sweep: referenced again, so the entry is dropped
    assert media.exists() and listed(env) == []
    body = env.svc.get(env.project_id, OWNER, again["version_id"], None)["body"]
    assert body["storage"] == "media" and body["availability"] == "retained"


COMPILE_TRANSFORMATION = {
    "type": "conversion",
    "engine": "tectonic",
    "engine_version": "synthetic-test",
    "model": None,
    "language": None,
    "parameters_digest": "0" * 64,
    "input_version_refs": [],
    "run_id": None,
}


@WINDOWS_ONLY
def test_a_listed_compile_output_a_recompile_reuses_is_not_swept_before_its_reference_commits(
    env: AudioEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule for compile outputs (``texcompile._register_outputs``): a compile that
    yields the same PDF bytes as a purged output whose media file is still listed reuses it."""
    paper_id = new_id("paper")
    env.apply([create("paper", paper_body(title="Synthetic compile paper"), paper_id)])
    monkeypatch.setattr(sources, "INLINE_LIMIT", 1024)  # same code path as an 8 MiB PDF
    pdf = tmp_path / "out.pdf"
    pdf.write_bytes(b"%PDF-1.7 synthetic\n" + bytes(range(256)) * 8)

    def register() -> dict[str, Any]:
        return texcompile._register_outputs(
            ProjectStore(env.store_dir),  # not Service.open: that would sweep the file first
            paper_id=paper_id,
            paper_title="Synthetic compile paper",
            outputs={"pdf": (pdf.read_bytes(), pdf)},
            transformation=COMPILE_TRANSFORMATION,
            sensitivity="normal",
            actor="test",
            request_id=new_request_id(),
            max_rank=2,
        )

    def created(commit: dict[str, Any], kind: str) -> str:
        return next(str(r["id"]) for r in commit["results"] if str(r["id"]).startswith(kind))

    first = register()
    body = env.svc.get(env.project_id, OWNER, created(first, "version_"), None)["body"]
    assert body["storage"] == "media"
    media = env.store_dir / "media" / str(body["storage_key"])
    with open(media, "rb"):  # a reader holds it: the purge lists it
        purge(env, created(first, "source_"))
    assert media.exists() and listed(env) == [pending.media_entry(str(body["storage_key"]))]
    swept = sweep_right_after_staging(env, media, monkeypatch)
    again = register()
    assert swept == [1]
    env.svc.open(env.project_id)
    assert media.exists() and listed(env) == []
    body = env.svc.get(env.project_id, OWNER, created(again, "version_"), None)["body"]
    assert body["storage"] == "media" and body["availability"] == "retained"


def test_media_entries_are_only_swept_when_the_database_can_say_they_are_unreferenced(
    env: AudioEnv,
) -> None:
    key = "ab/" + "ab" * 32
    media = env.store_dir / "media" / key
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x")
    pending.add(env.store_dir, [pending.media_entry(key)])
    assert pending.sweep(env.store_dir) == 1 and media.exists()  # no store given: not touched
    assert pending.sweep(env.store_dir, env.svc.open(env.project_id)) == 0
    assert not media.exists()


# ----- every place that sweeps -------------------------------------------------------------------


def _list_a_free_file(env: AudioEnv, name: str = "pcm-free.raw") -> Path:
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    (cache / name).write_bytes(b"x")
    pending.add(env.store_dir, [name])
    return cache / name


def test_opening_the_project_sweeps(env: AudioEnv) -> None:
    leftover = _list_a_free_file(env)
    env.svc.open(env.project_id)
    assert not leftover.exists() and listed(env) == []


def test_a_recovery_pass_sweeps_even_when_it_has_no_run_to_recover(env: AudioEnv) -> None:
    store = env.svc.open(env.project_id)
    leftover = _list_a_free_file(env)  # listed after the open's own sweep
    assert runs.recover(runs.Env(env.svc, env.project_id, store)) == []  # no run to recover
    assert not leftover.exists() and listed(env) == []


def test_the_end_of_a_materialized_block_sweeps(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    body = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]
    conn = env.svc.open(env.project_id).connect(readonly=True)
    with closing(conn):
        leftover = _list_a_free_file(env)
        with transcribe.materialized(conn, env.store_dir, body):
            assert leftover.exists()
        assert not leftover.exists() and listed(env) == []


def test_the_end_of_a_run_sweeps(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    leftover = env.store_dir / "cache" / "pcm-listed-mid-run.raw"

    def list_one(index: int) -> None:
        if index == 0:  # after the run's own start-of-run sweep
            leftover.write_bytes(b"x")
            pending.add(env.store_dir, [leftover.name])

    store = env.svc.open(env.project_id)
    transcribe.execute(store, env.svc.home, run["run_id"], owner="p", after_chunk=list_one)
    assert not leftover.exists() and listed(env) == []


@WINDOWS_ONLY
def test_files_a_purge_left_for_a_killed_worker_go_without_another_transcription(
    env: AudioEnv,
) -> None:
    """The purged run's record is gone, so crash recovery has nothing to recover: opening the
    project (or any recovery pass) is what removes what the dead worker's purge left listed."""
    audio = env.import_audio("run.wav", sensitivity="normal", plan=LONG_PLAN)
    recording = (env.research / "run.wav").read_bytes()
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    worker = start_worker(
        env.home, env.project_id, run["run_id"], PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="300"
    )
    wait_for_progress(worker, env.store_dir, run["run_id"], 1)
    receipt = purge(env, audio["source_id"])["purge_receipt"]
    assert receipt["cache_files_pending_deletion"] >= 1  # the worker holds its decoded audio
    kill(worker)
    assert listed(env) != []  # nothing runs any more; nothing has swept
    env.svc.open(env.project_id)
    assert listed(env) == []
    assert not [
        p
        for p in env.home.rglob("*")
        if p.is_file()
        and not p.name.startswith("record.sqlite")
        and recording[20000:21000] in p.read_bytes()
    ]


# ----- the backstop: derived files of bytes that are no longer retained --------------------------


def test_derived_cache_files_of_a_recording_that_is_not_retained_are_collected(
    env: AudioEnv,
) -> None:
    """Whatever way such a file came to exist (a writer killed between writing and checking, a
    copy made after a purge cleared the cache), the next sweep with the store removes it; the
    files of a retained recording, and everything that is not derived audio, stay."""
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    live = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]["sha256"]
    gone = "e" * 64
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    doomed = [
        cache / f"audio-{gone}.bin",
        cache / f"pcm-{gone}.raw",
        cache / pending.private_name("pcm", gone),
        cache / pending.private_name("audio", gone),
    ]
    kept = [
        cache / f"audio-{live}.bin",
        cache / f"pcm-{live}.raw",
        cache / pending.private_name("pcm", live),
        cache / pending.private_name("audio", live),
        cache / "media-verify.json",
        cache / "notes-on-pcm.txt",
        cache / f".pcm-{gone[:12]}-not-a-private-name.tmp",  # not derived by name: age decides
    ]
    for path in [*doomed, *kept]:
        path.write_bytes(b"x")
    env.svc.open(env.project_id)  # the sweep that opening a project performs
    assert not [p for p in doomed if p.exists()]
    assert all(p.exists() for p in kept)


def test_a_file_written_after_the_purge_cleared_the_cache_is_collected(env: AudioEnv) -> None:
    """The sliver no recheck can close: a writer checked, the purge cleared the cache, the
    writer published and was killed before it could check again."""
    audio = env.import_audio("late.wav", sensitivity="normal", plan=LONG_PLAN)
    sha = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]["sha256"]
    recording = (env.research / "late.wav").read_bytes()
    purge(env, audio["source_id"])
    late = env.store_dir / "cache" / f"audio-{sha}.bin"
    late.write_bytes(recording)  # the late publication
    assert late.exists()
    env.svc.open(env.project_id)
    assert not late.exists()


def test_private_files_are_no_longer_than_the_entries_and_name_their_recording(
    env: AudioEnv,
    competitor: Callable[..., Competitor],  # noqa: F811 - the fixture imported above
) -> None:
    """Regression (round 6, development run fix6-dev5): private files named with the recording's
    whole digest and a UUID (112 characters) pushed cache paths over the Windows path limit
    where the shared entries still fit, so every transcription in a deep enough store failed
    with FileNotFoundError. A private file carries a 16-digit prefix of the digest instead:
    enough for the collector to find it once its recording is purged, and never longer than an
    entry's name."""
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    sha = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]["sha256"]
    cache = env.store_dir / "cache"
    seen: set[str] = set()
    seam = competitor("audio-", lambda _source: b"x")  # a peer holds another entry: a private copy
    with faults.inject("transcribe_during_decode", lambda: seen.update(os.listdir(cache))):
        done = env.svc.transcribe_audio(
            env.project_id, OWNER, audio["version_id"], env.synthetic_config()
        )
    assert done["state"] == "completed" and seam.published
    private = sorted(name for name in seen if name.endswith(".tmp"))
    kinds = {name.split("-")[0] for name in private}
    assert ".pcm" in kinds  # the unfinished decode
    if sys.platform == "win32":
        assert ".audio" in kinds  # the private copy (the peer's entry could not be replaced)
    assert max(len(name) for name in seen) <= len(f"audio-{sha}.bin"), sorted(seen)
    for name in private:
        match = pending.PRIVATE.match(name)
        assert match is not None and match.group(1) == sha[: pending.PREFIX], name


def test_media_entries_wait_while_another_maintenance_operation_holds_the_lock(
    env: AudioEnv,
) -> None:
    """A media import holds the maintenance lock from staging its file to committing its
    reference, so while anyone holds that lock a listed media file may be about to be used
    again: it is not removed then, and goes at the next sweep."""
    key = "cd/" + "cd" * 32
    media = env.store_dir / "media" / key
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x")
    store = env.svc.open(env.project_id)
    pending.add(env.store_dir, [pending.media_entry(key)])
    with maintenance_lock(env.store_dir):
        assert pending.sweep(env.store_dir, store) == 1 and media.exists()
    assert pending.sweep(env.store_dir, store) == 0 and not media.exists()


@WINDOWS_ONLY
def test_a_sweep_never_waits_for_a_busy_gate_and_leaves_its_work_to_the_next(
    env: AudioEnv,
) -> None:
    """A sweep is opportunistic (review R6-F2): while another thread holds the project's gate
    it returns at once instead of waiting for it, keeps the media entry it could not check and
    the derived file it could not classify, and holds up no writer of the list (round 6 waited
    up to the gate's whole budget, twice). The next sweep with the gate free does the work."""
    key = "ef/" + "ef" * 32
    media = env.store_dir / "media" / key
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x")
    store = env.svc.open(env.project_id)
    pending.add(env.store_dir, [pending.media_entry(key)])
    cache = env.store_dir / "cache"
    unretained = cache / f"pcm-{'f' * 64}.raw"  # derived from bytes the store does not retain
    unretained.write_bytes(b"x")
    held = cache / "pcm-held-by-a-run.raw"
    held.write_bytes(b"x")
    gate_taken, release = threading.Event(), threading.Event()

    def hold_the_gate() -> None:
        conn = store.connect(readonly=True)
        try:
            gate_taken.set()
            release.wait(30)
        finally:
            conn.close()

    holder = threading.Thread(target=hold_the_gate)
    holder.start()
    try:
        assert gate_taken.wait(30)
        with open(held, "rb"):
            started = time.monotonic()
            left = pending.sweep(env.store_dir, store)
            swept_in = time.monotonic() - started
            pending.add(env.store_dir, [held.name])
            added_in = time.monotonic() - started - swept_in
            assert held.name in listed(env)  # still held: still listed, not lost
    finally:
        release.set()
        holder.join(30)
    assert swept_in < 1.0, f"a sweep waited {swept_in:.1f} s for a busy gate"
    assert added_in < 1.0, f"a writer of the list waited {added_in:.1f} s"
    assert left == 1 and media.exists() and unretained.exists()  # left for the next sweep
    assert pending.sweep(env.store_dir, store) == 0  # the gate is free: this one does it
    assert not media.exists() and not unretained.exists() and not held.exists()


# ----- round 7: the list is untrusted input; sweeps never wait; recorder staging --------------

DAMAGED_LISTS = {
    "zero-filled": b"\x00" * 64,
    "a NUL line beside a valid entry": b"pcm-free.raw\n\x00\x00\x00\x00\n",
    "invalid UTF-8": b"\xff\xfe\xfa\n",
    "over the size bound": b"x" * (2 * 1024 * 1024),  # the list is read up to 1 MiB
}


@pytest.mark.parametrize("damage", sorted(DAMAGED_LISTS))
def test_a_damaged_list_never_makes_the_project_unusable(env: AudioEnv, damage: str) -> None:
    """Review R6-F1 (probes F1, F1b): every operation sweeps the list when it opens the project,
    and a crash can leave the list zero-filled. Opening the project, its info, the hub's choice
    of mode (it must not fall over instead of serving) and doctor all work; the list's valid
    entries are still honoured and it is rewritten from them; a transcription and a purge work."""
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    free = cache / "pcm-free.raw"
    free.write_bytes(b"x")
    (cache / pending.LIST_NAME).write_bytes(DAMAGED_LISTS[damage])
    env.svc.open(env.project_id)
    assert env.svc.project_info(env.project_id, OWNER)["project_id"] == env.project_id
    assert create_app(env.svc, env.project_id, HubGate(port=1)).state.maintenance is False
    assert env.svc.doctor(env.project_id)["project"]["integrity_check"] == "ok"
    assert free.exists() is (damage != "a NUL line beside a valid entry")  # valid: honoured
    assert not (cache / pending.LIST_NAME).exists()  # rewritten from its valid entries
    audio = env.import_audio("d.wav", sensitivity="restricted", plan=LONG_PLAN)
    done = env.svc.transcribe_audio(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert done["state"] == "completed"
    receipt = purge(env, audio["source_id"])["purge_receipt"]
    assert receipt["records_purged"] >= 2 and receipt["database_compacted"] is True


def test_the_cli_answers_with_a_zero_filled_list(env: AudioEnv) -> None:
    """Review R6-F1 (probe F1b): every CLI command on the project failed with "internal
    error"."""
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    (cache / pending.LIST_NAME).write_bytes(b"\x00" * 64)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PROMETHEOS_")}
    environment["PROMETHEOS_HOME"] = str(env.home)
    answer = subprocess.run(
        [sys.executable, "-m", "prometheos", "project", "info", "--project", env.project_id,
         "--json"],
        env=environment, capture_output=True, timeout=120,
    )  # fmt: skip
    assert answer.returncode == 0, answer.stdout + answer.stderr


def test_opening_the_project_never_waits_for_a_busy_gate_to_sweep(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R6-F2 (probe F2): another operation takes the project's gate right after the
    identity check. Opening used to wait for it twice to sweep (10 s); a sweep is opportunistic:
    the open returns at once, as before it swept, and leaves its work to a later sweep point."""
    audio = env.import_audio("lat.wav", sensitivity="normal", plan=LONG_PLAN)
    sha = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]["sha256"]
    store = env.svc.open(env.project_id)
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    (cache / f"pcm-{sha}.raw").write_bytes(b"\0\0" * 100)  # a paused run's decode
    key = "ab/" + "ab" * 32
    media = env.store_dir / "media" / key
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x")
    pending.add(env.store_dir, [pending.media_entry(key)])
    real_identity = ProjectStore.identity
    taken, release = threading.Event(), threading.Event()
    holders: list[threading.Thread] = []

    def hold_the_gate() -> None:
        conn = store.connect(readonly=True)
        try:
            taken.set()
            release.wait(30)
        finally:
            conn.close()

    def identity(self: ProjectStore) -> str:
        found = real_identity(self)
        if not holders:
            holders.append(threading.Thread(target=hold_the_gate))
            holders[0].start()
            assert taken.wait(10)
        return found

    monkeypatch.setattr(ProjectStore, "identity", identity)
    started = time.monotonic()
    try:
        env.svc.open(env.project_id)
        took = time.monotonic() - started
    finally:
        release.set()
        for holder in holders:
            holder.join(30)
        monkeypatch.setattr(ProjectStore, "identity", real_identity)
    assert took < 1.0, f"opening the project waited {took:.1f} s for a busy gate"
    assert media.exists() and listed(env) == [pending.media_entry(key)]  # left for later
    env.svc.open(env.project_id)  # the gate is free: this sweep does it
    assert not media.exists() and listed(env) == []
    assert (cache / f"pcm-{sha}.raw").exists()  # a retained recording's decode stays


@WINDOWS_ONLY
def test_a_recorder_staging_copy_held_at_registration_goes_with_its_purge(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R6-F5 (probe S1): a file of the session folder that another handle (a scan, an
    indexer) held while the recorder removed the folder after registering the recording used to
    stay, and stayed after the recording was purged. It is now listed when the recorder cannot
    remove it, the purge removes the purged recording's session folder or lists what is still
    held, and a sweep point removes the rest once it is released."""
    real_rmtree = shutil.rmtree
    held: dict[str, Any] = {}

    def rmtree_while_a_scanner_holds_final(path: Any, *args: Any, **kwargs: Any) -> None:
        final = Path(path) / "final.wav"
        if final.exists() and "handle" not in held:
            held["handle"] = open(final, "rb")
            held["path"] = final
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree_while_a_scanner_holds_final)
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 40)})
    request = recorder.SessionRequest(
        "Private standup", ("mic-1",), consent_acknowledged=True, sensitivity="restricted"
    )
    session = env.svc.recording_start(env.project_id, OWNER, request, backend, authority=OWNER_ACT)
    final = env.svc.recording_run(env.project_id, session["session_id"], backend, stall_seconds=30)
    assert final["state"] == "imported"
    leftover: Path = held["path"]
    recording = leftover.read_bytes()
    try:
        assert listed(env) == [f"staging/{leftover.parent.name}/final.wav"]
        receipt = purge(env, final["source_id"])["purge_receipt"]
        assert receipt["staging_files_pending_deletion"] == 1  # still held: listed
        assert any("staging" in text for text in receipt["limitations"])
    finally:
        held["handle"].close()  # the scanner lets go
    env.svc.open(env.project_id)  # a sweep point
    copies = [
        path
        for path in env.store_dir.rglob("*")
        if path.is_file()
        and not path.name.startswith("record.sqlite")
        and recording[5000:6000] in path.read_bytes()
    ]
    assert copies == [] and listed(env) == [] and not leftover.parent.exists()
