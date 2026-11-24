"""Regression tests for the CP6 pre-checkpoint review (reviewer-subagent-cp6-a, decision 0013
"Review round"). Each test failed before its fix; the reviewer's probes P1-P4 and the hostile
WAV fixtures are adapted here."""

from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from audio_support import MIC, AudioEnv, ScriptedBackend, ScriptedSource, write_wav
from conftest import requires_private_records
from cp1_support import AGENT, CP10_REQUEST_TOOLS, OWNER, OWNER_ACT, create_entry
from test_cp6_audio_capture import request as recording_request
from test_cp6_audio_capture import start_endless

from prometheos.audio import engines, pcm, recorder, transcribe, wav
from prometheos.domain.models import PrometheosError


@pytest.fixture
def aenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AudioEnv:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    return AudioEnv(tmp_path)


# ----- M4 / L1 / L2: authority over recordings ------


def test_an_agent_cannot_control_recover_or_discard_an_owners_normal_recording(
    aenv: AudioEnv,
) -> None:
    session, thread, result = start_endless(aenv, "normal")
    sid = session["session_id"]
    for action in ("pause", "stop", "cancel"):
        with pytest.raises(PrometheosError) as err:
            aenv.svc.recording_control(aenv.project_id, AGENT, sid, action)
        assert err.value.code == "policy_denied", action
    assert aenv.svc.recording_status(aenv.project_id, AGENT, sid)["state"] == "recording"
    aenv.svc.recording_control(aenv.project_id, OWNER, sid, "cancel")
    thread.join(20)
    assert result["final"]["state"] == "cancelled"  # visible to the agent, still not its to touch
    assert aenv.svc.recording_status(aenv.project_id, AGENT, sid)["state"] == "cancelled"
    for call in (
        lambda: aenv.svc.recording_recover(aenv.project_id, AGENT, sid),
        lambda: aenv.svc.recording_discard(aenv.project_id, AGENT, sid, confirm=True),
    ):
        with pytest.raises(PrometheosError) as err:
            call()
        assert err.value.code == "policy_denied"
    directory = aenv.store_dir / "staging" / f"rec-{sid}"
    assert list(directory.glob("seg-*.pcm"))  # the segments are untouched
    # Even the owner must confirm: deletion is for good.
    with pytest.raises(PrometheosError) as unconfirmed:
        aenv.svc.recording_discard(aenv.project_id, OWNER, sid)
    assert unconfirmed.value.code == "validation_error" and list(directory.glob("seg-*.pcm"))
    assert (
        aenv.svc.recording_discard(aenv.project_id, OWNER, sid, confirm=True)["state"]
        == "discarded"
    )
    assert not directory.exists()


def test_an_agent_may_control_a_recording_it_started_itself(aenv: AudioEnv) -> None:
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 10**9)})
    session = aenv.svc.recording_start(
        aenv.project_id, AGENT, recording_request(segment_seconds=5), backend, authority=OWNER_ACT
    )
    sid = session["session_id"]
    done: dict[str, Any] = {}

    def go() -> None:
        done["final"] = aenv.svc.recording_run(aenv.project_id, sid, backend, stall_seconds=30)

    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    deadline = time.monotonic() + 20
    while aenv.svc.recording_status(aenv.project_id, AGENT, sid)["elapsed_ms"] == 0:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    aenv.svc.recording_control(aenv.project_id, AGENT, sid, "cancel")
    worker.join(20)
    assert (
        aenv.svc.recording_discard(aenv.project_id, AGENT, sid, confirm=True)["state"]
        == "discarded"
    )


def test_terminal_session_states_never_change(aenv: AudioEnv) -> None:
    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 20)})
    session = aenv.svc.recording_start(
        aenv.project_id, OWNER, recording_request(), backend, authority=OWNER_ACT
    )
    final = aenv.svc.recording_run(
        aenv.project_id, session["session_id"], backend, stall_seconds=30
    )
    assert final["state"] == "imported"
    store = aenv.svc.open(aenv.project_id)
    recorder._update(store, session["session_id"], state="stopped", reason="late recorder")
    recorder._update(store, session["session_id"], state="discarded")
    status = aenv.svc.recording_status(aenv.project_id, OWNER, session["session_id"])
    assert status["state"] == "imported" and status["reason"] != "late recorder"
    assert status["version_id"] == final["version_id"]


# ----- M5: hostile WAV headers ------


