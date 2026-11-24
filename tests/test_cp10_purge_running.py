"""A purge while a transcription runs (CP10 fixtures round 4, finding R4-F1).

A transcription holds its decoded audio open for the whole run (so a run that finishes first
cannot remove the shared cache entry from under it). On Windows a file that is held open cannot
be removed, and ``purge.commit`` clears the project's ``cache/`` folder AFTER its database
transaction has committed: everything except the derived files of recordings that are still
retained (round 7, review R6-F3: another recording's running transcription is left alone). The
purge must therefore always finish and return its receipt; a cache file it could not remove is
listed for deletion and goes as soon as nothing holds it; a purge of the recording being
transcribed asks that transcription to stop first; and no copy of a purged recording may stay
behind, whichever way the transcription ends. Synthetic recordings and the synthetic engine
only."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import threading
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from audio_support import AudioEnv
from cp1_support import AGENT, OWNER
from test_cp10_decode_cache import LONG_PLAN, transcript_end_ms
from test_cp10_runs_kill import (  # noqa: F401
    _reap_owned_workers,
    finish,
    kill,
    start_worker,
    stored,
    wait_for_progress,
)

from prometheos.app import purge as purge_mod
from prometheos.app import runs
from prometheos.app.purge import LIMITATIONS
from prometheos.audio import engines, transcribe, wav
from prometheos.domain.models import PrometheosError
from prometheos.store import lock
from prometheos.store.project import ProjectStore

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="relies on Windows refusing to remove an open file"
)


@pytest.fixture
def env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> AudioEnv:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    return AudioEnv(tmp_path)


def purge(env: AudioEnv, source_id: str) -> dict[str, Any]:
    plan = env.svc.purge_plan(env.project_id, [source_id], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    done: dict[str, Any] = env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    return done


def read_only(env: AudioEnv, sql: str) -> list[tuple[Any, ...]]:
    path = env.store_dir / "record.sqlite"
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        return [tuple(row) for row in conn.execute(sql).fetchall()]


def transcripts(env: AudioEnv) -> int:
    return int(
        read_only(
            env,
            "SELECT COUNT(*) FROM bodies b JOIN revisions v ON v.body_id = b.id "
            'WHERE b.encoded LIKE \'%"payload_schema":"transcript_v1"%\'',
        )[0][0]
    )


def copies_of(env: AudioEnv, recording: bytes) -> list[str]:
    """Files outside the database that hold a stretch of the recording."""
    return [
        path.name
        for path in env.store_dir.rglob("*")
        if path.is_file()
        and not path.name.startswith("record.sqlite")
        and recording[1000:3000] in path.read_bytes()
    ]


def cache_names(env: AudioEnv) -> list[str]:
    return sorted(p.name for p in (env.store_dir / "cache").iterdir())


def purge_during_chunk_zero(
    env: AudioEnv, run_id: str, source_id: str, seen: dict[str, Any]
) -> BaseException | dict[str, Any]:
    """Run the transcription in this thread and purge ``source_id`` right after its first chunk
    (the decoded audio is held open at that moment). Returns the run's result or its error."""

    def after_chunk(index: int) -> None:
        if index == 0:
            seen["receipt"] = purge(env, source_id)["purge_receipt"]
            seen["cache_after_purge"] = cache_names(env)

    try:
        return transcribe.execute(
            env.svc.open(env.project_id),
            env.svc.home,
            run_id,
            owner="purge-test",
            after_chunk=after_chunk,
        )
    except PrometheosError as error:
        return error


# ----- the purge always finishes ----------------------------------------------------------------


def test_a_purge_of_an_unrelated_source_mid_run_returns_its_receipt(env: AudioEnv) -> None:
    """The purge removes what derives from the recordings it purges and leaves another
    recording's running transcription alone: that decode belongs to a recording the store still
    retains (round 7, review R6-F3; this test used to expect it listed for deletion)."""
    running = env.import_audio("run.wav", sensitivity="normal", plan=LONG_PLAN)
    other = env.import_audio("other.wav", sensitivity="normal", plan=LONG_PLAN[:3])
    sha = env.svc.get(env.project_id, OWNER, running["version_id"], None)["body"]["sha256"]
    run = env.svc.transcribe_start(
        env.project_id, OWNER, running["version_id"], env.synthetic_config()
    )
    seen: dict[str, Any] = {}
    outcome = purge_during_chunk_zero(env, run["run_id"], other["source_id"], seen)
    assert isinstance(outcome, dict) and outcome["state"] == "completed"  # the run is unharmed
    receipt = seen["receipt"]  # and the purge finished: it did not fail after committing
    left = seen["cache_after_purge"]
    # What is left is the running recording's own decode: untouched, and not listed.
    assert f"pcm-{sha}.raw" in left and transcribe.STALE_LIST not in left
    assert all(sha in name for name in left), left
    assert receipt["cache_files_pending_deletion"] == 0
    assert receipt["limitations"] == list(LIMITATIONS)  # nothing pending: no extra line
    assert read_only(env, "SELECT COUNT(*) FROM purges")[0][0] == 1  # the purge is recorded
    assert read_only(env, "PRAGMA freelist_count")[0][0] == 0  # and the VACUUM ran
    assert cache_names(env) == []  # the completed run dropped its caches
    assert transcript_end_ms(env, outcome["result_version_id"]) >= 15000


