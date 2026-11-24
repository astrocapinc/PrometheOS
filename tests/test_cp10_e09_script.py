"""E09 session script (tools/live_host_e09.py): prepared, never run by the implementer.

These tests start no host and no hub. They check what can be checked without one: the script
refuses to start a session without the owner's approval and presence, its prompts ask for what
E09 requires and carry no ID in prose, the disposable project and the project-local host setup it
prepares are right (a deterministic revision conflict, nothing global written), and its
verification reports facts about stored state and transcripts instead of passing vacuously.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]


def load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "live_host_e09", REPO / "tools" / "live_host_e09.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_host_e09"] = module
    spec.loader.exec_module(module)
    return module


e09 = load()
ID_IN_PROSE = re.compile(r"(?:apreq|oprun|claim|entry|execution|version|source)_[0-9a-f]{8}")


def test_the_script_refuses_without_the_owners_approval_and_presence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "e09"
    assert e09.main(["--evidence", str(out)]) == 2
    assert e09.main(["--evidence", str(out), "--i-have-owner-approval"]) == 2
    assert not out.exists()  # nothing was created, nothing was started
    err = capsys.readouterr().err
    assert "--i-have-owner-approval" in err and "--owner-is-present" in err


def test_the_prompts_ask_for_what_e09_requires_and_carry_no_id() -> None:
    first, second, third = e09.PROMPTS
    for needed in ("context_get", "version_text", "base_rev 1", "revision conflict",
                   "source_import_plan", "correction_apply", "approval_required"):  # fmt: skip
        assert needed in first, needed
    assert "Do not try to grant it" in first and "then stop" in first
    for needed in ("now_get", "approval_list", "exactly once"):
        assert needed in second, needed
    for needed in ("run_list", "run_get", "run_resume", "Change no research records"):
        assert needed in third, needed
    # A fresh session finds its work itself: no run or approval ID is handed over in prose.
    for prompt in e09.PROMPTS:
        assert not ID_IN_PROSE.search(prompt)
    assert "Fresh session" in second and "Fresh session" in third


def test_prepare_builds_the_project_and_writes_only_project_local_setup(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    prepared = e09.prepare(tmp_path / "work", evidence)
    project = prepared["project"]
    from cp1_support import OWNER

    seeded = prepared["seeded"]
    entry = seeded.s.svc.get(project["project_id"], OWNER, project["conflict_entry"], None)
    assert entry["rev"] == 2  # the stale base_rev of 1 in the prompt is a certain conflict
    assert entry["body"]["title"] == e09.CONFLICT_TITLE
    config = json.loads((evidence / "mcp-config.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["prometheos"]
    assert server["args"][-2:] == ["--project", project["project_id"]]
    assert server["env"]["PROMETHEOS_HOME"] == project["home"]
    setup = json.loads((evidence / "host-setup.json").read_text(encoding="utf-8"))
    assert setup["scope"].startswith("project-local only")
    assert (prepared["cwd"] / ".mcp.json").is_file()  # next to the project, never global
    assert (evidence / "checklist.md").is_file() and (evidence / "prompt-3.txt").is_file()
    # The hub is not running and no session started: nothing but files exists.
    assert (
        not (evidence / "session-1.jsonl").exists() and not (evidence / "hub-window.json").exists()
    )


def test_verification_reports_facts_and_does_not_pass_a_session_that_did_not_happen(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    prepared = e09.prepare(tmp_path / "work", evidence)
    checks = e09.verify(prepared, evidence, None)
    assert checks["context_read"] is False and checks["evidence_retrieved"] is False
    assert checks["revision_conflict_seen"] is False
    assert checks["conflict_entry_revised_after_conflict"] is False
    assert checks["approval_used_once"] is False and checks["assumption_superseded"] is False
    assert checks["chat_2_resumed"] is False and checks["chat_3_read_the_run"] is False
    assert checks["tool_calls"] == 0
    assert (evidence / "verification.json").is_file()


def test_the_tool_call_parser_reads_a_host_stream(tmp_path: Path) -> None:
    line = json.dumps(
        {
            "message": {
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "name": "mcp__prometheos__context_get", "input": {}},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ]
            }
        }
    )
    (tmp_path / "session-2.jsonl").write_text(line + "\nnot json at all\n", encoding="utf-8")
    assert e09.tool_calls(tmp_path) == [{"session": 2, "tool": "context_get", "input": {}}]


def test_only_the_grant_step_may_start_the_hub_and_only_real_sessions_start_a_host() -> None:
    source = (REPO / "tools" / "live_host_e09.py").read_text(encoding="utf-8")
    assert source.count('"hub", "serve"') == 1  # the one grant window
    assert source.count('"claude"') == 2  # --version and the session command
    assert "--strict-mcp-config" in source and "--no-session-persistence" in source
    assert "never marks it passed" in source  # an external gate: a person decides


def test_version_evidence_requires_successful_linked_bytes(tmp_path: Path) -> None:
    call = {
        "type": "tool_use",
        "id": "read-1",
        "name": "mcp__prometheos__record_read",
        "input": {"request": {"op": "version_text", "version_id": "version-test"}},
    }
    result = {
        "type": "tool_result",
        "tool_use_id": "read-1",
        "content": [{"text": json.dumps({"result": {"text": "synthetic cited line"}})}],
    }
    path = tmp_path / "session-1.jsonl"

    def write(parts: Any) -> None:
        path.write_text(json.dumps({"message": {"content": parts}}), encoding="utf-8")

    write([call])
    assert e09.retrieved_version_bytes(tmp_path) is False
    write([call, result | {"is_error": True}])
    assert e09.retrieved_version_bytes(tmp_path) is False
    write([call, result])
    assert e09.retrieved_version_bytes(tmp_path) is True
