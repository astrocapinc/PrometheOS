"""CP6 CLI surface: the same service calls as the tests above, through real subprocesses with one
JSON document on stdout and stable exit codes (parity rule: CLI, service and MCP share validation
and authority)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from audio_support import MEETING_PLAN, SCRIPT, write_wav
from cp1_support import OWNER_ACT
from test_cp1_cli import Cli, synthetic_capability

from prometheos.audio import engines


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    research = tmp_path / "research"
    research.mkdir()
    write_wav(research / "meeting.wav", MEETING_PLAN)
    (research / "script.json").write_text(json.dumps(list(SCRIPT)), encoding="utf-8")
    runner = Cli(tmp_path)
    runner.env[engines.SYNTHETIC_ENV] = "1"
    runner.env["PROMETHEOS_SYNTHETIC_SPEED"] = "0"
    code, init, err = runner(
        "project", "init", "--title", "CLI audio", "--objective", "Undecided",
        "--root", f"research={research}",
    )  # fmt: skip
    assert code == 0, err
    runner.env["PROMETHEOS_PROJECT"] = init["project_id"]
    return runner


def ok(cli: Cli, *args: str) -> Any:
    code, out, err = cli(*args)
    assert code == 0, (args, out, err)
    return out


def test_import_transcribe_correct_and_meeting_through_the_cli(cli: Cli, tmp_path: Path) -> None:
    imported = ok(cli, "audio", "import", "meeting.wav", "--root", "research",
                  "--sensitivity", "normal")  # fmt: skip
    assert imported["audio"]["duration_ms"] == 8000
    version = imported["version_id"]
    run = ok(
        cli, "audio", "transcribe", version, "--engine", "synthetic",
        "--script", str(tmp_path / "research" / "script.json"),
        "--chunk-seconds", "4", "--overlap-seconds", "2",
    )  # fmt: skip
    assert run["state"] == "completed" and run["chunk_count"] == 3
    transcript = run["result_version_id"]
    shown = ok(cli, "audio", "transcript", "show", transcript)
    assert len(shown["payload"]["segments"]) == 3 and shown["audio_version_id"] == version
    clip = tmp_path / "clip.wav"
    span = ok(cli, "audio", "span", transcript, "--start-ms", "4000", "--end-ms", "6000",
              "--clip", str(clip))  # fmt: skip
    assert span["quote"] == "Send the constrained result." and clip.is_file()
    code, again, _ = cli("audio", "span", transcript, "--start-ms", "4000", "--end-ms", "6000",
                         "--clip", str(clip))  # fmt: skip
    assert code == 2 and again["error"]["code"] == "validation_error"  # never overwrites
    fixed = ok(cli, "audio", "transcript", "correct", transcript, "--segment", "seg-00002",
               "--text", "Send the constrained-plant result.")  # fmt: skip
    assert fixed["previous_version_id"] == transcript and fixed["segments_changed"] == 1
    meeting = ok(cli, "meeting", "from-audio", "--transcript", fixed["version_id"], "--audio",
                 version, "--title", "Call", "--held-at", "2026-10-01T09:00:00Z")  # fmt: skip
    assert meeting["meeting_id"].startswith("meeting_") and meeting["sensitivity"] == "normal"
    reconciled = ok(cli, "meeting", "reconcile", "--meeting", meeting["meeting_id"])
    assert reconciled["items"] == []
    runs = ok(cli, "audio", "runs", "--version", version)
    assert [r["state"] for r in runs["runs"]] == ["completed"]


def test_start_only_status_and_cancel(cli: Cli, tmp_path: Path) -> None:
    imported = ok(cli, "audio", "import", "meeting.wav", "--root", "research",
                  "--sensitivity", "normal")  # fmt: skip
    started = ok(cli, "audio", "transcribe", imported["version_id"], "--engine", "synthetic",
                 "--start-only")  # fmt: skip
    assert started["state"] == "queued"  # operation run states (CP10.3, decision 0017)
    status = ok(cli, "audio", "run-status", started["run_id"])
    assert status["chunks_done"] == 0
    cancelled = ok(cli, "audio", "run-cancel", started["run_id"])
    assert cancelled["outcome"] == "cancelled"
    code, out, _ = cli("audio", "transcribe", imported["version_id"], "--resume", started["run_id"])
    assert code == 2 and out["error"]["code"] == "validation_error"


def test_restricted_audio_needs_owner_scope(cli: Cli, tmp_path: Path) -> None:
    imported = ok(cli, "audio", "import", "meeting.wav", "--root", "research")  # default restricted
    assert imported["sensitivity"] == "restricted"
    code, out, _ = cli("audio", "transcribe", imported["version_id"], "--engine", "synthetic")
    assert code == 2 and out["error"]["code"] == "not_found"  # the agent cannot see it
    secret = synthetic_capability(tmp_path, cli.env["PROMETHEOS_PROJECT"], "read_sensitive")
    cap = tmp_path / "cap.txt"
    cap.write_text(secret, encoding="utf-8")
    run = ok(cli, "audio", "transcribe", imported["version_id"], "--engine", "synthetic",
             "--scope", "owner", "--capability-file", str(cap))  # fmt: skip
    assert run["state"] == "completed"
    code, hidden, _ = cli("audio", "transcript", "show", run["result_version_id"])
    assert code == 2 and hidden["error"]["code"] == "not_found"


def test_model_commands_show_the_plan_and_refuse_a_wrong_digest_without_downloading(
    cli: Cli,
    tmp_path: Path,
) -> None:
    empty_models = tmp_path / "empty-synthetic-models"
    empty_models.mkdir()
    cli.env["PROMETHEOS_MODELS_DIR"] = str(empty_models)
    plan = ok(cli, "audio", "model", "plan")
    assert plan["license"] == "MIT" and plan["total_mib"] > 140
    assert plan["revision"] == "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
    assert plan["installed"] is False and "faster-whisper-base" in plan["location"]
    code, out, _ = cli("audio", "model", "install", "--plan-sha256", "0" * 64)
    assert code == 3 and out["error"]["code"] == "revision_conflict"
    status = ok(cli, "audio", "model", "status")
    assert status["models"][0]["installed"] is False
    code, out, _ = cli("audio", "transcribe", "version_00000000-0000-4000-8000-000000000000")
    assert code in (2, 3)  # unknown version or missing model; never a download


def test_recording_through_the_cli_needs_permission_and_is_visible(cli: Cli) -> None:
    devices = ok(cli, "audio", "devices", "--backend", "synthetic")
    assert {d["id"] for d in devices["devices"]} == {"synthetic-mic", "synthetic-loopback"}
    # CP10.2 (decision 0016): consent is the owner's act at the computer. Without a terminal
    # (an agent) the start is an approval request, even with the permission flag; nothing
    # records until the owner grants it and the request is resumed.
    code, out, _ = cli(
        "audio", "record", "start", "--device", "synthetic-mic", "--device", "synthetic-loopback",
        "--title", "Online meeting", "--backend", "synthetic", "--max-seconds", "1",
        "--i-have-permission", "--sensitivity", "normal",
    )  # fmt: skip
    assert code == 4 and out["error"]["code"] == "approval_required"
    approval_id = out["error"]["details"]["approval_request_id"]
    assert ok(cli, "audio", "record", "list", "--all")["sessions"] == []
    from prometheos.app.service import Service

    svc = Service(home=Path(cli.env["PROMETHEOS_HOME"]))
    pid = cli.env["PROMETHEOS_PROJECT"]
    shown = svc.approval_owner_view(pid, approval_id)
    svc.approval_decide(
        pid, approval_id, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )
    # Decision 0016 round 2 (review F1): an agent's resume never captures; the owner starts the
    # granted recording where it is visible (the hub, or an interactive terminal).
    code, refused_resume, _ = cli("approval", "resume", approval_id, "--backend", "synthetic")
    assert code == 4 and refused_resume["error"]["code"] == "policy_denied"
    from prometheos.audio import capture

    backend = capture.SyntheticBackend()  # the test process stands in for the hub
    started = svc.approval_start_recording(
        pid, approval_id, owner=OWNER_ACT, plan_sha256=shown["plan_sha256"], backend=backend,
        start_capture=False,
    )  # fmt: skip
    session = started["result"]
    final = svc.recording_run(pid, session["session_id"], backend, max_seconds=1)
    assert final["state"] == "imported" and final["duration_ms"] >= 1000
    listing = ok(cli, "audio", "record", "list", "--all")
    assert [s["state"] for s in listing["sessions"]] == ["imported"]
    assert (
        "reminder" in json.dumps(listing["consent_reminder"]).lower() or listing["consent_reminder"]
    )
    status = ok(cli, "audio", "record", "status", final["session_id"])
    assert status["version_id"] == final["version_id"]
    code, again, _ = cli("audio", "record", "stop", final["session_id"])
    assert code == 2 and again["error"]["code"] == "validation_error"


def test_help_needs_no_project_or_extra(tmp_path: Path) -> None:
    runner = Cli(tmp_path)
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "prometheos", "audio", "--help"],
        env=runner.env, capture_output=True, timeout=60,
    )  # fmt: skip
    help_text = done.stdout.decode("utf-8", "replace")
    assert done.returncode == 0 and "transcribe" in help_text and "record" in help_text
    assert not (tmp_path / "home").exists()