def test_an_unrelated_purge_between_a_decode_and_its_publish_does_not_fail_the_run(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R6-F3 (probe G1): the purge of recording A lands after B's decode has finished and
    before B publishes it. The purge used to clear the whole cache, B's finished private decode
    included, and B failed with a retryable io_error; B's files are now left alone."""
    a = env.import_audio("a.wav", sensitivity="normal")
    b = env.import_audio("b.wav", sensitivity="normal", plan=LONG_PLAN)
    run = env.svc.transcribe_start(env.project_id, OWNER, b["version_id"], env.synthetic_config())
    me, seen = threading.get_ident(), {}
    real_publish = transcribe._publish

    def publish(tmp: Path, entry: Path, equivalent: Any) -> str:
        if threading.get_ident() == me and entry.name.startswith("pcm-") and "receipt" not in seen:
            done: dict[str, Any] = {}
            elsewhere = threading.Thread(target=lambda: done.update(purge(env, a["source_id"])))
            elsewhere.start()
            elsewhere.join(120)
            seen["receipt"] = done["purge_receipt"]
        return real_publish(tmp, entry, equivalent)

    monkeypatch.setattr(transcribe, "_publish", publish)
    done = transcribe.execute(env.svc.open(env.project_id), env.svc.home, run["run_id"], owner="g1")
    assert seen["receipt"]["records_purged"] >= 2
    assert done["state"] == "completed"
    assert transcript_end_ms(env, done["result_version_id"]) >= 15000


def test_a_failed_transcription_reports_its_error_without_a_local_path(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R6-F3: the error raised to the caller stays typed, and every local path in its
    text is replaced by a label (as run views always did) instead of naming the store."""
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    vanished = env.store_dir / "cache" / "vanished.tmp"

    def failing(source: Path, out: Path) -> int:
        raise FileNotFoundError(2, "No such file or directory", str(vanished))

    monkeypatch.setattr(wav, "normalize_to_raw16k", failing)
    with pytest.raises(PrometheosError) as err:
        transcribe.execute(env.svc.open(env.project_id), env.svc.home, run["run_id"], owner="m")
    assert err.value.code == "io_error" and err.value.details["error"] == "FileNotFoundError"
    assert "FileNotFoundError" in err.value.message
    assert re.search(r"[A-Za-z]:[\\/]", err.value.message) is None, err.value.message
    assert re.search(r"<[^<>]+>", err.value.message), err.value.message  # a labelled path


def test_the_receipt_of_a_purge_with_nothing_in_use_says_so(env: AudioEnv) -> None:
    other = env.import_audio("other.wav", sensitivity="normal", plan=LONG_PLAN[:3])
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "audio-leftover.bin").write_bytes(b"x")
    receipt = purge(env, other["source_id"])["purge_receipt"]
    assert receipt["cache_files_pending_deletion"] == 0
    assert receipt["limitations"] == list(LIMITATIONS)
    assert cache_names(env) == []


@WINDOWS_ONLY
def test_clear_cache_lists_a_file_in_use_and_the_sweep_removes_it_once_released(
    env: AudioEnv,
) -> None:
    store = env.svc.open(env.project_id)
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "pcm-free.raw").write_bytes(b"x")
    (cache / "pcm-held.raw").write_bytes(b"x")
    with open(cache / "pcm-held.raw", "rb"):
        assert transcribe.clear_cache(store) == 1
        assert (cache / transcribe.STALE_LIST).read_text("utf-8") == "pcm-held.raw\n"
        assert transcribe.sweep_stale_caches(store) == 1  # still held: still listed
    assert transcribe.sweep_stale_caches(store) == 0
    assert cache_names(env) == []


# ----- the recording being transcribed is purged ------------------------------------------------


def test_purging_the_recording_being_transcribed_stops_the_run_and_leaves_no_copy(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = env.import_audio("run.wav", sensitivity="restricted", plan=LONG_PLAN)
    recording = (env.research / "run.wav").read_bytes()
    run = env.svc.transcribe_start(
        env.project_id, OWNER, running["version_id"], env.synthetic_config()
    )
    asked: list[tuple[str, str]] = []
    real_cancel = runs.cancel

    def spying(run_env: Any, caller: Any, run_id: str) -> dict[str, Any]:
        reply = real_cancel(run_env, caller, run_id)
        asked.append((run_id, reply["outcome"]))
        return reply

    monkeypatch.setattr(runs, "cancel", spying)
    seen: dict[str, Any] = {}
    outcome = purge_during_chunk_zero(env, run["run_id"], running["source_id"], seen)
    # The purge asked the run to stop, through the run service's own cancellation, and finished.
    assert asked == [(run["run_id"], "cancel_requested")]
    assert seen["receipt"]["records_purged"] >= 2
    # The worker did not carry on: its run is gone with the recording and nothing was committed
    # from the purged audio. It ended with a typed reason, not a retryable "busy".
    assert isinstance(outcome, PrometheosError), outcome
    assert outcome.code == "not_found" and "purged" in outcome.message
    assert transcripts(env) == 0
    assert read_only(env, "SELECT COUNT(*) FROM operation_runs")[0][0] == 0
    # Neither the cache nor anything else holds the recording any more.
    assert cache_names(env) == []
    assert copies_of(env, recording) == []


def test_a_queued_transcription_of_the_purged_recording_is_cancelled_first(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = env.import_audio("queued.wav", sensitivity="restricted", plan=LONG_PLAN)
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    asked: list[tuple[str, str]] = []
    real_cancel = runs.cancel

    def spying(run_env: Any, caller: Any, run_id: str) -> dict[str, Any]:
        reply = real_cancel(run_env, caller, run_id)
        asked.append((run_id, reply["outcome"]))
        return reply

    monkeypatch.setattr(runs, "cancel", spying)
    purge(env, audio["source_id"])
    assert asked == [(run["run_id"], "cancelled")]  # nothing executed it: cancelled at once
    assert read_only(env, "SELECT COUNT(*) FROM operation_runs")[0][0] == 0


def test_an_unauthorised_purge_stops_nothing(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = env.import_audio("queued.wav", sensitivity="normal", plan=LONG_PLAN)
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    plan = env.svc.purge_plan(env.project_id, [audio["source_id"]], OWNER)
    monkeypatch.setattr(runs, "cancel", lambda *a, **k: pytest.fail("a run was cancelled"))
    with pytest.raises(PrometheosError) as err:
        env.svc.purge_commit(env.project_id, plan["plan_id"], "not-a-capability")
    assert err.value.code == "policy_denied"
    assert env.svc.run_get(env.project_id, OWNER, run["run_id"])["state"] == "queued"


def test_a_purge_refused_while_another_maintenance_operation_runs_stops_nothing(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup (or a restore, a media import, another purge) holds the maintenance lock. The
    purge takes that lock before anything else, so it is refused as busy before it has asked any
    transcription to stop, and its capability is still unused: the same grant works later."""
    audio = env.import_audio("queued.wav", sensitivity="normal", plan=LONG_PLAN)
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    plan = env.svc.purge_plan(env.project_id, [audio["source_id"]], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    # Only the wait is shortened (the lock is the real one): the default is five seconds.
    monkeypatch.setattr(purge_mod, "maintenance_lock", partial(lock.maintenance_lock, timeout=0.2))
    real_cancel = runs.cancel
    monkeypatch.setattr(runs, "cancel", lambda *a, **k: pytest.fail("a run was cancelled"))
    with lock.maintenance_lock(env.store_dir):  # the backup
        with pytest.raises(PrometheosError) as err:
            env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    assert err.value.code == "busy"
    assert env.svc.run_get(env.project_id, OWNER, run["run_id"])["state"] == "queued"
    assert read_only(env, "SELECT COUNT(*) FROM purges")[0][0] == 0
    monkeypatch.setattr(runs, "cancel", real_cancel)
    receipt = env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    assert receipt["purge_receipt"]["records_purged"] >= 2  # the grant was not used up
    assert read_only(env, "SELECT COUNT(*) FROM operation_runs")[0][0] == 0


# ----- a worker in another process ---------------------------------------------------------------


def test_purging_the_recording_a_worker_process_is_transcribing_leaves_no_copy(
    env: AudioEnv,
) -> None:
    running = env.import_audio("run.wav", sensitivity="normal", plan=LONG_PLAN)
    recording = (env.research / "run.wav").read_bytes()
    run = env.svc.transcribe_start(
        env.project_id, AGENT, running["version_id"], env.synthetic_config()
    )
    worker = start_worker(
        env.home, env.project_id, run["run_id"], PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="300"
    )
    wait_for_progress(worker, env.store_dir, run["run_id"], 1)
    receipt = purge(env, running["source_id"])["purge_receipt"]
    code, _ = finish(worker)  # the worker stops at its next stage boundary
    assert code != 0 and transcripts(env) == 0, worker.describe("the worker's end")
    assert receipt["records_purged"] >= 2
    assert cache_names(env) == [] and copies_of(env, recording) == []


@WINDOWS_ONLY
def test_a_worker_killed_while_a_purge_waits_on_its_files_leaves_nothing_after_the_next_sweep(
    env: AudioEnv,
) -> None:
    running = env.import_audio("run.wav", sensitivity="normal", plan=LONG_PLAN)
    recording = (env.research / "run.wav").read_bytes()
    run = env.svc.transcribe_start(
        env.project_id, AGENT, running["version_id"], env.synthetic_config()
    )
    worker = start_worker(
        env.home, env.project_id, run["run_id"], PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="300"
    )
    wait_for_progress(worker, env.store_dir, run["run_id"], 1)
    receipt = purge(env, running["source_id"])["purge_receipt"]
    assert receipt["cache_files_pending_deletion"] >= 1  # the worker holds its decoded audio
    kill(worker)  # no cleanup runs in a killed process: its files are still listed
    assert copies_of(env, recording) != []
    # The next sweep: here the project open of an unrelated recording's import (and then its
    # transcription's start). test_cp10_pending shows a project open alone is enough.
    other = env.import_audio("other.wav", sensitivity="normal", plan=LONG_PLAN[:3])
    env.svc.transcribe_audio(env.project_id, OWNER, other["version_id"], env.synthetic_config())
    assert copies_of(env, recording) == [] and cache_names(env) == []


def test_an_unrelated_purge_leaves_a_worker_process_alone_and_a_killed_worker_resumes(
    env: AudioEnv,
) -> None:
    """The worker's decode belongs to a recording that is still retained: a purge of another
    recording neither removes nor lists it (round 7, review R6-F3). The worker is then killed;
    recovery makes its run truthful and the resume completes from the kept decode."""
    running = env.import_audio("run.wav", sensitivity="normal", plan=LONG_PLAN)
    other = env.import_audio("other.wav", sensitivity="normal", plan=LONG_PLAN[:3])
    sha = env.svc.get(env.project_id, OWNER, running["version_id"], None)["body"]["sha256"]
    decode = env.store_dir / "cache" / f"pcm-{sha}.raw"
    run = env.svc.transcribe_start(
        env.project_id, AGENT, running["version_id"], env.synthetic_config()
    )
    worker = start_worker(
        env.home, env.project_id, run["run_id"], PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="300"
    )
    wait_for_progress(worker, env.store_dir, run["run_id"], 1)
    receipt = purge(env, other["source_id"])["purge_receipt"]  # unrelated: the run survives
    assert receipt["cache_files_pending_deletion"] == 0 and decode.exists()
    kill(worker)
    view = env.svc.run_get(env.project_id, AGENT, run["run_id"])  # recovery makes it truthful
    assert view["state"] == "queued"
    assert decode.exists()  # kept for the resume (review cp6-a L5)
    resumed = env.svc.transcribe_run(env.project_id, OWNER, run["run_id"])
    assert resumed["state"] == "completed"
    assert transcript_end_ms(env, resumed["result_version_id"]) >= 15000
    assert os.listdir(env.store_dir / "cache") == []


@WINDOWS_ONLY
def test_crash_recovery_removes_what_a_purge_left_listed_for_a_killed_worker(
    env: AudioEnv,
) -> None:
    """The worker's own recording is purged while it holds its decoded audio, then the worker
    is killed. The purge removed its run, so recovery has no run left to see: the sweep that
    starts every recovery pass removes the listed files anyway (no project open, no later
    transcription). Round 7: the purge of an UNRELATED recording no longer lists anything of
    the worker's (see the test above), so the listed files now come from its own purge."""
    running = env.import_audio("run.wav", sensitivity="normal", plan=LONG_PLAN)
    recording = (env.research / "run.wav").read_bytes()
    run = env.svc.transcribe_start(
        env.project_id, AGENT, running["version_id"], env.synthetic_config()
    )
    worker = start_worker(
        env.home, env.project_id, run["run_id"], PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="300"
    )
    wait_for_progress(worker, env.store_dir, run["run_id"], 1)
    receipt = purge(env, running["source_id"])["purge_receipt"]
    assert receipt["cache_files_pending_deletion"] >= 1  # the worker holds its decoded audio
    kill(worker)
    assert copies_of(env, recording) != []  # nothing has swept since the worker let go
    store = ProjectStore(env.store_dir)  # not Service.open, which sweeps too
    assert runs.recover(runs.Env(env.svc, env.project_id, store)) == []  # no run to recover
    assert copies_of(env, recording) == [] and cache_names(env) == []
