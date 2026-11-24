"""The purge/transcription invariant, checked at every point of the pipeline (CP10 fixtures,
round 6, after the round-5 review; decision 0031).

INVARIANT. Once a purge of recording X has returned, and after every run touching X has ended
(or the next sweep has run), no file under the project's ``media/`` or ``cache/`` (or any other
file of the project, or any temporary or private copy) contains X's bytes or bytes derived from
X; and the purge itself always returns its receipt.

Earlier rounds found the windows one at a time: a purge during a decode, during a publish, with
a media-stored recording, with a worker that was killed... This test does not look for the next
one. It injects the purge of the recording at EVERY named point of the transcription pipeline
(the ``faults.fault_point`` calls in ``transcribe.py`` and ``wav.py``), for an inline recording,
a media-stored recording and an inline recording whose decoded audio differs byte for byte from
the file, lets the run end, and scans every file of the project for the recording's bytes and
for a stretch of its decoded audio. A second matrix kills a real worker process at each point
and purges afterwards. The purge is performed from another thread, as a real one is: it must
meet the worker exactly where the point is, with the worker's files open.

The injection points, in pipeline order:

* ``before_materialize``     the run has started; nothing derived exists yet
* ``after_audio_publish``    the audio entry (or a private copy) has just been published
                             (inline recordings only: a media file is read in place). The worker
                             holds its database connection here, so a purge cannot land: it is
                             refused as busy and succeeds when retried (the matrix checks both)
* ``during_decode``          the decoder has written part of its output and still holds the
                             source and the unfinished output open
* ``after_pcm_publish``      the decoded audio has just been published
* ``between_chunks``         the first chunk is stored; the decoded audio is held open
* ``before_transcript_commit``   all chunks are done, the transcript is not committed yet
* ``after_transcript_commit``    the transcript is committed, the run is not finished yet
* ``during_sweep_at_start`` / ``during_sweep_at_end``   the pending-deletion sweep that opens and
                             closes every run
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from audio_support import SPEECH, AudioEnv
from cp1_support import AGENT, OWNER, create_entry
from test_cp10_decode_cache import LONG_PLAN
from test_cp10_runs_kill import _reap_owned_workers, finish, start_worker  # noqa: F401

from prometheos import faults
from prometheos.app import runs, sources
from prometheos.audio import engines, transcribe, wav
from prometheos.domain.models import PrometheosError
from prometheos.store import pending

# (id, fault point, which occurrence of the point the purge is injected at)
POINTS = [
    ("before_materialize", "transcribe_before_materialize", 1),
    ("after_audio_publish", "transcribe_after_audio_publish", 1),
    ("during_decode", "transcribe_during_decode", 1),
    ("after_pcm_publish", "transcribe_after_pcm_publish", 1),
    ("between_chunks", "transcribe_after_chunk", 1),
    ("before_transcript_commit", "transcribe_before_commit", 1),
    ("after_transcript_commit", "transcribe_after_commit", 1),
    ("during_sweep_at_start", "transcribe_during_sweep", 1),
    ("during_sweep_at_end", "transcribe_during_sweep", 2),
]
STORAGES = ["inline", "media", "inline-48k"]
NOT_ON_THE_PATH = {("after_audio_publish", "media")}  # a media file is read in place: no copy
HOLDS_THE_GATE = {"after_audio_publish"}  # the worker has its database connection open there


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AudioEnv:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    return AudioEnv(tmp_path)


@dataclass
class Recording:
    source_id: str
    version_id: str
    storage: str
    markers: list[bytes]


def make_recording(
    env: AudioEnv, storage: str, monkeypatch: pytest.MonkeyPatch, *, real_size: bool = False
) -> Recording:
    """A restricted-looking synthetic recording and two recognisable stretches of it: one of
    the file as imported and one of its decoded 16 kHz audio (not the same bytes at 48 kHz)."""
    plan = LONG_PLAN
    options: dict[str, Any] = {}
    if storage == "media" and real_size:
        plan = [(2000, SPEECH)] * 170  # about 10.9 MB: over the 8 MiB inline limit for real
    elif storage == "media":
        monkeypatch.setattr(sources, "INLINE_LIMIT", 64 * 1024)  # same code path, cheaply
    if storage == "inline-48k":
        options["rate"] = 48000
    imported = env.import_audio("x.wav", sensitivity="normal", plan=plan, **options)
    body = env.svc.get(env.project_id, OWNER, imported["version_id"], None)["body"]
    assert (body["storage"] == "media") == (storage == "media"), body["storage"]
    source = env.research / "x.wav"
    decoded = env.tmp / "decoded-marker.raw"
    wav.normalize_to_raw16k(source, decoded)
    markers = [source.read_bytes()[20000:21000], decoded.read_bytes()[20000:21000]]
    for marker in markers:
        assert len(set(marker)) > 8  # a stretch of real signal, not silence
    return Recording(imported["source_id"], imported["version_id"], storage, markers)


def purge_from_another_thread(env: AudioEnv, source_id: str) -> dict[str, Any]:
    """Purge ``source_id`` the way a real purge arrives: from another thread, while the worker
    is stopped at the injection point with everything it has open still open."""
    outcome: dict[str, Any] = {}

    def purge() -> None:
        try:
            plan = env.svc.purge_plan(env.project_id, [source_id], OWNER)
            grant = env.svc.grant(
                env.project_id,
                operation="purge",
                digest=plan["plan_sha256"],
                label="SYNTHETIC-TEST",
            )
            done = env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
            outcome["receipt"] = done["purge_receipt"]
        except BaseException as error:
            outcome["error"] = f"{type(error).__name__}: {error}"

    thread = threading.Thread(target=purge, name="purge-under-test")
    thread.start()
    thread.join(120)
    assert not thread.is_alive(), "the purge did not finish"
    return outcome


def read_only(env: AudioEnv, sql: str) -> list[tuple[Any, ...]]:
    path = env.store_dir / "record.sqlite"
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        return [tuple(row) for row in conn.execute(sql).fetchall()]


def holders(env: AudioEnv, markers: list[bytes]) -> list[str]:
    """Every file of the project (the database aside) that holds one of the stretches, and every
    temporary or private cache copy that exists at all."""
    found = []
    for path in env.home.rglob("*"):
        if not path.is_file() or path.name.startswith("record.sqlite"):
            continue
        data = path.read_bytes()
        if any(marker in data for marker in markers) or path.name.startswith((".pcm-", ".audio-")):
            found.append(str(path.relative_to(env.home)))
    return found


def assert_invariant(env: AudioEnv, recording: Recording, receipt: dict[str, Any] | None) -> None:
    assert receipt is not None, "the purge did not return its receipt"
    assert receipt["database_compacted"] is True and receipt["records_purged"] >= 2
    assert holders(env, recording.markers) == []
    assert pending.count(env.store_dir) == 0  # nothing holds anything: nothing may stay listed
    transcripts = read_only(
        env,
        "SELECT COUNT(*) FROM bodies b JOIN revisions v ON v.body_id = b.id "
        'WHERE b.encoded LIKE \'%"payload_schema":"transcript_v1"%\'',
    )[0][0]
    assert transcripts == 0 and read_only(env, "SELECT COUNT(*) FROM operation_runs")[0][0] == 0


# ----- the purge at every point, the worker alive ------------------------------------------------


@pytest.mark.parametrize("storage", STORAGES)
@pytest.mark.parametrize("point, fault, nth", POINTS, ids=[p[0] for p in POINTS])
def test_a_purge_at_every_point_of_the_pipeline_leaves_no_copy(
    env: AudioEnv,
    monkeypatch: pytest.MonkeyPatch,
    storage: str,
    point: str,
    fault: str,
    nth: int,
) -> None:
    if (point, storage) in NOT_ON_THE_PATH:
        pytest.skip("this point is not on the path of a media-stored recording")
    recording = make_recording(env, storage, monkeypatch)
    run = env.svc.transcribe_start(
        env.project_id, OWNER, recording.version_id, env.synthetic_config()
    )
    me = threading.get_ident()
    seen: dict[str, Any] = {"hits": 0}

    def hook() -> None:
        if threading.get_ident() != me:
            return  # the purge's own thread passes through some of the same points
        seen["hits"] += 1
        if seen["hits"] == nth:
            seen["purge"] = purge_from_another_thread(env, recording.source_id)

    with faults.inject(fault, hook):
        try:
            outcome: Any = transcribe.execute(
                env.svc.open(env.project_id), env.svc.home, run["run_id"], owner="matrix"
            )
        except PrometheosError as error:
            outcome = error
    assert "purge" in seen, f"the run never reached {point} (occurrence {nth})"
    if point in HOLDS_THE_GATE:
        # The worker holds its database connection here, so a purge from anywhere else is
        # refused as busy (it meets the admission gate): it cannot land in this window. The run
        # finishes, and the purge that is retried afterwards purges the transcript too.
        assert seen["purge"].get("error", "").startswith("PrometheosError: another project")
        assert isinstance(outcome, dict) and outcome["state"] == "completed"
        seen["purge"] = purge_from_another_thread(env, recording.source_id)
        assert "error" not in seen["purge"], seen["purge"]
    else:
        assert "error" not in seen["purge"], seen["purge"]
        # The run ended, with one typed reason whatever it was doing, and committed nothing.
        assert isinstance(outcome, PrometheosError), outcome
        assert (outcome.code, outcome.message) == ("not_found", runs.REMOVED_MESSAGE)
    assert_invariant(env, recording, seen["purge"].get("receipt"))


# ----- a worker process killed at every point, then the purge ------------------------------------


@pytest.mark.parametrize("storage", ["inline", "media"])
@pytest.mark.parametrize("point, fault, nth", POINTS[:-1], ids=[p[0] for p in POINTS[:-1]])
def test_a_purge_after_a_worker_died_at_every_point_leaves_no_copy(
    env: AudioEnv,
    monkeypatch: pytest.MonkeyPatch,
    storage: str,
    point: str,
    fault: str,
    nth: int,
) -> None:
    if (point, storage) in NOT_ON_THE_PATH or nth != 1:
        pytest.skip("not a point a killed worker reaches")
    recording = make_recording(env, storage, monkeypatch)
    run = env.svc.transcribe_start(
        env.project_id, AGENT, recording.version_id, env.synthetic_config()
    )
    worker = start_worker(env.home, env.project_id, run["run_id"], PROMETHEOS_FAULT_POINT=fault)
    code, _ = finish(worker)
    assert code == faults.FAULT_EXIT_CODE, worker.describe(f"the worker did not die at {point}")
    purged = purge_from_another_thread(env, recording.source_id)
    assert "error" not in purged, purged
    env.svc.open(env.project_id)  # a project open sweeps whatever is still listed
    assert_invariant(env, recording, purged.get("receipt"))


# ----- the windows the reviewers found, kept as regressions --------------------------------------


def test_a_recording_over_the_inline_limit_purged_while_it_is_decoded(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe M of round 5, at real size: 10.9 MB, a media-stored recording the decoder holds
    open while the owner purges it."""
    recording = make_recording(env, "media", monkeypatch, real_size=True)
    run = env.svc.transcribe_start(
        env.project_id, OWNER, recording.version_id, env.synthetic_config(chunk_ms=60000)
    )
    me, seen = threading.get_ident(), {}

    def hook() -> None:
        if threading.get_ident() == me and "purge" not in seen:
            seen["purge"] = purge_from_another_thread(env, recording.source_id)

    with faults.inject("transcribe_during_decode", hook):
        with pytest.raises(PrometheosError) as err:
            transcribe.execute(
                env.svc.open(env.project_id), env.svc.home, run["run_id"], owner="matrix"
            )
    assert err.value.message == runs.REMOVED_MESSAGE
    assert "error" not in seen["purge"], seen["purge"]
    assert_invariant(env, recording, seen["purge"].get("receipt"))


