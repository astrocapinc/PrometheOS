"""C06-AUDIO (part 1): PCM/WAV handling, audio import, resumable cancellable transcription with
timestamp locators, transcript provenance, evidence locator validation and corrections.

All of this uses the deterministic synthetic engine on real WAV bytes: no model, no device and
no network. The real engine has its own marked test (test_cp6_real_model.py)."""

from __future__ import annotations

from array import array
from pathlib import Path

import pytest
from audio_support import (
    MEETING_PLAN,
    SPEECH,
    AudioEnv,
    burst_samples,
    write_wav,
)
from cp1_support import AGENT, OWNER, create_entry

from prometheos.audio import engines, pcm, transcribe, wav
from prometheos.audio import transcript as tr
from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError


@pytest.fixture(autouse=True)
def synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")


@pytest.fixture
def env(tmp_path: Path) -> AudioEnv:
    return AudioEnv(tmp_path)


def run_all(env: AudioEnv, version_id: str, **overrides: object) -> dict:
    return env.svc.transcribe_audio(
        env.project_id, OWNER, version_id, env.synthetic_config(**overrides)
    )


# ----- PCM and WAV -------------------------------------------------------------------------


def test_resampling_keeps_the_tone_and_block_boundaries_are_seamless() -> None:
    # 1 kHz tone at 48 kHz stereo and 44.1 kHz mono both come out as a 1 kHz tone at 16 kHz.
    for rate in (48000, 44100, 8000):
        source = pcm.tone(1000.0, 1000, rate)
        whole = pcm.Resampler(rate).process(source)
        chunked = array("h")
        resampler = pcm.Resampler(rate)
        for start in range(0, len(source), 777):
            chunked.extend(resampler.process(source[start : start + 777]))
        assert abs(pcm.zero_crossing_frequency(whole) - 1000.0) < 25
        assert abs(pcm.zero_crossing_frequency(chunked) - 1000.0) < 25
        assert abs(len(chunked) - len(whole)) <= 2


def test_pcm_layouts_convert_to_mono_int16() -> None:
    stereo = array("h", [1000, 3000, -2000, -4000])
    assert list(pcm.to_mono_int16(pcm.samples_to_bytes(stereo), 2, 2)) == [2000, -3000]
    eight_bit = bytes([128, 255, 0])
    assert list(pcm.to_mono_int16(eight_bit, 1, 1)) == [0, 127 << 8, -128 << 8]
    twenty_four = (0x123456).to_bytes(3, "little", signed=True)
    assert pcm.to_mono_int16(twenty_four, 3, 1)[0] == 0x1234
    thirty_two = (0x12345678).to_bytes(4, "little", signed=True)
    assert pcm.to_mono_int16(thirty_two, 4, 1)[0] == 0x1234
    with pytest.raises(ValueError):
        pcm.to_mono_int16(b"\x00", 5, 1)


def test_wav_inspect_clip_and_normalize(tmp_path: Path) -> None:
    source = write_wav(tmp_path / "s.wav", [(1000, SPEECH), (1000, 0.0)], rate=44100, channels=2)
    info = wav.inspect_wav(source)
    assert (info.rate, info.channels, info.duration_ms) == (44100, 2, 2000)
    clip = wav.extract_clip(source, 500, 1500, tmp_path / "c.wav")
    assert clip.duration_ms == 1000 and wav.inspect_wav(tmp_path / "c.wav").frames == 44100
    raw = tmp_path / "out.raw"
    samples = wav.normalize_to_raw16k(source, raw)
    assert abs(samples - 32000) <= 2 and raw.stat().st_size == samples * 2
    assert pcm.peak_level(pcm.samples_from_bytes(raw.read_bytes()[:16000])) > 0.2
    not_wav = tmp_path / "x.wav"
    not_wav.write_bytes(b"this is not audio")
    with pytest.raises(PrometheosError) as err:
        wav.inspect_wav(not_wav)
    assert err.value.code == "unsupported"