def make_wav(
    path: Path, channels: int, rate: int, width: int, declared: int, actual: bytes
) -> Path:
    fmt = struct.pack(
        "<HHIIHH", 1, channels, rate, (rate * channels * width) & 0xFFFFFFFF,
        (channels * width) & 0xFFFF, width * 8,
    )  # fmt: skip
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", declared)
    path.write_bytes(
        b"RIFF" + struct.pack("<I", (len(body) + len(actual)) & 0xFFFFFFFF) + body + actual
    )
    return path


@pytest.mark.parametrize(
    "channels, rate, width",
    [(1, 1, 2), (1, 3999, 2), (1, 192_001, 2), (2, 16000, 5), (0, 16000, 2),
     (9, 16000, 2), (65535, 16000, 4), (16383, 16000, 4)],
)  # fmt: skip
def test_headers_outside_the_bounds_are_refused_fast(
    tmp_path: Path, channels: int, rate: int, width: int
) -> None:
    path = make_wav(tmp_path / "hostile.wav", channels, rate, width, 0xFFFFFF00, b"\x01" * 64)
    started = time.monotonic()
    with pytest.raises(PrometheosError) as err:
        wav.inspect_wav(path)
    assert err.value.code in ("validation_error", "unsupported")
    with pytest.raises(PrometheosError):
        wav.normalize_to_raw16k(path, tmp_path / "out.raw")
    assert time.monotonic() - started < 5
    assert not (tmp_path / "out.raw").exists() or (tmp_path / "out.raw").stat().st_size == 0


def test_the_one_hertz_header_that_expanded_to_hundreds_of_megabytes(tmp_path: Path) -> None:
    path = make_wav(tmp_path / "rate1.wav", 1, 1, 2, 20000, b"\x07" * 20000)
    with pytest.raises(PrometheosError) as err:
        wav.normalize_to_raw16k(path, tmp_path / "rate1.raw")
    assert err.value.code == "validation_error" and "sample rate" in err.value.message


def test_the_length_comes_from_the_bytes_present_not_the_header(tmp_path: Path) -> None:
    path = make_wav(tmp_path / "liar.wav", 2, 16000, 2, 0xFFFFFF00, b"\x00" * 64)
    info = wav.inspect_wav(path)
    assert info.frames == 16 and info.duration_ms == 1  # 64 bytes, not hours
    data = path.read_bytes()
    assert wav.wav_info_bytes(data) == info
    raw = tmp_path / "liar.raw"
    assert wav.normalize_to_raw16k(path, raw) == 16 and raw.stat().st_size == 32


def test_a_span_beyond_the_real_audio_is_refused_even_if_the_header_claims_more(
    aenv: AudioEnv,
) -> None:
    (aenv.research / "liar.wav").write_bytes(
        make_wav(aenv.tmp / "x.wav", 1, 16000, 2, 0xFFFFFF00, b"\x01\x00" * 16000).read_bytes()
    )
    imported = aenv.svc.audio_import(
        aenv.project_id, OWNER, root_alias="research", path="liar.wav", sensitivity="normal"
    )
    assert imported["audio"]["duration_ms"] == 1000
    cite = {
        "source_id": imported["source_id"], "version_id": imported["version_id"],
        "locator": {"type": "audio", "start_ms": 0, "end_ms": 5000}, "quote_sha256": None,
    }  # fmt: skip
    with pytest.raises(PrometheosError, match="beyond the recording"):
        aenv.apply([create_entry(evidence=[cite])])
    cite["locator"]["end_ms"] = 900  # type: ignore[index]
    assert aenv.apply([create_entry(evidence=[cite])])["ok"]


def test_import_refuses_a_hostile_header_before_anything_commits(aenv: AudioEnv) -> None:
    make_wav(aenv.research / "bad.wav", 1, 1, 2, 20000, b"\x01" * 20000)
    with pytest.raises(PrometheosError) as err:
        aenv.svc.audio_import(aenv.project_id, OWNER, root_alias="research", path="bad.wav")
    assert err.value.code == "validation_error"
    assert aenv.svc.list_records(aenv.project_id, OWNER, kind="version")["items"] == []


def test_a_long_wav_beyond_the_twelve_hour_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wav, "MAX_DECODED_SECONDS", 1)
    write_wav(tmp_path / "two.wav", [(2000, 0.3)])
    with pytest.raises(PrometheosError, match="12 hour"):
        wav.inspect_wav(tmp_path / "two.wav")


# ----- L1: no IDs above the caller's rank in an advanced run's response ------