def test_a_transcript_committed_before_the_purge_makes_an_old_plan_refuse(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe T2: the transcript commits first; the purge planned before it is refused, and a
    new plan purges the recording and its transcript."""
    recording = make_recording(env, "inline", monkeypatch)
    run = env.svc.transcribe_start(
        env.project_id, OWNER, recording.version_id, env.synthetic_config()
    )
    plan = env.svc.purge_plan(env.project_id, [recording.source_id], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    done = transcribe.execute(
        env.svc.open(env.project_id), env.svc.home, run["run_id"], owner="matrix"
    )
    assert done["state"] == "completed"
    with pytest.raises(PrometheosError) as err:
        env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    assert err.value.code == "revision_conflict"
    outcome = purge_from_another_thread(env, recording.source_id)
    assert_invariant(env, recording, outcome.get("receipt"))


def test_a_purge_that_its_transaction_refuses_cancels_nothing(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe T3: an authorised purge made stale by an unrelated commit is refused, and the
    queued transcription of the recording it named stays queued."""
    recording = make_recording(env, "inline", monkeypatch)
    run = env.svc.transcribe_start(
        env.project_id, OWNER, recording.version_id, env.synthetic_config()
    )
    plan = env.svc.purge_plan(env.project_id, [recording.source_id], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.apply([create_entry(text="an unrelated note written after the plan")])
    monkeypatch.setattr(runs, "cancel", lambda *a, **k: pytest.fail("a run was cancelled"))
    with pytest.raises(PrometheosError) as err:
        env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    assert err.value.code == "revision_conflict"
    assert env.svc.run_get(env.project_id, OWNER, run["run_id"])["state"] == "queued"
    assert transcribe.OPERATION == "audio_transcribe"
