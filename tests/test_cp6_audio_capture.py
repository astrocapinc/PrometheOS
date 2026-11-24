"""C06-AUDIO (part 2): live recording sessions against scripted capture devices (S14).

No microphone or loopback is touched: sources are in-process devices that deliver a tone, end,
lose the device or never stop. What is proven here is the recorder's behaviour (mixing,
segments, cancel, device loss, recovery, crash); the real WASAPI path is E04 evidence."""

from __future__ import annotations

import sys
import threading
import time
import types
from contextlib import closing
from pathlib import Path
from typing import Any, ClassVar

import pytest
from audio_support import LOOP, MIC, AudioEnv, ScriptedBackend, ScriptedSource
from cp1_support import AGENT, OWNER, OWNER_ACT

from prometheos.audio import capture, engines, pcm, recorder, transcribe, wav
from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> AudioEnv:
    return AudioEnv(tmp_path)


def request(title: str = "Standup", devices: tuple[str, ...] = ("mic-1",), **kw: Any):
    values: dict[str, Any] = {"consent_acknowledged": True, "sensitivity": "normal"}
    values.update(kw)
    return recorder.SessionRequest(title, devices, **values)


def run(env: AudioEnv, backend: ScriptedBackend, req: recorder.SessionRequest, **kw: Any) -> dict:
    session = env.svc.recording_start(env.project_id, OWNER, req, backend, authority=OWNER_ACT)
    kw.setdefault("stall_seconds", 30)
    final = env.svc.recording_run(env.project_id, session["session_id"], backend, **kw)
    return final | {"created": session}


def audio_samples(env: AudioEnv, version_id: str):
    """The version body, its WAV header facts and its samples, read while the materialized file
    is valid (the path itself must not outlive the block)."""
    version = env.svc.get(env.project_id, OWNER, version_id, None)["body"]
    with closing(env.svc.open(env.project_id).connect(readonly=True)) as conn:
        with transcribe.materialized(conn, env.store_dir, version) as path:
            return version, wav.inspect_wav(path), wav.read_all_samples(path)


# ----- consent, validation ----------------------------------------------------------------


def test_recording_needs_the_consent_acknowledgement_and_shows_the_reminder(env: AudioEnv) -> None:
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 10)})
    with pytest.raises(PrometheosError) as err:
        env.svc.recording_start(
            env.project_id,
            OWNER,
            recorder.SessionRequest("x", ("mic-1",), consent_acknowledged=False),
            backend,
            authority=OWNER_ACT,
        )
    assert err.value.code == "policy_denied" and "reminder" in err.value.details
    assert env.svc.recording_list(env.project_id, OWNER)["sessions"] == []


def test_devices_are_validated(env: AudioEnv) -> None:
    backend = ScriptedBackend(
        {"mic-1": ScriptedSource(MIC, 10), "loop-1": ScriptedSource(LOOP, 10)}
    )
    other_mic = capture.DeviceInfo("mic-2", "Second microphone", "microphone", 16000, 1)
    backend.sources["mic-2"] = ScriptedSource(other_mic, 10)
    for devices, code in (
        (("nope",), "not_found"),
        ((), "validation_error"),
        (("mic-1", "mic-1"), "validation_error"),
        (("mic-1", "mic-2"), "validation_error"),  # two microphones
        (("mic-1", "loop-1", "mic-2"), "validation_error"),
    ):
        with pytest.raises(PrometheosError) as err:
            env.svc.recording_start(
                env.project_id, OWNER, request(devices=devices), backend, authority=OWNER_ACT
            )
        assert err.value.code == code, devices
    with pytest.raises(PrometheosError):
        env.svc.recording_start(
            env.project_id, OWNER, request(segment_seconds=1), backend, authority=OWNER_ACT
        )


# ----- a normal session --------------------------------------------------------------------


def test_microphone_and_system_audio_are_converted_mixed_and_registered(env: AudioEnv) -> None:
    backend = ScriptedBackend(
        {"mic-1": ScriptedSource(MIC, 40), "loop-1": ScriptedSource(LOOP, 40, freq=1000.0)}
    )
    final = run(env, backend, request("Online meeting", ("mic-1", "loop-1")))
    assert final["state"] == "imported" and final["partial"] is False
    assert 1900 <= final["duration_ms"] <= 2100  # 40 blocks of 50 ms, two devices
    version, info, samples = audio_samples(env, final["version_id"])
    assert (info.rate, info.channels, info.width) == (16000, 1, 2)
    assert abs(pcm.zero_crossing_frequency(samples) - 1000.0) < 40
    assert pcm.peak_level(samples) > 0.2
    assert version["mime"] == "audio/wav" and version["availability"] == "retained"
    source = env.svc.get(env.project_id, OWNER, version["source_id"], None)
    assert source["body"]["origin"] == "recording" and source["body"]["title"] == "Online meeting"
    assert "Test microphone" in source["body"]["description"]
    assert "Test system audio" in source["body"]["description"]
    assert "Partial" not in source["body"]["description"]
    assert not (env.store_dir / "staging" / f"rec-{final['session_id']}").exists()
    # What was captured is private to the owner unless chosen otherwise.
    assert source["sensitivity"] == "normal"