# ----- transcripts and locators -----------------------------------------------------------


def test_merge_keeps_each_overlapping_segment_exactly_once_and_deterministically() -> None:
    def seg(a: int, b: int) -> tr.RawSegment:
        return tr.RawSegment(a, b, f"{a}-{b}")

    # chunks cover 0-6000 and 4000-10000 (overlap 2000, boundary at 5000); the segment
    # 4000-6000 (midpoint 5000) is seen by both and belongs to the second chunk.
    first = [seg(0, 2000), seg(2000, 4000), seg(4000, 6000)]
    second = [seg(4000, 6000), seg(6000, 8000), seg(8000, 10000)]
    merged = tr.merge_chunks([(0, 0, 6000, first), (1, 4000, 10000, second)])
    assert [(s.start_ms, s.end_ms) for s in merged] == [
        (0, 2000), (2000, 4000), (4000, 6000), (6000, 8000), (8000, 10000),
    ]  # fmt: skip
    assert merged == tr.merge_chunks([(1, 4000, 10000, second), (0, 0, 6000, first)])
    payload = tr.build_payload(merged)
    assert [s.id for s in payload.segments] == [f"seg-{i:05d}" for i in range(1, 6)]


def test_locator_rules_on_a_transcript() -> None:
    payload = tr.build_payload([tr.RawSegment(0, 2000, "one"), tr.RawSegment(4000, 6000, "two")])
    assert [s.text for s in tr.validate_locator(payload, 1000, 5000)] == ["one", "two"]
    assert [s.text for s in tr.validate_locator(payload, 4000, 4000)] == ["two"]  # a point
    with pytest.raises(PrometheosError):  # silence between segments
        tr.validate_locator(payload, 2000, 4000)
    with pytest.raises(PrometheosError):  # beyond the end
        tr.validate_locator(payload, 5000, 7000)
    assert tr.quote_text(tr.validate_locator(payload, 0, 6000)) == "one\ntwo"


# ----- importing audio ----------------------------------------------------------------------


def test_audio_import_defaults_to_restricted_and_agents_cannot_see_it(env: AudioEnv) -> None:
    env.audio_file()
    result = env.svc.audio_import(env.project_id, OWNER, root_alias="research", path="meeting.wav")
    assert result["sensitivity"] == "restricted"
    assert result["audio"]["duration_ms"] == 8000
    version = env.svc.get(env.project_id, OWNER, result["version_id"], None)
    assert version["sensitivity"] == "restricted"
    assert version["body"]["mime"] == "audio/wav" and version["body"]["availability"] == "retained"
    with pytest.raises(PrometheosError) as err:
        env.svc.get(env.project_id, AGENT, result["version_id"], None)
    assert err.value.code == "not_found"
    # An agent cannot start a transcription of it either, and cannot tell it exists.
    with pytest.raises(PrometheosError) as hidden:
        env.svc.transcribe_start(
            env.project_id, AGENT, result["version_id"], env.synthetic_config()
        )
    assert hidden.value.code == "not_found"


def test_audio_import_refuses_non_audio_and_unreadable_wav(env: AudioEnv) -> None:
    with pytest.raises(PrometheosError) as err:
        env.svc.audio_import(env.project_id, OWNER, root_alias="research", path="baseline.md")
    assert err.value.code == "validation_error"
    (env.research / "broken.wav").write_bytes(b"RIFFnotreallyawavefile")
    imported = env.svc.audio_import(env.project_id, OWNER, root_alias="research", path="broken.wav")
    # Imported honestly as bytes; transcription then explains what is missing.
    assert imported["audio"] is None
    with pytest.raises(PrometheosError) as run_err:
        run = env.svc.transcribe_start(
            env.project_id, OWNER, imported["version_id"], env.synthetic_config()
        )
        env.svc.transcribe_run(env.project_id, OWNER, run["run_id"])
    assert run_err.value.code in ("unsupported", "io_error")