def test_a_response_to_an_agent_carries_no_ids_above_its_rank(
    aenv: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = aenv.import_audio(sensitivity="normal")
    run = aenv.svc.transcribe_start(
        aenv.project_id, AGENT, audio["version_id"], aenv.synthetic_config()
    )
    hidden = aenv.apply([create_entry(title="Hidden", text="x", sensitivity="restricted")])
    hidden_id = hidden["results"][0]["id"]
    real = transcribe.execute

    def leaky(*a: Any, **k: Any) -> dict[str, Any]:
        out = real(*a, **k)
        return out | {"result_version_id": hidden_id, "source_id": hidden_id}

    monkeypatch.setattr(transcribe, "execute", leaky)
    answer = aenv.svc.transcribe_run(aenv.project_id, AGENT, run["run_id"])
    assert hidden_id not in json.dumps(answer)


# ----- L3: agent corrections are labelled as agent corrections ------


def test_a_correction_is_labelled_by_who_made_it(aenv: AudioEnv) -> None:
    audio = aenv.import_audio(sensitivity="normal")
    done = aenv.svc.transcribe_audio(
        aenv.project_id, OWNER, audio["version_id"], aenv.synthetic_config()
    )
    first = done["result_version_id"]
    by_agent = aenv.svc.transcript_correct(
        aenv.project_id, AGENT, first, [transcribe.Edit("seg-00001", text="agent words")],
        "00000000-0000-4000-8000-0000000000a1",
    )  # fmt: skip
    label = aenv.svc.get(aenv.project_id, OWNER, by_agent["version_id"], None)["body"]
    assert label["transformation"]["engine"] == "agent-correction"
    by_owner = aenv.svc.transcript_correct(
        aenv.project_id, OWNER, by_agent["version_id"],
        [transcribe.Edit("seg-00001", text="owner words")], "00000000-0000-4000-8000-0000000000a2",
    )  # fmt: skip
    label = aenv.svc.get(aenv.project_id, OWNER, by_owner["version_id"], None)["body"]
    assert label["transformation"]["engine"] == "manual-correction"


# ----- L5: decoded caches are removed ------


def test_decoded_audio_caches_are_deleted_when_a_run_finishes_or_is_cancelled(
    aenv: AudioEnv,
) -> None:
    audio = aenv.import_audio(sensitivity="restricted")
    run = aenv.svc.transcribe_start(
        aenv.project_id, OWNER, audio["version_id"], aenv.synthetic_config()
    )
    cache = aenv.store_dir / "cache"
    store = aenv.svc.open(aenv.project_id)

    def crash(index: int) -> None:
        raise transcribe.Interrupted

    with pytest.raises(transcribe.Interrupted):
        transcribe.execute(store, aenv.svc.home, run["run_id"], owner="a", after_chunk=crash)
    assert list(cache.glob("pcm-*.raw"))  # kept for the resume
    aenv.execute(
        "UPDATE operation_runs SET lease_expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE lease_owner IS NOT NULL"
    )
    done = aenv.svc.transcribe_run(aenv.project_id, OWNER, run["run_id"])
    assert done["state"] == "completed"
    assert not list(cache.glob("pcm-*.raw")) and not list(cache.glob("audio-*.bin"))
    # Cancelling an idle run removes what an earlier attempt left behind.
    other = aenv.import_audio("second.wav", sensitivity="restricted")
    second = aenv.svc.transcribe_start(
        aenv.project_id, OWNER, other["version_id"], aenv.synthetic_config()
    )
    with pytest.raises(transcribe.Interrupted):
        transcribe.execute(store, aenv.svc.home, second["run_id"], owner="b", after_chunk=crash)
    assert list(cache.glob("pcm-*.raw"))
    aenv.execute(
        "UPDATE operation_runs SET lease_expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE lease_owner IS NOT NULL"
    )
    aenv.svc.transcription_cancel(aenv.project_id, OWNER, second["run_id"])
    assert not list(cache.glob("pcm-*.raw"))


# ----- L6: "latest" ignores deleted, archived and hidden versions ------


def test_the_latest_version_skips_archived_corrections(aenv: AudioEnv) -> None:
    audio = aenv.import_audio(sensitivity="normal")
    done = aenv.svc.transcribe_audio(
        aenv.project_id, OWNER, audio["version_id"], aenv.synthetic_config()
    )
    first = done["result_version_id"]
    fixed = aenv.svc.transcript_correct(
        aenv.project_id, OWNER, first, [transcribe.Edit("seg-00001", text="fixed")],
        "00000000-0000-4000-8000-0000000000b1",
    )  # fmt: skip
    assert (
        aenv.svc.transcript_get(aenv.project_id, OWNER, first)["latest_version_id"]
        == fixed["version_id"]
    )
    aenv.execute("UPDATE records SET lifecycle = 'archived' WHERE id = ?", (fixed["version_id"],))
    view = aenv.svc.transcript_get(aenv.project_id, OWNER, first)
    assert view["latest_version_id"] == first and view["superseded_by_correction"] is False


# ----- L7: a duplicate run call does not run the same transcription twice ------


def test_a_second_call_for_a_running_transcription_is_refused_as_busy(aenv: AudioEnv) -> None:
    audio = aenv.import_audio(sensitivity="restricted")
    run = aenv.svc.transcribe_start(
        aenv.project_id, OWNER, audio["version_id"], aenv.synthetic_config()
    )
    chunks: list[int] = []
    store = aenv.svc.open(aenv.project_id)

    def duplicate(index: int) -> None:
        chunks.append(index)
        if index == 0:
            with pytest.raises(PrometheosError) as err:
                transcribe.execute(
                    store,
                    aenv.svc.home,
                    run["run_id"],
                    owner="same-label",
                    after_chunk=chunks.append,
                )
            assert err.value.code == "busy" and "already running" in err.value.message

    done = transcribe.execute(
        store, aenv.svc.home, run["run_id"], owner="same-label", after_chunk=duplicate
    )
    assert done["state"] == "completed" and chunks == [0, 1, 2]  # each chunk computed once


# ----- L4: seeking does not re-read and re-hash the recording ------


class MediaIO:
    """What :mod:`prometheos.app.reads` does with retained media, and nothing else.

    ``hashed`` counts the bytes fed to SHA-256 by ``reads`` and ``read`` the bytes read from
    files under the store's media folder. The module's own ``hashlib`` name is replaced by a
    proxy, never the shared ``hashlib.sha256``: the admission gate hashes its keys with the same
    function, so patching the shared module counts unrelated calls (it once reported ``[1, 1, 1]``
    for a request that hashed no media at all)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, reads: Any, media_dir: Path) -> None:
        self.hashed = 0
        self.read = 0
        spy, real_hashlib, real_open = self, reads.hashlib, open
        root = media_dir.resolve()

        class Digest:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            def update(self, data: bytes) -> None:
                spy.hashed += len(data)
                self.inner.update(data)

            def hexdigest(self) -> str:
                return str(self.inner.hexdigest())

        class HashlibProxy:
            def sha256(self, data: bytes = b"", **kwargs: Any) -> Digest:
                spy.hashed += len(data)
                return Digest(real_hashlib.sha256(data, **kwargs))

            def __getattr__(self, name: str) -> Any:
                return getattr(real_hashlib, name)

        class CountedFile:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            def __enter__(self) -> CountedFile:
                self.inner.__enter__()
                return self

            def __exit__(self, *exc: object) -> Any:
                return self.inner.__exit__(*exc)

            def seek(self, *args: Any) -> Any:
                return self.inner.seek(*args)

            def read(self, *args: Any) -> bytes:
                data: bytes = self.inner.read(*args)
                spy.read += len(data)
                return data

        def counting_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            handle = real_open(file, mode, *args, **kwargs)
            is_media = isinstance(file, str | Path) and Path(file).resolve().is_relative_to(root)
            return CountedFile(handle) if is_media else handle

        monkeypatch.setattr(reads, "hashlib", HashlibProxy())
        monkeypatch.setattr(reads, "open", counting_open, raising=False)


def test_a_range_request_reads_only_the_slice_and_verifies_once(
    aenv: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app import reads

    path = aenv.research / "long.wav"
    with wave_writer(path) as handle:
        handle.writeframes(b"\x01\x02" * (5 * 1024 * 1024))  # 10 MiB: stored as media
    imported = aenv.svc.audio_import(
        aenv.project_id, OWNER, root_alias="research", path="long.wav", sensitivity="normal"
    )
    version = aenv.svc.get(aenv.project_id, OWNER, imported["version_id"], None)["body"]
    assert version["storage"] == "media"
    monkeypatch.setattr(
        reads,
        "version_content",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("whole file")),
    )
    data, total, mime, first, last = aenv.svc.version_audio_range(
        aenv.project_id, OWNER, imported["version_id"], 44, 143
    )
    assert (len(data), first, last, mime) == (
        100,
        44,
        143,
        "audio/wav",
    ) and total > 10 * 1024 * 1024
    tail = aenv.svc.version_audio_range(aenv.project_id, OWNER, imported["version_id"], None, 50)
    assert len(tail[0]) == 50 and tail[4] == total - 1
    media_file = aenv.store_dir / "media" / str(version["storage_key"])
    media = MediaIO(monkeypatch, reads, aenv.store_dir / "media")

    def request(first_byte: int, last_byte: int) -> bytes:
        return bytes(
            aenv.svc.version_audio_range(
                aenv.project_id, OWNER, imported["version_id"], first_byte, last_byte
            )[0]
        )

    expected = path.read_bytes()[1000:2000]
    assert request(1000, 1999) == expected
    assert media.hashed == 0  # verified once per size/mtime, not on every request
    assert media.read == 1000  # and only the requested slice was read
    with pytest.raises(PrometheosError):
        aenv.svc.version_audio_range(
            aenv.project_id, OWNER, imported["version_id"], total + 5, None
        )

    # The count is not vacuous: a changed mtime (same size) invalidates the verification
    # cache, the whole file is hashed once and read once more, and the next request is cheap.
    info = media_file.stat()
    os.utime(media_file, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    assert request(1000, 1999) == expected
    assert (media.hashed, media.read) == (total, total + 1000 + 1000)
    assert request(1000, 1999) == expected
    assert (media.hashed, media.read) == (total, total + 1000 + 1000 + 1000)

    # Corrupt bytes of the same size are never served: the file is verified again, refused,
    # and not read for the slice.
    with open(media_file, "r+b") as corrupt:
        corrupt.seek(1500)
        byte = corrupt.read(1)
        corrupt.seek(1500)
        corrupt.write(bytes([byte[0] ^ 0xFF]))
    before = media.read
    with pytest.raises(PrometheosError) as refused:
        request(1000, 1999)
    assert refused.value.code == "source_unavailable"
    assert media.hashed == 2 * total and media.read == before + total  # verified, no slice read


class wave_writer:
    def __init__(self, path: Path) -> None:
        import wave

        self._w = wave.open(str(path), "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(pcm.TARGET_RATE)

    def __enter__(self) -> Any:
        return self._w

    def __exit__(self, *exc: object) -> None:
        self._w.close()


# ----- I4: MCP never returns an absolute local path ------


def test_mcp_model_plan_has_no_absolute_path(aenv: AudioEnv) -> None:
    import anyio
    from mcp import Client

    from prometheos.mcp_adapter import build_server

    async def steps(client: Client) -> tuple[Any, Any]:
        plan = await client.call_tool("audio_models_plan", {})
        status = await client.call_tool("audio_models_status", {})
        return plan.structured_content, status.structured_content

    async def main() -> tuple[Any, Any]:
        async with Client(build_server(aenv.svc, aenv.project_id)) as client:
            return await steps(client)

    plan, status = anyio.run(main)
    text = json.dumps([plan, status])
    assert (
        str(aenv.home) not in text
        and "\\\\" not in plan["location"]
        and ":" not in plan["location"]
    )
    assert plan["location"].startswith("<PrometheOS models>/")
    assert Path(plan["location"]).drive == ""
    # The owner's CLI shows the exact configured location, including an explicit stage override.
    from prometheos.audio import models

    configured = models.models_dir(aenv.home)
    expected = models.target_dir(aenv.home, models.spec_for(models.DEFAULT_MODEL))
    assert str(configured) not in text
    assert Path(aenv.svc.audio_models_plan()["location"]) == expected


# ----- M6: the MCP inventory added by CP6 is exactly the table in decision 0013 -------------------

CP6_MCP_TOOLS: dict[str, tuple[str, str, str]] = {
    # tool: (service operation, annotation class, scope note)
    "audio_import": ("audio_import", "write-new", "agent"),
    "audio_transcribe": ("transcribe_start + transcribe_run", "write-idempotent", "agent"),
    "audio_transcription_status": ("transcription_status", "read-only", "agent"),
    "audio_transcription_cancel": ("transcription_cancel", "write-idempotent", "agent"),
    "transcript_get": ("transcript_get", "read-only", "agent"),
    "audio_span": ("audio_span", "read-only", "agent"),
    "transcript_correct": ("transcript_correct", "write-idempotent", "agent"),
    "meeting_from_audio": ("capture_meeting_from_transcript", "write-idempotent", "agent"),
    "audio_models_plan": ("audio_models_plan (paths redacted)", "read-only", "agent"),
    "audio_models_status": ("audio_models_status (paths redacted)", "read-only", "agent"),
    "provider_status": ("provider_status", "read-only", "agent"),
    "provider_preview": ("provider_preview", "read-only", "agent"),
}


@requires_private_records  # the table is in a private decision record (decision 0072)
def test_the_cp6_mcp_tools_match_the_inventory_table_in_decision_0013(aenv: AudioEnv) -> None:
    import anyio
    from mcp import Client

    from prometheos.mcp_adapter import build_server

    async def main() -> dict[str, Any]:
        async with Client(build_server(aenv.svc, aenv.project_id)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    tools = anyio.run(main)
    kind = {
        (True, False, True): "read-only",
        (False, False, True): "write-idempotent",
        (False, False, False): "write-new",
    }
    for name, (_operation, expected, _scope) in CP6_MCP_TOOLS.items():
        hints = tools[name].annotations
        assert hints is not None and hints.open_world_hint is False
        assert (
            kind[(hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint)] == expected
        )
    text = (
        Path(__file__).resolve().parents[1] / "development/decisions/0013-cp6-routine-decisions.md"
    ).read_text(encoding="utf-8")
    for name, (operation, expected, _scope) in CP6_MCP_TOOLS.items():
        row = next((line for line in text.splitlines() if f"| `{name}` |" in line), "")
        assert operation in row and expected in row, f"decision 0013 table row for {name}: {row!r}"
    # CP10.2 (decision 0016): these exist only as approval requests (always approval_required).
    for requested in (
        "provider_run",
        "provider_configure",
        "recording_start",
        "audio_models_install",
    ):
        assert requested in tools and requested in CP10_REQUEST_TOOLS


# ===== round 2 (review-precheck-cp6-a-r2) =====================================================


def test_a_cache_file_held_open_does_not_undo_a_completed_run(
    aenv: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-M1: on Windows an open handle makes the unlink fail. Simulated on every platform by
    making the removal of cache files raise; the run must stay completed, with exactly one
    transcript, and the leftover is swept later."""
    audio = aenv.import_audio(sensitivity="normal")
    run = aenv.svc.transcribe_start(
        aenv.project_id, OWNER, audio["version_id"], aenv.synthetic_config()
    )
    held: list[Any] = []

    def hold(_index: int) -> None:
        if not held:
            raw = next((aenv.store_dir / "cache").glob("pcm-*.raw"))
            held.append(open(raw, "rb"))

    real_unlink = Path.unlink
    state = {"released": False}

    def refuse(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith(("pcm-", "audio-")) and not state["released"]:
            raise PermissionError(13, "The process cannot access the file", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)
    store = aenv.svc.open(aenv.project_id)
    try:
        done = transcribe.execute(store, aenv.svc.home, run["run_id"], owner="x", after_chunk=hold)
    finally:
        for handle in held:
            handle.close()
    assert done["state"] == "completed" and done["result_version_id"]
    row = aenv.query(
        "SELECT state, (SELECT json_extract(value, '$.id') FROM json_each(artifacts) "
        "WHERE json_e"
        "xtract(value, '$.role') = 'transcript'), error FROM operation_runs WHERE id = ?",
        (run["run_id"],),
    )[0]
    assert row[0] == "completed" and row[1] == done["result_version_id"] and row[2] is None
    transcripts = [
        r
        for r in aenv.svc.list_records(aenv.project_id, OWNER, kind="source")["items"]
        if r["body"]["origin"] == "recording" and r["body"]["title"].startswith("Transcript")
    ]
    assert len(transcripts) == 1
    marker = aenv.store_dir / "cache" / transcribe.STALE_LIST
    assert marker.is_file() and "pcm-" in marker.read_text(encoding="utf-8")
    state["released"] = True  # the other handle is gone: the next sweep removes the leftover
    assert transcribe.sweep_stale_caches(store) == 0
    assert not marker.exists() and not list((aenv.store_dir / "cache").glob("pcm-*.raw"))


def test_a_failure_handler_never_downgrades_a_completed_run(aenv: AudioEnv) -> None:
    audio = aenv.import_audio(sensitivity="normal")
    done = aenv.svc.transcribe_audio(
        aenv.project_id, OWNER, audio["version_id"], aenv.synthetic_config()
    )
    transcribe._fail(aenv.svc.open(aenv.project_id), done["run_id"], "late error", drop_chunks=True)
    row = aenv.query("SELECT state, error FROM operation_runs WHERE id = ?", (done["run_id"],))
    assert row[0][0] == "completed" and row[0][1] is None


def test_cancel_succeeds_even_when_the_cache_cannot_be_removed(
    aenv: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = aenv.import_audio(sensitivity="restricted")
    run = aenv.svc.transcribe_start(
        aenv.project_id, OWNER, audio["version_id"], aenv.synthetic_config()
    )
    store = aenv.svc.open(aenv.project_id)

    def crash(_index: int) -> None:
        raise transcribe.Interrupted

    with pytest.raises(transcribe.Interrupted):
        transcribe.execute(store, aenv.svc.home, run["run_id"], owner="a", after_chunk=crash)
    aenv.execute(
        "UPDATE operation_runs SET lease_expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE lease_owner IS NOT NULL"
    )
    real_unlink = Path.unlink

    def refuse(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith("pcm-"):
            raise PermissionError(13, "in use", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)
    reply = aenv.svc.transcription_cancel(aenv.project_id, OWNER, run["run_id"])
    assert reply["outcome"] == "cancelled"
    assert (
        aenv.query("SELECT state FROM operation_runs WHERE operation = 'audio_transcribe'")[0][0]
        == "cancelled"
    )
    assert aenv.query("SELECT COUNT(*) FROM transcription_chunks")[0][0] == 0
    assert (aenv.store_dir / "cache" / transcribe.STALE_LIST).is_file()


def test_open_ended_range_requests_return_a_bounded_206_slice(aenv: AudioEnv) -> None:
    from prometheos.app import reads

    name = aenv.audio_file("long.wav", plan=[(1000, 0.3)] * 300)  # about 9.6 MB: stored as media
    imported = aenv.svc.audio_import(
        aenv.project_id, OWNER, root_alias="research", path=name, sensitivity="normal"
    )
    data, total, _mime, first, last = aenv.svc.version_audio_range(
        aenv.project_id, OWNER, imported["version_id"], 0, None
    )
    assert total > 2 * reads.MAX_AUDIO_RANGE_BYTES
    assert len(data) == reads.MAX_AUDIO_RANGE_BYTES and (first, last) == (0, len(data) - 1)
    mid = aenv.svc.version_audio_range(
        aenv.project_id, OWNER, imported["version_id"], 5_000_000, None
    )
    assert mid[3] == 5_000_000 and len(mid[0]) == reads.MAX_AUDIO_RANGE_BYTES
    tail = aenv.svc.version_audio_range(
        aenv.project_id, OWNER, imported["version_id"], total - 100, None
    )
    assert len(tail[0]) == 100 and tail[4] == total - 1  # near the end: what is left
    suffix = aenv.svc.version_audio_range(
        aenv.project_id, OWNER, imported["version_id"], None, 2000
    )
    assert len(suffix[0]) == 2000 and suffix[4] == total - 1


def test_the_hub_answers_open_ended_ranges_with_a_correct_content_range(aenv: AudioEnv) -> None:
    from hub_support import running_hub

    from prometheos.app import reads

    name = aenv.audio_file("long.wav", plan=[(1000, 0.3)] * 300)
    imported = aenv.svc.audio_import(
        aenv.project_id, OWNER, root_alias="research", path=name, sensitivity="normal"
    )
    path = f"/api/v1/versions/{imported['version_id']}/content"
    with running_hub(aenv.svc, aenv.project_id) as hub:
        hub.pair()
        reply = hub.request("GET", path, headers={"Range": "bytes=0-"})
        assert reply.status == 206 and len(reply.body) == reads.MAX_AUDIO_RANGE_BYTES
        total = int(reply.headers["content-range"].rsplit("/", 1)[1])
        assert reply.headers["content-range"] == f"bytes 0-{len(reply.body) - 1}/{total}"
        more = hub.request("GET", path, headers={"Range": f"bytes={len(reply.body)}-"})
        assert more.status == 206
        assert more.headers["content-range"].startswith(f"bytes {len(reply.body)}-")


def test_a_six_kilohertz_wav_imports_and_the_refusal_names_the_range(aenv: AudioEnv) -> None:
    write_wav(aenv.research / "dictaphone.wav", [(2000, 0.3)], rate=6000)
    imported = aenv.svc.audio_import(
        aenv.project_id, OWNER, root_alias="research", path="dictaphone.wav", sensitivity="normal"
    )
    assert imported["audio"]["sample_rate"] == 6000 and imported["audio"]["duration_ms"] == 2000
    raw = aenv.tmp / "six.raw"
    samples = wav.normalize_to_raw16k(aenv.research / "dictaphone.wav", raw)
    assert abs(samples - 2 * pcm.TARGET_RATE) <= 2
    make_wav(aenv.research / "tiny.wav", 1, 3999, 2, 100, b"\x01" * 100)
    with pytest.raises(PrometheosError) as err:
        aenv.svc.audio_import(aenv.project_id, OWNER, root_alias="research", path="tiny.wav")
    assert "4000-192000 Hz" in err.value.message and "convert" in err.value.message
    assert err.value.details["accepted_min"] == 4000


def test_the_model_plan_names_a_configured_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.audio import models

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    quiet = models.plan(tmp_path)
    assert not [h for h in quiet["hosts_contacted"] if h.startswith("via proxy")]
    monkeypatch.setattr(
        urllib.request, "getproxies", lambda: {"https": "http://user:secret@proxy.corp.test:3128"}
    )
    named = models.plan(tmp_path)
    assert "via proxy proxy.corp.test:3128" in named["hosts_contacted"]
    assert "secret" not in json.dumps(named) and "user" not in json.dumps(named["hosts_contacted"])
    assert named["plan_sha256"] != quiet["plan_sha256"]  # the reviewed plan changed


# ===== round 3 (review-precheck-cp6-a-r3) =====================================================

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="an open handle blocks unlink")


@windows_only
def test_a_really_open_cache_file_does_not_undo_a_completed_run(aenv: AudioEnv) -> None:
    """R3-I1: no patching. A plain open() on Windows denies deletion of the cache file for as
    long as the handle lives, exactly the antivirus or indexer case; the run stays completed."""
    audio = aenv.import_audio(sensitivity="normal")
    run = aenv.svc.transcribe_start(
        aenv.project_id, OWNER, audio["version_id"], aenv.synthetic_config()
    )
    held: list[Any] = []

    def hold(_index: int) -> None:
        if not held:
            raw = next((aenv.store_dir / "cache").glob("pcm-*.raw"))
            held.append(open(raw, "rb"))

    store = aenv.svc.open(aenv.project_id)
    try:
        done = transcribe.execute(store, aenv.svc.home, run["run_id"], owner="x", after_chunk=hold)
        assert held, "the cache file was never opened by the test"
        assert done["state"] == "completed" and done["result_version_id"]
        row = aenv.query(
            "SELECT state, (SELECT json_extract(value, '$.id') FROM json_each(artifacts) "
            "WHERE json_e"
            "xtract(value, '$.role') = 'transcript'), error FROM operation_runs WHERE id = ?",
            (run["run_id"],),
        )[0]
        assert row[0] == "completed" and row[1] == done["result_version_id"] and row[2] is None
        marker = aenv.store_dir / "cache" / transcribe.STALE_LIST
        assert marker.is_file() and "pcm-" in marker.read_text(encoding="utf-8")
        assert list((aenv.store_dir / "cache").glob("pcm-*.raw"))  # the handle kept it alive
    finally:
        for handle in held:
            handle.close()
    assert transcribe.sweep_stale_caches(store) == 0
    assert not marker.exists() and not list((aenv.store_dir / "cache").glob("pcm-*.raw"))


@windows_only
def test_cancel_succeeds_while_the_cache_file_is_really_open(aenv: AudioEnv) -> None:
    """R3-I1: the same without patching, across a cancel of an interrupted run."""
    audio = aenv.import_audio(sensitivity="restricted")
    run = aenv.svc.transcribe_start(
        aenv.project_id, OWNER, audio["version_id"], aenv.synthetic_config()
    )
    store = aenv.svc.open(aenv.project_id)

    def crash(_index: int) -> None:
        raise transcribe.Interrupted

    with pytest.raises(transcribe.Interrupted):
        transcribe.execute(store, aenv.svc.home, run["run_id"], owner="a", after_chunk=crash)
    aenv.execute(
        "UPDATE operation_runs SET lease_expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE lease_owner IS NOT NULL"
    )
    raw = next((aenv.store_dir / "cache").glob("pcm-*.raw"))
    handle = open(raw, "rb")
    try:
        reply = aenv.svc.transcription_cancel(aenv.project_id, OWNER, run["run_id"])
        assert reply["outcome"] == "cancelled"
        assert (
            aenv.query("SELECT state FROM operation_runs WHERE operation = 'audio_transcribe'")[0][
                0
            ]
            == "cancelled"
        )
        assert aenv.query("SELECT COUNT(*) FROM transcription_chunks")[0][0] == 0
        marker = aenv.store_dir / "cache" / transcribe.STALE_LIST
        assert marker.is_file() and raw.name in marker.read_text(encoding="utf-8")
        assert raw.exists()
    finally:
        handle.close()
    assert transcribe.sweep_stale_caches(store) == 0
    assert not raw.exists() and not marker.exists()
