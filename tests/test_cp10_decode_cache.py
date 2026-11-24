"""The decoded-audio caches under concurrency (CP10 fixtures round 2, finding R2-F2).

``cache/audio-<sha>.bin`` (a body copied to a file the decoder can open) and
``cache/pcm-<sha>.raw`` (the decoded 16 kHz audio the chunk loop reads) are keyed by the
recording's digest, so two runs over byte-identical audio (two versions of one recording, two
configurations, two processes) share the same two file names. On Windows a file another handle
holds open cannot be replaced or removed, so a naive "write a temporary file, replace the entry"
and "remove the entry when the run finishes" made one of two concurrent transcriptions fail with
``PermissionError: [WinError 5]`` (a retryable ``io_error``) or ``FileNotFoundError``.

The contract these tests pin: an entry is only ever published whole (atomic replace of a
private completed file); an audio entry must hold exactly the verified bytes and a PCM entry
must have the length the source (or the run's own earlier decode) says, otherwise it is
replaced (content of the right length is not re-verified); a run that loses the publishing race
uses the equal entry the other run published, or its own completed output privately; whatever
is private is removed when its use ends, by every caller; and a run holds the entries it reads
open, so another run finishing (and removing them) cannot take them away mid-run. Synthetic
recordings and the synthetic engine only."""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from audio_support import SPEECH, AudioEnv
from cp1_support import AGENT, OWNER

# ``_reap_owned_workers`` is that module's autouse fixture: it owns, and always cleans up, every
# worker process these tests start (imported so that pytest applies it here too).
from test_cp10_runs_kill import LONG_PLAN, _reap_owned_workers, finish, start_worker  # noqa: F401

from prometheos.audio import engines, transcribe
from prometheos.domain.models import PrometheosError

ROUNDS = 6  # per run of the process test; the evidence runs repeat it many more times


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AudioEnv:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    return AudioEnv(tmp_path)


def transcript_end_ms(env: AudioEnv, result_version_id: str) -> int:
    view = env.svc.transcript_get(env.project_id, OWNER, result_version_id)
    return max(segment["end_ms"] for segment in view["payload"]["segments"])


# ----- two real worker processes on byte-identical audio ----------------------------------------


def identical_recordings(env: AudioEnv, index: int) -> list[str]:
    """Two runs over two versions with byte-identical audio (so one digest, one cache name).
    ``index`` varies the bytes between rounds, never within one."""
    plan = [(2000, SPEECH)] * 7 + [(1000 + 50 * index, SPEECH)]
    run_ids = []
    for name in ("a", "b"):
        audio = env.import_audio(f"{name}{index}.wav", sensitivity="normal", plan=plan)
        started = env.svc.transcribe_start(
            env.project_id, AGENT, audio["version_id"], env.synthetic_config()
        )
        run_ids.append(started["run_id"])
    assert len(set(run_ids)) == 2
    return run_ids


def run_overlapping_workers(env: AudioEnv, rounds: int) -> None:
    cache = env.store_dir / "cache"
    for index in range(rounds):
        workers = [
            start_worker(
                env.home, env.project_id, run_id, PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="40"
            )
            for run_id in identical_recordings(env, index)
        ]
        for number, worker in enumerate(workers):
            code, view = finish(worker)
            assert code == 0 and view.get("state") == "completed", worker.describe(
                f"round {index}: worker {number} of two on identical audio did not complete"
            )
        assert not list(cache.glob(".*.tmp")), "a private temporary cache file was left behind"


def test_two_worker_processes_on_identical_audio_both_complete(env: AudioEnv) -> None:
    run_overlapping_workers(env, ROUNDS)


# ----- deterministic controls: the race, forced -------------------------------------------------