def test_recordings_default_to_restricted_and_agents_cannot_see_them(env: AudioEnv) -> None:
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 20)})
    session = env.svc.recording_start(
        env.project_id,
        OWNER,
        recorder.SessionRequest("Private", ("mic-1",), consent_acknowledged=True),
        backend,
        authority=OWNER_ACT,
    )
    assert session["sensitivity"] == "restricted"
    final = env.svc.recording_run(env.project_id, session["session_id"], backend, stall_seconds=30)
    with pytest.raises(PrometheosError):
        env.svc.get(env.project_id, AGENT, final["version_id"], None)


def test_a_recording_can_be_transcribed_and_cited_by_time(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 100)})  # 5 s of tone
    final = run(env, backend, request("Talk"))
    done = env.svc.transcribe_audio(
        env.project_id, OWNER, final["version_id"], env.synthetic_config(chunk_ms=4000)
    )
    view = env.svc.transcript_get(env.project_id, OWNER, done["result_version_id"])
    assert view["audio_version_id"] == final["version_id"]
    assert view["payload"]["segments"], "the recorded tone is speech to the synthetic engine"
    assert view["duration_ms"] <= final["duration_ms"] + 50


# ----- failure, cancel, recovery -----------------------------------------------------------


def test_device_loss_keeps_recoverable_segments_and_says_so(env: AudioEnv) -> None:
    source = ScriptedSource(MIC, 160, ending="lost")  # 8 s, then the device vanishes
    backend = ScriptedBackend({"mic-1": source})
    final = run(env, backend, request("Meeting", segment_seconds=5))
    assert final["state"] == "interrupted" and final["recoverable"] is True
    assert "device lost" in final["reason"] and "unplugged" in final["reason"]
    assert final["version_id"] is None  # nothing is registered behind the user's back
    files = sorted((env.store_dir / "staging" / f"rec-{final['session_id']}").glob("seg-*.pcm"))
    assert len(files) == 2 and files[0].stat().st_size == 5 * 16000 * 2
    assert source.closed
    status = env.svc.recording_status(env.project_id, OWNER, final["session_id"])
    assert status["segments"] == 2 and 7500 <= status["elapsed_ms"] <= 8000

    recovered = env.svc.recording_recover(env.project_id, OWNER, final["session_id"])
    assert recovered["state"] == "imported" and recovered["partial"] is True
    assert 7500 <= recovered["duration_ms"] <= 8000
    description = env.svc.get(env.project_id, OWNER, recovered["source_id"], None)["body"]
    assert description["description"].startswith("Partial recording (device lost")
    assert not (env.store_dir / "staging" / f"rec-{final['session_id']}").exists()
    with pytest.raises(PrometheosError):  # already registered
        env.svc.recording_recover(env.project_id, OWNER, final["session_id"])


def test_a_lost_second_device_ends_the_session_visibly_not_silently(env: AudioEnv) -> None:
    backend = ScriptedBackend(
        {"mic-1": ScriptedSource(MIC, 10**6), "loop-1": ScriptedSource(LOOP, 30, ending="lost")}
    )
    final = run(env, backend, request("Call", ("mic-1", "loop-1")))
    assert (final["state"] == "interrupted" and "Test system audio" in final["reason"]) or (
        "device lost" in final["reason"]
    )


def test_a_device_that_cannot_be_opened_is_reported(env: AudioEnv) -> None:
    class Broken(ScriptedBackend):
        def open(self, device_id: str) -> capture.CaptureSource:
            raise capture.DeviceLost("access denied by privacy settings")

    backend = Broken({"mic-1": ScriptedSource(MIC, 10)})
    final = run(env, backend, request())
    assert final["state"] == "interrupted" and "access denied" in final["reason"]
    assert (
        env.svc.recording_discard(env.project_id, OWNER, final["session_id"], confirm=True)["state"]
        == "discarded"
    )


