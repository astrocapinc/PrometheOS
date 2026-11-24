"""CP6 hub routes over real HTTP: recording, import, transcription, transcript corrections,
playback with byte ranges, meeting from audio, provider status/preview/run. Owner session only;
every mutation needs the paired session and CSRF token like the rest of the hub."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from audio_support import AudioEnv
from hub_support import Hub, running_hub
from provider_support import SECRET, completion, config_doc

from prometheos.audio import engines
from prometheos.domain.ids import new_request_id
from prometheos.providers import transport as tp
from prometheos.providers.secrets import MemorySecretStore


@pytest.fixture(autouse=True)
def synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    monkeypatch.setenv("PROMETHEOS_CAPTURE_BACKEND", "synthetic")
    monkeypatch.setenv("PROMETHEOS_SYNTHETIC_SPEED", "0")


@pytest.fixture
def env(tmp_path: Path) -> AudioEnv:
    return AudioEnv(tmp_path)


@pytest.fixture
def hub(env: AudioEnv) -> Iterator[Hub]:
    with running_hub(env.svc, env.project_id) as running:
        running.pair()
        yield running


def wait(hub: Hub, path: str, done: Any, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = hub.request("GET", path)
        assert reply.status == 200, reply.body
        if done(reply.json()):
            return reply.json()  # type: ignore[no-any-return]
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting on {path}")


def test_new_mutations_need_the_session_and_the_csrf_token(hub: Hub) -> None:
    posts = [
        ("/api/v1/recordings", {"title": "x", "device_ids": ["synthetic-mic"]}),
        ("/api/v1/audio/import", {"root_alias": "research", "path": "a.wav"}),
        ("/api/v1/audio/transcriptions", {"version_id": "version_x"}),
        ("/api/v1/meetings/from-audio", {"transcript_version_id": "version_x", "title": "t"}),
        ("/api/v1/provider/preview", {"purpose": "notebook_draft"}),
        ("/api/v1/provider/run", {"purpose": "notebook_draft", "digest": "0" * 64}),
        ("/api/v1/provider/configure", {"config": {}}),
    ]
    for path, body in posts:
        assert hub.request("POST", path, body, csrf=False).status == 403, path
        assert hub.request("POST", path, body, cookie=False, csrf=False).status == 401, path
        assert hub.request("POST", path, body, origin="http://evil.example").status == 403, path
    for path in ("/api/v1/audio/devices", "/api/v1/recordings", "/api/v1/provider",
                 "/api/v1/audio/models"):  # fmt: skip
        assert hub.request("GET", path, cookie=False).status == 401, path


def test_recording_through_the_hub_is_visible_and_registers_the_audio(
    env: AudioEnv, hub: Hub
) -> None:
    devices = hub.request("GET", "/api/v1/audio/devices").json()
    assert {d["id"] for d in devices["devices"]} == {"synthetic-mic", "synthetic-loopback"}
    body = {"title": "Hub meeting", "device_ids": ["synthetic-mic", "synthetic-loopback"],
            "sensitivity": "normal", "segment_seconds": 5}  # fmt: skip
    refused = hub.request("POST", "/api/v1/recordings", body)
    assert refused.status == 403 and refused.json()["error"]["code"] == "policy_denied"
    assert "reminder" in refused.json()["error"]["details"]
    assert env.svc.recording_list(env.project_id, _owner())["sessions"] == []
    started = hub.request("POST", "/api/v1/recordings", body | {"consent_acknowledged": True})
    assert started.status == 200 and started.json()["state"] == "recording"
    sid = started.json()["session_id"]
    live = wait(hub, f"/api/v1/recordings/{sid}", lambda s: s["elapsed_ms"] > 0)
    assert live["state"] == "recording" and len(live["sources"]) == 2
    assert hub.request("POST", f"/api/v1/recordings/{sid}/pause").status == 200
    wait(hub, f"/api/v1/recordings/{sid}", lambda s: s["state"] == "paused")
    assert hub.request("POST", f"/api/v1/recordings/{sid}/resume").status == 200
    assert hub.request("POST", f"/api/v1/recordings/{sid}/stop").status == 200
    done = wait(hub, f"/api/v1/recordings/{sid}", lambda s: s["state"] == "imported")
    assert done["version_id"] and done["sensitivity"] == "normal"
    assert hub.request("POST", f"/api/v1/recordings/{sid}/stop").status == 400
    assert hub.request("POST", f"/api/v1/recordings/{sid}/explode").status == 400
    stored = env.svc.get(env.project_id, _owner(), done["version_id"], None)
    assert stored["body"]["mime"] == "audio/wav" and stored["body"]["availability"] == "retained"


def _owner() -> Any:
    from cp1_support import OWNER

    return OWNER


def test_import_transcribe_correct_meeting_and_reconcile_through_the_hub(
    env: AudioEnv, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_models = env.tmp / "empty-synthetic-models"
    empty_models.mkdir()
    monkeypatch.setenv("PROMETHEOS_MODELS_DIR", str(empty_models))
    env.audio_file()
    # An operation run (decision 0017): the import is the run's result.
    import_run = hub.request(
        "POST", "/api/v1/audio/import",
        {"root_alias": "research", "path": "meeting.wav", "sensitivity": "normal",
         "wait_seconds": 20},
    ).json()  # fmt: skip
    assert import_run["state"] == "completed" and import_run["run_id"].startswith("oprun_")
    imported = import_run["result"]
    assert imported["audio"]["duration_ms"] == 8000
    # No model installed: the real engine is refused with the explicit-install message.
    nomodel = hub.request(
        "POST", "/api/v1/audio/transcriptions", {"version_id": imported["version_id"]}
    )
    assert nomodel.status == 422 and "install" in nomodel.json()["error"]["message"]
    assert not list(empty_models.iterdir())
    started = hub.request(
        "POST", "/api/v1/audio/transcriptions",
        {"version_id": imported["version_id"], "engine": "synthetic"},
    )  # fmt: skip
    assert started.status == 200
    run = wait(
        hub, f"/api/v1/audio/transcriptions/{started.json()['run_id']}",
        lambda r: r["state"] in ("completed", "failed"),
    )  # fmt: skip
    assert run["state"] == "completed"
    tid = run["result_version_id"]
    view = hub.request("GET", f"/api/v1/transcripts/{tid}").json()
    assert len(view["payload"]["segments"]) == 3 and view["audio_available"] is True
    listed = hub.request("GET", f"/api/v1/audio/transcriptions?version_id={imported['version_id']}")
    assert [r["state"] for r in listed.json()["runs"]] == ["completed"]

    fix = hub.request(
        "POST", f"/api/v1/transcripts/{tid}/corrections",
        {"edits": [{"segment_id": "seg-00001", "text": "Compare the controllers."}]},
    )  # fmt: skip
    assert fix.status == 200 and fix.json()["segments_changed"] == 1
    meeting = hub.request(
        "POST", "/api/v1/meetings/from-audio",
        {"transcript_version_id": fix.json()["version_id"],
         "audio_version_id": imported["version_id"], "title": "Hub call"},
    ).json()  # fmt: skip
    reconcile = hub.request("GET", f"/api/v1/meetings/{meeting['meeting_id']}/reconcile")
    assert reconcile.status == 200 and reconcile.json()["items"] == []
    bad = hub.request("POST", f"/api/v1/transcripts/{tid}/corrections", {"edits": []})
    assert bad.status == 400
    cancelled = hub.request(
        "POST", f"/api/v1/audio/transcriptions/{started.json()['run_id']}/cancel"
    )
    assert cancelled.status == 400  # already completed


def test_audio_is_served_with_byte_ranges_and_a_locked_down_policy(env: AudioEnv, hub: Hub) -> None:
    audio = env.import_audio(sensitivity="normal")
    path = f"/api/v1/versions/{audio['version_id']}/content"
    whole = hub.request("GET", path)
    assert whole.status == 200 and whole.headers["content-type"] == "audio/wav"
    assert whole.headers["accept-ranges"] == "bytes"
    assert "default-src 'none'" in whole.headers["content-security-policy"]
    assert whole.headers["content-disposition"] == "inline" and len(whole.body) > 100000
    part = hub.request("GET", path, headers={"Range": "bytes=44-1043"})
    assert part.status == 206 and len(part.body) == 1000
    assert part.headers["content-range"] == f"bytes 44-1043/{len(whole.body)}"
    assert part.body == whole.body[44:1044]
    tail = hub.request("GET", path, headers={"Range": "bytes=-100"})
    assert tail.status == 206 and tail.body == whole.body[-100:]
    assert hub.request("GET", path, headers={"Range": "bytes=999999999-"}).status == 416
    assert hub.request("GET", path, headers={"Range": "garbage"}).status == 200
    # Restricted audio is the owner's: this session is the owner, so it plays; the agent
    # scope cannot reach the route at all (it is not exposed to MCP/CLI as a byte stream).


def test_provider_routes_preview_configure_run_and_report_outages(env: AudioEnv, hub: Hub) -> None:
    env.svc.secret_store = MemorySecretStore()
    transport_script: list[Any] = []

    class Scripted:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
            step = transport_script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

    env.svc.provider_transport = Scripted()
    status = hub.request("GET", "/api/v1/provider").json()
    assert status["policy"] == "no-ai" and status["route"] == "none"
    assert SECRET not in str(status)
    doc = config_doc()
    shown = hub.request("POST", "/api/v1/provider/configure/preview", {"config": doc}).json()
    assert shown["route"] == "remote" and shown["stale"] is False
    wrong = hub.request("POST", "/api/v1/provider/configure", {"config": doc, "digest": "0" * 64})
    assert wrong.status == 409
    assert hub.request("GET", "/api/v1/provider").json()["revision"] == 0
    env.svc.secret_store.set("provider-key", SECRET)
    applied = hub.request(
        "POST", "/api/v1/provider/configure", {"config": doc, "digest": shown["digest"]}
    )
    assert applied.status == 200 and applied.json()["revision"] == 1
    invalid = hub.request(
        "POST", "/api/v1/provider/configure/preview",
        {"config": config_doc(policy="local-only")},
    )  # fmt: skip
    assert invalid.status == 400

    captured = env.svc.capture_meeting(
        env.project_id, title="Supervision", text="Supervisor: Compare the controllers.\n",
        held_at=None, request_id=new_request_id(),
    )  # fmt: skip
    ask = {"purpose": "meeting_extract", "meeting_id": captured["meeting_id"]}
    preview = hub.request("POST", "/api/v1/provider/preview", ask).json()
    assert preview["would_send"] is True and preview["endpoint_host"] == "api.example.test"
    stale = hub.request("POST", "/api/v1/provider/run", ask | {"digest": "0" * 64})
    assert stale.status == 409
    transport_script.append(tp.NotSent("gaierror"))
    outage = hub.request(
        "POST", "/api/v1/provider/run", ask | {"digest": preview["digest"], "wait_seconds": 20}
    )
    # An operation run (decision 0017): the outage is the failed run's error.
    assert outage.status == 200 and outage.json()["state"] == "failed"
    assert outage.json()["external_effect"] == "confirmed"  # nothing was sent
    error = outage.json()["error"]
    assert error["code"] == "source_unavailable"
    assert error["details"]["fallback"]["model_free"] is True
    assert error["details"]["task_id"].startswith("task_")
    calls = hub.request("GET", "/api/v1/provider/calls").json()
    assert [c["state"] for c in calls["calls"]] == ["failed"]
    assert SECRET not in str(calls) and SECRET not in outage.body.decode()
    # The hub's other routes are untouched by the outage.
    assert hub.request("GET", "/api/v1/notebook").status == 200
    preview = hub.request("POST", "/api/v1/provider/preview", ask).json()
    item = {
        "purpose": "meeting_extract",
        "items": [{
            "type": "suggestion", "text": "Compare the controllers.", "proposed_owner": None,
            "due_at": None,
            "evidence": [{
                "source_id": captured["source_id"], "version_id": captured["version_id"],
                "locator": {"type": "text", "start_line": 1, "end_line": 1}, "quote_sha256": None,
            }],
        }],
    }  # fmt: skip
    transport_script.append(completion(item))
    done = hub.request(
        "POST", "/api/v1/provider/run", ask | {"digest": preview["digest"], "wait_seconds": 20}
    )
    assert done.status == 200 and done.json()["result"]["status"] == "completed"
    needs = hub.request(
        "POST", "/api/v1/provider/run",
        ask | {"digest": preview["digest"], "include_restricted": True},
    )  # fmt: skip
    assert needs.status in (403, 409)  # a different request: its own digest and confirmation