class Competitor:
    """Stands in for a concurrent run. The moment this process is about to publish a cache entry
    named ``prefix*``, the competitor has published its own entry under that name first and is
    READING it (the file is held open), which Windows answers to a replace with an error."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, prefix: str, content: Callable[[Path], bytes]
    ) -> None:
        self.held: list[BinaryIO] = []
        self.published: list[Path] = []
        real_replace = os.replace

        def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
            target = Path(destination)
            if target.name.startswith(prefix) and not self.held:
                target.write_bytes(content(Path(source)))
                self.published.append(target)
                self.held.append(open(target, "rb"))
            real_replace(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)

    def close(self) -> None:
        for handle in self.held:
            handle.close()
        self.held.clear()


@pytest.fixture
def competitor(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Competitor]]:
    made: list[Competitor] = []

    def install(prefix: str, content: Callable[[Path], bytes]) -> Competitor:
        made.append(Competitor(monkeypatch, prefix, content))
        return made[-1]

    yield install
    for one in made:
        one.close()


@contextlib.contextmanager
def materialized_in(env: AudioEnv, version_id: str) -> Iterator[Path]:
    body = env.svc.get(env.project_id, OWNER, version_id, None)["body"]
    conn = env.svc.open(env.project_id).connect(readonly=True)
    with closing(conn), transcribe.materialized(conn, env.store_dir, body) as path:
        yield path


@pytest.mark.parametrize("equal", [True, False], ids=["equal entry", "different entry"])
def test_an_audio_entry_published_and_held_by_another_run_does_not_fail_this_one(
    env: AudioEnv, competitor: Callable[..., Competitor], equal: bool
) -> None:
    audio = env.import_audio(sensitivity="normal")
    expected = (env.research / "meeting.wav").read_bytes()
    seam = competitor(
        "audio-", (lambda source: source.read_bytes()) if equal else (lambda _s: b"x")
    )
    cache = env.store_dir / "cache"
    try:
        with materialized_in(env, audio["version_id"]) as path:
            assert seam.published, "the competing run never published: the control did not fire"
            assert path.read_bytes() == expected  # verified bytes, whichever file they come from
            if equal:
                assert path == seam.published[0]
            elif sys.platform == "win32":  # the replace is refused: a private copy, theirs kept
                assert path.name.startswith(".audio-") and seam.published[0].read_bytes() == b"x"
        assert not list(cache.glob(".audio-*.tmp"))  # nothing private outlives the block
    finally:
        seam.close()


@pytest.mark.parametrize("equal", [True, False], ids=["equal entry", "different entry"])
def test_a_pcm_entry_published_and_held_by_another_run_does_not_fail_this_one(
    env: AudioEnv, competitor: Callable[..., Competitor], equal: bool
) -> None:
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    seam = competitor(
        "pcm-", (lambda source: source.read_bytes()) if equal else (lambda _s: bytes(2000))
    )
    done = env.svc.transcribe_audio(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert seam.published, "the competing run never published: the control did not fire"
    assert done["state"] == "completed"
    assert transcript_end_ms(env, done["result_version_id"]) >= 15000  # the whole recording
    assert not list((env.store_dir / "cache").glob(".pcm-*.tmp"))  # no private file left
    if not equal and sys.platform == "win32":  # their entry, held open, is neither used nor lost
        assert seam.published[0].read_bytes() == bytes(2000)


def test_a_run_keeps_its_decoded_audio_when_another_run_finishes_and_drops_the_caches(
    env: AudioEnv,
) -> None:
    """The other half of the race: a run that finishes removes the shared entry (the same
    ``_drop_caches`` as a completing run does) while this one is between two chunks."""
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    store = env.svc.open(env.project_id)
    sha = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]["sha256"]
    chunks: list[int] = []

    def other_run_finishes(index: int) -> None:
        chunks.append(index)
        if index == 0:
            transcribe._drop_caches(store, sha)

    done = transcribe.execute(
        store, env.svc.home, run["run_id"], owner="this-run", after_chunk=other_run_finishes
    )
    assert done["state"] == "completed" and chunks == list(range(7))


# ----- a reader never sees a partial or a damaged entry -----------------------------------------


def test_an_entry_appears_only_whole_and_only_by_replacing_a_complete_private_file(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    seen: list[tuple[str, bool, int]] = []
    real_replace = os.replace

    def watching(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        target = Path(destination)
        if target.name.startswith(("pcm-", "audio-")):  # at publication time:
            seen.append((target.name[:3], target.exists(), Path(source).stat().st_size))
        real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", watching)
    done = env.svc.transcribe_audio(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert done["state"] == "completed"
    published = {kind: (exists, size) for kind, exists, size in seen}
    # Nothing was visible under the final name before the replace, and the file moved in was
    # complete: all 16 s of the decoded 16 kHz 16-bit audio.
    assert published["pcm"] == (False, 16 * 16000 * 2)
    assert published["aud"][0] is False and published["aud"][1] > 16 * 16000 * 2


def test_a_damaged_audio_cache_entry_is_not_trusted(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    expected = (env.research / "meeting.wav").read_bytes()
    body = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    (cache / f"audio-{body['sha256']}.bin").write_bytes(expected[: len(expected) // 2])
    with materialized_in(env, audio["version_id"]) as path:
        assert path.read_bytes() == expected


@pytest.mark.parametrize("length", [2001, 2000], ids=["not whole samples", "whole samples"])
def test_a_pcm_entry_of_the_wrong_length_is_replaced_not_reused(env: AudioEnv, length: int) -> None:
    """A 16 kHz WAV: the header says the decoded recording is exactly 256000 samples, so a junk
    entry of 2001 bytes (not whole samples) or of 2000 bytes (whole, but 1000 samples) is
    replaced and the run transcribes all 16 s. The length is what is checked; an entry of the
    right length is not re-verified."""
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    body = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    (cache / f"pcm-{body['sha256']}.raw").write_bytes(bytes(length))
    done = env.svc.transcribe_audio(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert done["state"] == "completed"
    assert transcript_end_ms(env, done["result_version_id"]) >= 15000  # not the junk's 62 ms


def test_a_resumed_run_checks_the_cached_audio_against_its_own_recorded_decode(
    env: AudioEnv,
) -> None:
    """A 44.1 kHz WAV is interpolated, so its header does not fix the decoded length; what the
    run recorded when it first decoded does. A resume that finds a whole-sample entry of another
    length replaces it instead of transcribing 62 ms."""
    audio = env.import_audio("r44.wav", sensitivity="normal", plan=LONG_PLAN, rate=44100)
    sha = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]["sha256"]
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )

    def crash(index: int) -> None:
        raise transcribe.Interrupted

    with pytest.raises(transcribe.Interrupted):  # the first attempt decodes, then dies
        transcribe.execute(
            env.svc.open(env.project_id), env.svc.home, run["run_id"], owner="a", after_chunk=crash
        )
    entry = env.store_dir / "cache" / f"pcm-{sha}.raw"
    assert entry.stat().st_size > 400000  # kept for the resume
    entry.write_bytes(bytes(2000))
    env.execute(
        "UPDATE operation_runs SET lease_expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE lease_owner IS NOT NULL"
    )
    done = env.svc.transcribe_run(env.project_id, OWNER, run["run_id"])
    assert done["state"] == "completed"
    assert transcript_end_ms(env, done["result_version_id"]) >= 15000


# ----- a private copy never outlives its use, whoever asked for it ------------------------------


def test_a_run_whose_audio_entry_is_held_by_a_peer_removes_its_private_copy(
    env: AudioEnv, competitor: Callable[..., Competitor]
) -> None:
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    seam = competitor("audio-", lambda _source: b"x")  # a different entry, held open by a peer
    done = env.svc.transcribe_audio(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert seam.published and done["state"] == "completed"
    assert transcript_end_ms(env, done["result_version_id"]) >= 15000
    assert not list((env.store_dir / "cache").glob(".audio-*.tmp"))


def test_audio_span_leaves_no_copy_of_the_recording_behind(
    env: AudioEnv, competitor: Callable[..., Competitor], tmp_path: Path
) -> None:
    """The clip extraction used to keep the private copy that ``materialize`` could return: a
    full copy of a possibly restricted recording that no sweep knew and nothing removed."""
    audio = env.import_audio(sensitivity="restricted")
    recording = (env.research / "meeting.wav").read_bytes()
    done = env.svc.transcribe_audio(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    cache = env.store_dir / "cache"
    clip = tmp_path / "clip.wav"
    seam = competitor("audio-", lambda _source: b"x")  # forces the private copy
    try:
        report = env.svc.audio_span(
            env.project_id, OWNER, done["result_version_id"], 0, 1000,
            out=clip, audio_version_id=audio["version_id"],
        )  # fmt: skip
        assert seam.published and report["clip"]["duration_ms"] > 0 and clip.is_file()
        assert not list(cache.glob(".audio-*.tmp"))
        with pytest.raises(PrometheosError) as err:  # a failure inside the block releases too
            env.svc.audio_span(
                env.project_id, OWNER, done["result_version_id"], 0, 1000,
                out=clip, audio_version_id=audio["version_id"],
            )  # fmt: skip
        assert err.value.code == "validation_error"
        assert not list(cache.glob(".audio-*.tmp"))
    finally:
        seam.close()
    plan = env.svc.purge_plan(env.project_id, [audio["source_id"]], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    held = [
        path
        for path in env.store_dir.rglob("*")
        if path.is_file()
        and not path.name.startswith("record.sqlite")
        and recording[:2048] in path.read_bytes()
    ]
    assert held == []  # no copy of the recording remains anywhere outside the database


# ----- the window between obtaining the audio and decoding it -----------------------------------

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="relies on Windows refusing to remove an open file"
)


@WINDOWS_ONLY
def test_the_audio_entry_a_run_decodes_from_survives_a_peer_finishing_first(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer that finishes drops the caches (the same ``_drop_caches``) right after this run
    obtained its audio and before it decodes: the run holds the entry, so the drop fails and is
    listed for the sweep, and the decode still finds its source."""
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    store = env.svc.open(env.project_id)
    sha = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]["sha256"]
    fired: list[str] = []
    real_open_entry = transcribe._open_entry

    def open_entry(entry: Path, *args: Any) -> Any:
        if not fired and entry.name.startswith("pcm-"):
            fired.append(entry.name)
            transcribe._drop_caches(store, sha)
        return real_open_entry(entry, *args)

    monkeypatch.setattr(transcribe, "_open_entry", open_entry)
    done = env.svc.transcribe_audio(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert fired and done["state"] == "completed"
    assert transcript_end_ms(env, done["result_version_id"]) >= 15000


# ----- abandoned private files ------------------------------------------------------------------


def test_abandoned_private_temporaries_are_swept_and_nothing_else(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")  # its bytes are retained: its entry may stay
    retained = env.svc.get(env.project_id, OWNER, audio["version_id"], None)["body"]["sha256"]
    store = env.svc.open(env.project_id)
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    long_ago = time.time() - 3600
    abandoned = [cache / ".pcm-killed-mid-decode.tmp", cache / ".audio-killed-in-use.tmp"]
    young = cache / ".pcm-being-decoded-now.tmp"  # younger than a run lease
    others = [
        cache / f"pcm-{retained}.raw",  # a shared entry is not a private temporary
        cache / "media-verify.json",
        cache / ".something-else.tmp",
    ]
    for path in [*abandoned, young, *others]:
        path.write_bytes(b"x" * 100)
    for path in [*abandoned, *others]:
        os.utime(path, (long_ago, long_ago))
    transcribe.sweep_stale_caches(store)
    assert not [p for p in abandoned if p.exists()]
    assert young.exists() and all(p.exists() for p in others)


@WINDOWS_ONLY
def test_a_private_temporary_a_live_run_holds_open_is_not_swept(env: AudioEnv) -> None:
    store = env.svc.open(env.project_id)
    cache = env.store_dir / "cache"
    cache.mkdir(exist_ok=True)
    private = cache / ".pcm-in-use-by-a-long-run.tmp"
    private.write_bytes(b"x" * 100)
    long_ago = time.time() - 3600
    os.utime(private, (long_ago, long_ago))
    with open(private, "rb"):
        transcribe.sweep_stale_caches(store)
        assert private.exists()
    transcribe.sweep_stale_caches(store)
    assert not private.exists()  # once nobody holds it, it is an abandoned temporary