def wait_until(predicate: Any, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def start_endless(
    env: AudioEnv, sensitivity: str = "normal"
) -> tuple[dict, threading.Thread, dict]:
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 10**9)})
    session = env.svc.recording_start(
        env.project_id,
        OWNER,
        request(segment_seconds=5, sensitivity=sensitivity),
        backend,
        authority=OWNER_ACT,
    )
    result: dict[str, Any] = {}

    def body() -> None:
        try:
            result["final"] = env.svc.recording_run(
                env.project_id, session["session_id"], backend, stall_seconds=30
            )
        except BaseException as exc:  # surfaced by the test
            result["error"] = exc

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    wait_until(
        lambda: (
            env.svc.recording_status(env.project_id, OWNER, session["session_id"])["elapsed_ms"] > 0
        )
    )
    return session, thread, result


def test_cancel_stops_capture_and_keeps_segments_until_discarded(env: AudioEnv) -> None:
    session, thread, result = start_endless(env)
    live = env.svc.recording_status(env.project_id, OWNER, session["session_id"])
    assert live["state"] == "recording" and live["level_dbfs"] > -40 and live["elapsed_ms"] > 0
    env.svc.recording_control(env.project_id, OWNER, session["session_id"], "cancel")
    thread.join(20)
    assert not thread.is_alive() and "error" not in result
    final = result["final"]
    assert final["state"] == "cancelled" and final["recoverable"] is True
    directory = env.store_dir / "staging" / f"rec-{session['session_id']}"
    assert list(directory.glob("seg-*.pcm"))
    # The orphan collector must not take an unfinished or cancelled recording.
    report = env.svc.orphans(env.project_id, collect=True, grace_seconds=0)
    assert all(o["name"] != directory.name for o in report["orphans"])
    assert directory.exists()
    assert (
        env.svc.recording_discard(env.project_id, OWNER, session["session_id"], confirm=True)[
            "state"
        ]
        == "discarded"
    )
    assert not directory.exists()
    with pytest.raises(PrometheosError):
        env.svc.recording_recover(env.project_id, OWNER, session["session_id"])


def test_restricted_sessions_are_invisible_and_uncontrollable_for_agents(env: AudioEnv) -> None:
    session, thread, result = start_endless(env, "restricted")
    sid = session["session_id"]
    assert env.svc.recording_list(env.project_id, AGENT)["sessions"] == []
    assert [s["session_id"] for s in env.svc.recording_list(env.project_id, OWNER)["sessions"]] == [
        sid
    ]
    for call in (
        lambda: env.svc.recording_status(env.project_id, AGENT, sid),
        lambda: env.svc.recording_control(env.project_id, AGENT, sid, "stop"),
        lambda: env.svc.recording_control(env.project_id, AGENT, sid, "cancel"),
        lambda: env.svc.recording_recover(env.project_id, AGENT, sid),
        lambda: env.svc.recording_discard(env.project_id, AGENT, sid),
    ):
        with pytest.raises(PrometheosError) as err:
            call()
        assert err.value.code == "not_found"
    env.svc.recording_control(env.project_id, OWNER, sid, "stop")
    thread.join(30)
    final = result["final"]
    assert final["state"] == "imported"
    with pytest.raises(PrometheosError):  # the finished session still hides its version id
        env.svc.recording_status(env.project_id, AGENT, sid)
    assert env.svc.recording_list(env.project_id, AGENT, True)["sessions"] == []


def test_recording_commit_is_bound_to_the_rank_of_the_caller_who_started_it(env: AudioEnv) -> None:
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 20)})
    session = env.svc.recording_start(
        env.project_id,
        AGENT,
        request("Agent started", sensitivity="normal"),
        backend,
        authority=OWNER_ACT,
    )
    assert env.query("SELECT max_rank FROM recording_sessions")[0][0] == 0
    final = env.svc.recording_run(env.project_id, session["session_id"], backend, stall_seconds=30)
    assert final["state"] == "imported"
    # The registering commit was made at rank 0: the same request at another rank conflicts
    # instead of replaying IDs created for a different rank.
    store = env.svc.open(env.project_id)
    version = env.svc.get(env.project_id, OWNER, final["version_id"], None)["body"]
    from audio_support import write_wav

    clip = env.tmp / "again.wav"
    write_wav(clip, [(500, 0.3)])
    rid = recorder.derived_request_id("recording", env.project_id, session["session_id"])
    with pytest.raises(PrometheosError) as err:
        recorder.importing.import_recording(
            store, clip, title="x", sensitivity="normal", description="x", actor="t",
            request_id=rid, max_rank=2,
        )  # fmt: skip
    assert err.value.code == "request_key_conflict" and version["availability"] == "retained"


