"""CP6 over MCP: the same audio operations with agent authority only. Restricted audio is
invisible, nothing records the microphone or installs a model or spends money from here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from audio_support import AudioEnv
from cp1_support import CP10_REQUEST_TOOLS, OWNER
from mcp import Client

from prometheos.audio import engines
from prometheos.domain.ids import new_request_id
from prometheos.mcp_adapter import build_server


@pytest.fixture(autouse=True)
def synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")


@pytest.fixture
def env(tmp_path: Path) -> AudioEnv:
    return AudioEnv(tmp_path)


def run_steps(env: AudioEnv, steps: Any) -> Any:
    async def main() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await steps(client)

    return anyio.run(main)


def doc(result: Any) -> Any:
    assert not result.is_error, result.content[0].text
    return result.structured_content


def test_audio_flow_over_mcp_matches_the_service(env: AudioEnv) -> None:
    env.audio_file()

    async def steps(client: Client) -> dict[str, Any]:
        # An operation run (CP10.3, decision 0017): the import is the run's result.
        run = doc(
            await client.call_tool(
                "audio_import",
                {
                    "root_alias": "research",
                    "path": "meeting.wav",
                    "sensitivity": "normal",
                    "wait_seconds": 20,
                },
            )
        )
        assert run["state"] == "completed" and run["run_id"].startswith("oprun_")
        imported = run["result"]
        started = doc(
            await client.call_tool(
                "audio_transcribe",
                {
                    "version_id": imported["version_id"],
                    "engine": "synthetic",
                    "chunk_seconds": 4,
                    "overlap_seconds": 2,
                },
            )
        )
        status: dict[str, Any] = started
        for _ in range(200):  # the run executes in the server process; poll it
            status = doc(
                await client.call_tool("audio_transcription_status", {"run_id": started["run_id"]})
            )
            if status["state"] in ("completed", "failed", "cancelled"):
                break
            await anyio.sleep(0.05)
        assert status["state"] == "completed", status
        transcript = doc(
            await client.call_tool("transcript_get", {"version_id": status["result_version_id"]})
        )
        span = doc(
            await client.call_tool(
                "audio_span",
                {"version_id": status["result_version_id"], "start_ms": 0, "end_ms": 2000},
            )
        )
        fixed = doc(
            await client.call_tool(
                "transcript_correct",
                {
                    "version_id": status["result_version_id"],
                    "edits": [{"segment_id": "seg-00001", "text": "Compare the controllers."}],
                    "request_id": new_request_id(),
                },
            )
        )
        fixed_view = doc(
            await client.call_tool("transcript_get", {"version_id": fixed["version_id"]})
        )
        meeting = doc(
            await client.call_tool(
                "meeting_from_audio",
                {
                    "transcript_version_id": fixed["version_id"],
                    "audio_version_id": imported["version_id"],
                    "title": "Call",
                    "request_id": new_request_id(),
                },
            )
        )
        again = doc(
            await client.call_tool(
                "audio_transcribe",
                {"version_id": imported["version_id"], "engine": "synthetic"},
            )
        )
        return {
            "imported": imported, "status": status, "transcript": transcript, "span": span,
            "fixed": fixed, "meeting": meeting, "again": again, "fixed_view": fixed_view,
        }  # fmt: skip

    out = run_steps(env, steps)
    from cp1_support import AGENT

    direct = env.svc.transcript_get(env.project_id, AGENT, out["fixed"]["version_id"])
    assert out["fixed_view"] == json.loads(json.dumps(direct))
    assert out["transcript"]["superseded_by_correction"] is False  # read before the correction
    assert out["span"]["quote"] == "[synthetic segment 1]"  # honestly labelled placeholder
    assert out["fixed"]["segments_changed"] == 1 and out["meeting"]["sensitivity"] == "normal"
    # A second call with other settings than the finished run starts a new run (not a replay).
    assert out["again"]["run_id"] != out["status"]["run_id"]


def test_restricted_audio_is_invisible_to_mcp(env: AudioEnv) -> None:
    owner_import = env.import_audio(sensitivity="restricted")

    async def steps(client: Client) -> list[Any]:
        listed = await client.call_tool("record_list", {"kind": "version"})
        blocked = await client.call_tool(
            "audio_transcribe", {"version_id": owner_import["version_id"], "engine": "synthetic"}
        )
        span = await client.call_tool("transcript_get", {"version_id": owner_import["version_id"]})
        # An agent can register audio, but a fresh import is restricted and then unreadable.
        env.audio_file("later.wav")
        fresh = await client.call_tool(
            "audio_import", {"root_alias": "research", "path": "later.wav", "wait_seconds": 20}
        )
        fresh_read = await client.call_tool(
            "record_get", {"record_id": fresh.structured_content["result"]["version_id"]}
        )
        return [listed, blocked, span, fresh, fresh_read]

    listed, blocked, span, fresh, fresh_read = run_steps(env, steps)
    assert listed.structured_content["items"] == []
    for denied in (blocked, span, fresh_read):
        assert denied.is_error and "not_found" in denied.content[0].text
    assert fresh.structured_content["state"] == "completed"
    assert fresh.structured_content["result"]["sensitivity"] == "restricted"
    assert fresh.structured_content["artifacts"] == []  # the agent cannot see what it imported


def test_capture_install_and_spend_tools_only_request_the_owner(env: AudioEnv) -> None:
    """CP10.2 (decision 0016): recording, model installs and provider calls exist on MCP only
    as approval requests (approval_required); nothing records, downloads or spends."""

    async def steps(client: Client) -> Any:
        names = {t.name for t in (await client.list_tools()).tools}
        start = await client.call_tool(
            "recording_start", {"title": "T", "device_ids": ["synthetic-mic"]}
        )
        return names, start

    names, start = run_steps(env, steps)
    for forbidden in ("audio_record_start", "audio_model_install", "provider_secret_set"):
        assert forbidden not in names
    # Starting is only ever the request; controlling a session that exists is the family tool
    # (CP10.6, decision 0022: pause/resume/stop/cancel/recover/discard under creator-rank rules).
    recording = sorted(n for n in names if n.startswith(("recording", "audio_record")))
    assert recording == ["recording_manage", "recording_start"]
    assert {"recording_start", "audio_models_install", "provider_run"} <= CP10_REQUEST_TOOLS
    assert start.is_error and "approval_required" in start.content[0].text
    assert env.svc.recording_list(env.project_id, OWNER, True)["sessions"] == []
    # The pure model plan is offered; installing stays with the owner.
    assert {"audio_models_plan", "audio_models_status"} <= names


def test_model_plan_over_mcp_downloads_nothing(
    env: AudioEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_models = tmp_path / "empty-synthetic-models"
    empty_models.mkdir()
    monkeypatch.setenv("PROMETHEOS_MODELS_DIR", str(empty_models))

    async def steps(client: Client) -> Any:
        return doc(await client.call_tool("audio_models_plan", {}))

    plan = run_steps(env, steps)
    assert plan["license"] == "MIT" and plan["installed"] is False
    assert not list(empty_models.iterdir())
    assert not (env.home / "models").exists()