def test_reference_only_audio_cannot_be_transcribed(env: AudioEnv) -> None:
    env.audio_file()
    imported = env.svc.audio_import(
        env.project_id,
        OWNER,
        root_alias="research",
        path="meeting.wav",
        sensitivity="normal",
        retention="reference",
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.transcribe_start(
            env.project_id, OWNER, imported["version_id"], env.synthetic_config()
        )
    assert err.value.code == "source_unavailable"


# ----- transcription ------------------------------------------------------------------------


def test_transcription_records_a_derived_version_with_provenance(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    done = run_all(env, audio["version_id"])
    assert done["state"] == "completed" and done["chunks_done"] == done["chunk_count"] == 3
    version = env.svc.get(env.project_id, OWNER, done["result_version_id"], None)
    body = version["body"]
    assert version["sensitivity"] == "restricted"  # inherits the audio's level
    assert body["payload_schema"] == "transcript_v1" and body["mime"] == "application/json"
    assert body["derived_from"]["version_id"] == audio["version_id"]
    assert body["derived_from"]["locator"] == {"type": "audio", "start_ms": 0, "end_ms": 8000}
    transformation = body["transformation"]
    assert transformation["type"] == "transcription"
    assert transformation["engine"] == "synthetic-engine"
    assert transformation["input_version_refs"] == [audio["version_id"]]
    assert transformation["run_id"] is None
    source = env.svc.get(env.project_id, OWNER, body["source_id"], None)
    assert source["body"]["origin"] == "recording" and source["sensitivity"] == "restricted"

    view = env.svc.transcript_get(env.project_id, OWNER, done["result_version_id"])
    segments = view["payload"]["segments"]
    # 2 s speech, 2 s silence (dropped), 2 s speech, 2 s quiet marked uncertain.
    assert [(s["start_ms"], s["end_ms"], s["uncertain"]) for s in segments] == [
        (0, 2000, False), (4000, 6000, False), (6000, 8000, True),
    ]  # fmt: skip
    assert segments[0]["text"] == "Compare the controllers under actuator limits."
    assert segments[2]["text"] == "[inaudible]"
    assert view["audio_version_id"] == audio["version_id"] and view["audio_available"]

    # Chunked with overlap, the result equals a single-chunk run (deterministic merge).
    env.audio_file("again.wav")
    again = env.svc.audio_import(
        env.project_id, OWNER, root_alias="research", path="again.wav", sensitivity="restricted"
    )
    single = run_all(env, again["version_id"], chunk_ms=60000, overlap_ms=2000)
    other = env.svc.transcript_get(env.project_id, OWNER, single["result_version_id"])
    assert other["payload"] == view["payload"]


def test_a_transcript_is_searchable_by_its_owner_and_hidden_from_agents(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    run_all(env, audio["version_id"])
    hits = env.svc.search(env.project_id, OWNER, "actuator")["hits"]
    assert any(h["kind"] == "version" for h in hits)
    assert env.svc.search(env.project_id, AGENT, "actuator")["hits"] == []


def test_resume_after_a_crash_between_chunks_reuses_stored_chunks(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    seen: list[int] = []

    def crash(index: int) -> None:
        seen.append(index)
        if index == 1:
            raise transcribe.Interrupted

    store = env.svc.open(env.project_id)
    with pytest.raises(transcribe.Interrupted):
        transcribe.execute(store, env.svc.home, run["run_id"], owner="a", after_chunk=crash)
    assert seen == [0, 1]
    stored = env.query("SELECT COUNT(*) FROM transcription_chunks")[0][0]
    env.execute(
        "UPDATE operation_runs SET lease_expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE lease_owner IS NOT NULL"
    )
    assert stored == 2
    seen.clear()
    done = transcribe.execute(
        store, env.svc.home, run["run_id"], owner="b", after_chunk=seen.append
    )
    assert done["state"] == "completed" and seen == [2]  # only the missing chunk was run
    # The resumed result equals an uninterrupted run over the same audio.
    env.audio_file("twin.wav")
    twin = env.svc.audio_import(
        env.project_id, OWNER, root_alias="research", path="twin.wav", sensitivity="restricted"
    )
    reference = run_all(env, twin["version_id"])
    resumed = env.svc.transcript_get(env.project_id, OWNER, done["result_version_id"])
    plain = env.svc.transcript_get(env.project_id, OWNER, reference["result_version_id"])
    assert resumed["payload"] == plain["payload"]
    assert env.query("SELECT COUNT(*) FROM transcription_chunks")[0][0] == 0


def test_a_live_lease_blocks_a_second_runner(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    store = env.svc.open(env.project_id)

    def other(index: int) -> None:
        with pytest.raises(PrometheosError) as err:
            transcribe.execute(store, env.svc.home, run["run_id"], owner="rival")
        assert err.value.code == "busy"
        raise transcribe.Interrupted

    with pytest.raises(transcribe.Interrupted):
        transcribe.execute(store, env.svc.home, run["run_id"], owner="first", after_chunk=other)


def test_cancel_stops_between_chunks_and_leaves_no_transcript_or_chunks(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )

    def cancel_after_first(index: int) -> None:
        if index == 0:
            reply = env.svc.transcription_cancel(env.project_id, OWNER, run["run_id"])
            assert reply["outcome"] == "cancel_requested"

    store = env.svc.open(env.project_id)
    result = transcribe.execute(
        store, env.svc.home, run["run_id"], owner="p", after_chunk=cancel_after_first
    )
    assert result["state"] == "cancelled" and result["result_version_id"] is None
    assert env.query("SELECT COUNT(*) FROM transcription_chunks")[0][0] == 0
    kinds = {r[0] for r in env.query("SELECT kind FROM records")}
    assert "source" in kinds  # the audio itself
    transcripts = [
        1
        for (encoded,) in env.query("SELECT encoded FROM bodies WHERE encoded IS NOT NULL")
        if "transcript_v1" in encoded
    ]
    assert transcripts == []
    with pytest.raises(PrometheosError):  # a cancelled run cannot be resumed
        env.svc.transcribe_run(env.project_id, OWNER, run["run_id"])


def test_an_agent_cannot_resume_or_see_an_owners_restricted_run(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert (
        env.query("SELECT caller_rank FROM operation_runs WHERE operation = 'audio_transcribe'")[0][
            0
        ]
        == 2
    )
    for call in (
        lambda: env.svc.transcribe_run(env.project_id, AGENT, run["run_id"]),
        lambda: env.svc.transcription_status(env.project_id, AGENT, run["run_id"]),
        lambda: env.svc.transcription_cancel(env.project_id, AGENT, run["run_id"]),
    ):
        with pytest.raises(PrometheosError) as err:
            call()
        assert err.value.code == "not_found"
    assert env.svc.transcription_list(env.project_id, AGENT)["runs"] == []
    done = env.svc.transcribe_run(env.project_id, OWNER, run["run_id"])
    assert done["state"] == "completed"


def test_a_transcript_commit_replays_only_at_the_rank_that_created_it(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    assert (
        env.query("SELECT caller_rank FROM operation_runs WHERE operation = 'audio_transcribe'")[0][
            0
        ]
        == 0
    )
    done = env.svc.transcribe_run(env.project_id, AGENT, run["run_id"])
    assert done["state"] == "completed"
    view = env.svc.transcript_get(env.project_id, AGENT, done["result_version_id"])
    store = env.svc.open(env.project_id)

    def recommit(rank: int) -> dict:
        return transcribe.commit_transcript(
            store,
            run_id=run["run_id"],
            audio_version_id=audio["version_id"],
            payload=tr.TranscriptV1.model_validate(view["payload"]),
            total_ms=done["total_ms"],
            engine="synthetic-engine",
            engine_version="1",
            model=None,
            language=None,
            parameters_digest=done["config_digest"],
            actor="test",
            max_rank=rank,
        )

    assert recommit(0)["commit"]["replayed"] is True  # the creating rank may replay
    with pytest.raises(PrometheosError) as err:  # another rank gets a conflict, not the IDs
        recommit(2)
    assert err.value.code == "request_key_conflict"


def test_cancel_before_start_cancels_at_once(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert (
        env.svc.transcription_cancel(env.project_id, OWNER, run["run_id"])["outcome"] == "cancelled"
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.transcription_cancel(env.project_id, OWNER, run["run_id"])
    assert err.value.code == "validation_error"


def test_resume_requires_the_same_configuration(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    first = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    again = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert again["run_id"] == first["run_id"] and again["resumed"] is True
    with pytest.raises(PrometheosError) as err:
        env.svc.transcribe_start(
            env.project_id, OWNER, audio["version_id"], env.synthetic_config(language="de")
        )
    assert err.value.code == "validation_error" and "different settings" in err.value.message


def test_engine_version_change_requires_restart(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    other = engines.SyntheticEngine(version="2")
    with pytest.raises(PrometheosError) as err:
        env.svc.transcribe_run(env.project_id, OWNER, run["run_id"], engine=other)
    assert err.value.code == "stale_task"
    status = env.svc.transcription_status(env.project_id, OWNER, run["run_id"])
    assert status["state"] == "failed" and "restart" in status["error"]


def test_engine_failure_is_visible_and_resumable(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")

    class Flaky(engines.SyntheticEngine):
        calls = 0

        def transcribe(self, data: bytes, start_ms: int, language: str | None):  # type: ignore[no-untyped-def]
            Flaky.calls += 1
            if Flaky.calls == 2:
                raise RuntimeError("decoder blew up")
            return super().transcribe(data, start_ms, language)

    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.transcribe_run(
            env.project_id,
            OWNER,
            run["run_id"],
            engine=Flaky(script=list(env.synthetic_config().script)),
        )
    assert err.value.code == "io_error"
    status = env.svc.transcription_status(env.project_id, OWNER, run["run_id"])
    assert status["state"] == "failed" and "RuntimeError" in status["error"]
    assert status["chunks_done"] == 1
    again = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    assert again["run_id"] == run["run_id"]
    done = env.svc.transcribe_run(env.project_id, OWNER, run["run_id"])
    assert done["state"] == "completed"


def test_real_engine_without_model_explains_the_explicit_install(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_models = env.tmp / "empty-synthetic-models"
    empty_models.mkdir()
    monkeypatch.setenv("PROMETHEOS_MODELS_DIR", str(empty_models))
    audio = env.import_audio(sensitivity="restricted")
    with pytest.raises(PrometheosError) as err:
        env.svc.transcribe_start(
            env.project_id, OWNER, audio["version_id"], transcribe.RunConfig("faster-whisper")
        )
    assert err.value.code == "unsupported" and "install" in err.value.message
    assert not list(empty_models.iterdir())
    monkeypatch.delenv(engines.SYNTHETIC_ENV)
    with pytest.raises(PrometheosError) as synth:
        env.svc.transcribe_start(env.project_id, OWNER, audio["version_id"], env.synthetic_config())
    assert synth.value.code == "unsupported"


def test_run_config_is_validated(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    for bad in (
        {"chunk_ms": 3000},
        {"chunk_ms": 61500},
        {"overlap_ms": 3000, "chunk_ms": 4000},
        {"language": "english"},
        {"engine": "nonsense"},
    ):
        with pytest.raises(PrometheosError) as err:
            env.svc.transcribe_start(
                env.project_id, OWNER, audio["version_id"], env.synthetic_config(**bad)
            )
        assert err.value.code == "validation_error", bad


# ----- locators as evidence -----------------------------------------------------------------


def cite(env: AudioEnv, transcript_version: str, source: str, start: int, end: int, quote=None):
    return {
        "source_id": source,
        "version_id": transcript_version,
        "locator": {"type": "audio", "start_ms": start, "end_ms": end},
        "quote_sha256": quote,
    }


def note_with(evidence: dict) -> dict:
    op = create_entry(evidence=[evidence])
    return op


def test_audio_locators_are_validated_against_the_transcript(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    done = run_all(env, audio["version_id"])
    view = env.svc.transcript_get(env.project_id, OWNER, done["result_version_id"])
    version, source = done["result_version_id"], view["source_id"]
    good = env.apply([note_with(cite(env, version, source, 0, 2000))])
    assert good["ok"]
    segments = [tr.Segment.model_validate(s) for s in view["payload"]["segments"]]
    digest = tr.quote_sha256(segments[:1])
    assert env.apply([note_with(cite(env, version, source, 500, 1500, digest))])["ok"]
    with pytest.raises(PrometheosError, match="quote digest"):
        env.apply([note_with(cite(env, version, source, 500, 1500, "0" * 64))])
    with pytest.raises(PrometheosError, match="beyond"):
        env.apply([note_with(cite(env, version, source, 7000, 9000))])
    with pytest.raises(PrometheosError, match="no transcript segment"):
        env.apply([note_with(cite(env, version, source, 2100, 3900))])  # the silent gap


def test_audio_locators_on_the_recording_itself(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    src, ver = audio["source_id"], audio["version_id"]
    assert env.apply([note_with(cite(env, ver, src, 1000, 7999))])["ok"]
    with pytest.raises(PrometheosError, match="beyond the recording"):
        env.apply([note_with(cite(env, ver, src, 1000, 8001))])
    with pytest.raises(PrometheosError, match="no text to quote"):
        env.apply([note_with(cite(env, ver, src, 0, 1000, "a" * 64))])
    # Not audio at all: cite text with a text locator instead.
    env.import_files(["baseline.md"])
    base = env.svc.list_records(env.project_id, OWNER, kind="version", limit=50)["items"]
    text_version = next(r for r in base if r["body"]["mime"].startswith("text/"))
    with pytest.raises(PrometheosError, match="transcript or a recording"):
        env.apply(
            [note_with(cite(env, text_version["id"], text_version["body"]["source_id"], 0, 10))]
        )


def test_audio_locator_on_a_compressed_recording_is_refused_not_guessed(env: AudioEnv) -> None:
    (env.research / "talk.mp3").write_bytes(b"ID3" + bytes(300))
    imported = env.svc.audio_import(
        env.project_id, OWNER, root_alias="research", path="talk.mp3", sensitivity="normal"
    )
    with pytest.raises(PrometheosError, match="cannot be checked without decoding"):
        env.apply([note_with(cite(env, imported["version_id"], imported["source_id"], 0, 1000))])


# ----- corrections --------------------------------------------------------------------------


def test_correction_is_a_new_version_and_old_citations_keep_their_text(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    done = run_all(env, audio["version_id"])
    first = done["result_version_id"]
    edits = [transcribe.Edit("seg-00002", text="Send the constrained-plant result.")]
    corrected = env.svc.transcript_correct(env.project_id, OWNER, first, edits, new_request_id())
    second = corrected["version_id"]
    assert corrected["segments_changed"] == 1 and corrected["previous_version_id"] == first
    old = env.svc.transcript_get(env.project_id, OWNER, first)
    new = env.svc.transcript_get(env.project_id, OWNER, second)
    assert old["payload"]["segments"][1]["text"] == "Send the constrained result."
    assert new["payload"]["segments"][1]["text"] == "Send the constrained-plant result."
    assert old["superseded_by_correction"] is True and new["superseded_by_correction"] is False
    assert old["latest_version_id"] == second
    body = env.svc.get(env.project_id, OWNER, second, None)["body"]
    assert body["transformation"]["type"] == "conversion"
    assert body["transformation"]["engine"] == "manual-correction"  # owner-made
    assert body["transformation"]["input_version_refs"] == [first]
    assert body["source_id"] == old["source_id"]  # the same transcript, a later version
    assert new["audio_version_id"] == audio["version_id"]
    # Only the latest version can be corrected; no-ops and unknown segments are refused.
    with pytest.raises(PrometheosError) as stale:
        env.svc.transcript_correct(env.project_id, OWNER, first, edits, new_request_id())
    assert stale.value.code == "revision_conflict"
    with pytest.raises(PrometheosError, match="change nothing"):
        env.svc.transcript_correct(
            env.project_id, OWNER, second,
            [transcribe.Edit("seg-00002", text="Send the constrained-plant result.")],
            new_request_id(),
        )  # fmt: skip
    with pytest.raises(PrometheosError, match="unknown segment"):
        env.svc.transcript_correct(
            env.project_id,
            OWNER,
            second,
            [transcribe.Edit("seg-99999", text="x")],
            new_request_id(),
        )


def test_correction_retry_with_the_same_request_id_replays(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    first = run_all(env, audio["version_id"])["result_version_id"]
    rid = new_request_id()
    edits = [transcribe.Edit("seg-00001", text="Compare controllers under actuator limits.")]
    one = env.svc.transcript_correct(env.project_id, OWNER, first, edits, rid)
    two = env.svc.transcript_correct(env.project_id, OWNER, first, edits, rid)
    assert two["commit"]["replayed"] is True and two["version_id"] == one["version_id"]


def test_span_report_and_clip_let_a_person_verify_a_sample_span(
    env: AudioEnv, tmp_path: Path
) -> None:
    audio = env.import_audio(sensitivity="restricted")
    done = run_all(env, audio["version_id"])
    clip = tmp_path / "clip.wav"
    report = env.svc.audio_span(
        env.project_id, OWNER, done["result_version_id"], 4000, 6000, out=clip
    )
    assert report["quote"] == "Send the constrained result."
    assert report["clock"] == "00:00:04.000 - 00:00:06.000"
    assert report["clip"]["duration_ms"] == 2000 and wav.inspect_wav(clip).duration_ms == 2000
    samples = wav.read_all_samples(clip)
    assert pcm.peak_level(samples) > 0.25  # the clip is the spoken burst, not the silence
    with pytest.raises(PrometheosError):
        env.svc.audio_span(env.project_id, AGENT, done["result_version_id"], 4000, 6000)


def test_burst_helper_is_what_the_tests_think_it_is() -> None:
    samples = burst_samples(MEETING_PLAN)
    assert len(samples) == 8 * pcm.TARGET_RATE
    assert pcm.peak_level(samples[: pcm.TARGET_RATE]) > 0.25
    assert pcm.peak_level(samples[2 * pcm.TARGET_RATE : 3 * pcm.TARGET_RATE]) == 0.0


def test_transcribe_start_does_not_change_the_research_sequence(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="restricted")
    before = env.seq()
    run = env.svc.transcribe_start(
        env.project_id, OWNER, audio["version_id"], env.synthetic_config()
    )
    env.svc.transcription_cancel(env.project_id, OWNER, run["run_id"])
    assert env.seq() == before  # operational state is not research record


def test_transcript_payload_version_must_be_the_integer_one() -> None:
    import json

    for bad in (True, 1.0, "1", 2):
        raw = json.dumps({"schema_version": bad, "segments": []}).encode()
        with pytest.raises(PrometheosError):
            tr.parse_payload(raw)
    assert tr.parse_payload(b'{"schema_version":1,"segments":[]}').segments == []


def test_an_agent_cannot_import_over_a_path_bound_to_the_owners_restricted_audio(
    env: AudioEnv,
) -> None:
    env.import_audio("private.wav", sensitivity="restricted")
    write_wav(env.research / "private.wav", [(1000, 0.3)])  # the file changed on disk
    with pytest.raises(PrometheosError) as err:
        env.svc.audio_import(env.project_id, AGENT, root_alias="research", path="private.wav")
    assert err.value.code == "policy_denied"
    # The owner may add a new version, and it keeps the source's sensitivity.
    again = env.svc.audio_import(env.project_id, OWNER, root_alias="research", path="private.wav")
    assert again["sensitivity"] == "restricted"