def test_pause_resume_and_stop_register_the_recording(env: AudioEnv) -> None:
    session, thread, result = start_endless(env)
    sid = session["session_id"]
    env.svc.recording_control(env.project_id, OWNER, sid, "pause")
    wait_until(lambda: env.svc.recording_status(env.project_id, OWNER, sid)["state"] == "paused")
    time.sleep(1.3)  # a heartbeat after the pause has stored the final count
    frozen = env.svc.recording_status(env.project_id, OWNER, sid)["elapsed_ms"]
    time.sleep(1.6)
    assert (
        env.svc.recording_status(env.project_id, OWNER, sid)["elapsed_ms"] == frozen
    )  # no audio kept
    env.svc.recording_control(env.project_id, OWNER, sid, "resume")
    wait_until(lambda: env.svc.recording_status(env.project_id, OWNER, sid)["state"] == "recording")
    env.svc.recording_control(env.project_id, OWNER, sid, "stop")
    thread.join(30)
    assert not thread.is_alive() and "error" not in result
    final = result["final"]
    assert final["state"] == "imported" and final["version_id"]
    with pytest.raises(PrometheosError):  # a finished session takes no more control
        env.svc.recording_control(env.project_id, OWNER, sid, "stop")


def test_a_crashed_recorder_is_detected_and_recovered(env: AudioEnv) -> None:
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 10)})
    session = env.svc.recording_start(
        env.project_id, OWNER, request("Crash"), backend, authority=OWNER_ACT
    )
    sid = session["session_id"]
    directory = env.store_dir / "staging" / f"rec-{sid}"
    one_second = pcm.samples_to_bytes(pcm.tone(440.0, 1000))
    (directory / "seg-00001.pcm").write_bytes(one_second + b"\x01")  # torn final sample
    assert env.svc.recording_status(env.project_id, OWNER, sid)["abandoned"] is False
    with pytest.raises(PrometheosError):
        env.svc.recording_recover(env.project_id, OWNER, sid)  # still looks alive
    env.execute(
        "UPDATE recording_sessions SET heartbeat_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
        (sid,),
    )
    status = env.svc.recording_status(env.project_id, OWNER, sid)
    assert status["abandoned"] is True and status["recoverable"] is True
    report = env.svc.orphans(env.project_id, collect=True, grace_seconds=0)
    assert all(o["name"] != directory.name for o in report["orphans"])
    recovered = env.svc.recording_recover(env.project_id, OWNER, sid)
    assert recovered["state"] == "imported" and recovered["duration_ms"] == 1000
    assert recovered["partial"] is True


def test_nothing_recorded_is_discarded_not_registered(env: AudioEnv) -> None:
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 10)})
    session = env.svc.recording_start(
        env.project_id, OWNER, request("Empty"), backend, authority=OWNER_ACT
    )
    env.execute(
        "UPDATE recording_sessions SET state = 'interrupted' WHERE id = ?", (session["session_id"],)
    )
    with pytest.raises(PrometheosError, match="nothing was recorded"):
        env.svc.recording_recover(env.project_id, OWNER, session["session_id"])
    assert (
        env.svc.recording_status(env.project_id, OWNER, session["session_id"])["state"]
        == "discarded"
    )


@pytest.mark.slow
def test_process_killed_after_a_segment_is_recoverable(env: AudioEnv) -> None:
    done = env.worker("record", env.project_id, "Killed", fault="recording_after_segment")
    assert done.returncode == 86, done.stderr  # os._exit at the fault point
    sessions = env.svc.recording_list(env.project_id, OWNER)["sessions"]
    assert len(sessions) == 1 and sessions[0]["state"] == "recording"
    sid = sessions[0]["session_id"]
    env.execute(
        "UPDATE recording_sessions SET heartbeat_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
        (sid,),
    )
    recovered = env.svc.recording_recover(env.project_id, OWNER, sid)
    assert recovered["state"] == "imported" and recovered["duration_ms"] >= 5000
    version = env.svc.get(env.project_id, OWNER, recovered["version_id"], None)["body"]
    assert version["availability"] == "retained"


# ----- the real backend's adapter logic against a stand-in vendor module ---------------------


class FakeStream:
    def __init__(self, fail_after: int | None) -> None:
        self.reads = 0
        self.fail_after = fail_after
        self.closed = False

    def read(self, frames: int, exception_on_overflow: bool = True) -> bytes:
        assert exception_on_overflow is False  # a glitch must not abort a recording
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise OSError(-9988, "Stream closed")
        return bytes(frames * 2 * 2)

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakePyAudio:
    paWASAPI = 13
    paInt16 = 8
    last_open: ClassVar[dict[str, Any]] = {}
    fail_after: ClassVar[int | None] = None

    DEVICES: ClassVar[list[dict[str, Any]]] = [
        {"index": 1, "hostApi": 0, "name": "MME mic", "maxInputChannels": 2,
         "defaultSampleRate": 44100.0},
        {"index": 15, "hostApi": 3, "name": "Array (Realtek)", "maxInputChannels": 2,
         "defaultSampleRate": 48000.0, "isLoopbackDevice": False},
        {"index": 16, "hostApi": 3, "name": "Speakers [Loopback]", "maxInputChannels": 2,
         "defaultSampleRate": 48000.0, "isLoopbackDevice": True},
        {"index": 17, "hostApi": 3, "name": "Output only", "maxInputChannels": 0,
         "defaultSampleRate": 48000.0, "isLoopbackDevice": False},
    ]  # fmt: skip

    def get_host_api_info_by_type(self, kind: int) -> dict[str, Any]:
        return {"index": 3, "defaultInputDevice": 15}

    def get_default_wasapi_loopback(self) -> dict[str, Any]:
        return {"index": 16}

    def get_device_count(self) -> int:
        return max(d["index"] for d in self.DEVICES) + 1  # PyAudio: index == position

    def get_device_info_by_index(self, index: int) -> dict[str, Any]:
        for device in self.DEVICES:
            if device["index"] == index:
                return device
        return {"index": index, "hostApi": 9, "name": "unused", "maxInputChannels": 0,
                "defaultSampleRate": 0.0}  # fmt: skip

    def open(self, **kwargs: Any) -> FakeStream:
        FakePyAudio.last_open = kwargs
        return FakeStream(FakePyAudio.fail_after)

    def terminate(self) -> None:
        pass


def test_wasapi_backend_enumerates_and_opens_through_the_vendor_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = types.ModuleType("pyaudiowpatch")
    module.PyAudio = FakePyAudio  # type: ignore[attr-defined]
    module.paWASAPI = 13  # type: ignore[attr-defined]
    module.paInt16 = 8  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", module)
    monkeypatch.setattr(sys, "platform", "win32")
    backend = capture.PyAudioWPatchBackend()
    devices = {d.id: d for d in backend.devices()}
    assert set(devices) == {"wasapi:15", "wasapi:16"}  # WASAPI inputs and loopbacks only
    assert devices["wasapi:15"].kind == "microphone" and devices["wasapi:15"].default
    assert devices["wasapi:16"].kind == "loopback" and devices["wasapi:16"].default
    source = backend.open("wasapi:16")
    assert FakePyAudio.last_open["input_device_index"] == 16
    assert FakePyAudio.last_open["rate"] == 48000 and FakePyAudio.last_open["input"] is True
    assert len(source.read(2400) or b"") == 2400 * 2 * 2
    source.close()
    FakePyAudio.fail_after = 1
    try:
        lost = backend.open("wasapi:15")
        lost.read(10)
        with pytest.raises(capture.DeviceLost):  # the vendor error becomes DeviceLost
            lost.read(10)
    finally:
        FakePyAudio.fail_after = None
    with pytest.raises(PrometheosError):
        backend.open("wasapi:99")


def test_backend_choice_needs_explicit_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engines.SYNTHETIC_ENV, raising=False)
    with pytest.raises(PrometheosError) as err:
        capture.get_backend("synthetic")
    assert err.value.code == "unsupported"
    with pytest.raises(PrometheosError) as unknown:
        capture.get_backend("alsa")
    assert unknown.value.code == "validation_error"
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    backend = capture.get_backend("synthetic")
    assert {d.kind for d in backend.devices()} == {"microphone", "loopback"}


def test_synthetic_backend_records_through_the_same_loop(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    backend = capture.SyntheticBackend(speed=0)
    session = env.svc.recording_start(
        env.project_id, OWNER, request("Tone", ("synthetic-mic",)), backend, authority=OWNER_ACT
    )
    thread_result: dict[str, Any] = {}

    def go() -> None:
        thread_result["final"] = env.svc.recording_run(
            env.project_id, session["session_id"], backend, max_seconds=1.0, stall_seconds=30
        )

    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    worker.join(60)
    final = thread_result["final"]
    assert final["state"] == "imported" and 1000 <= final["duration_ms"] <= 1300
