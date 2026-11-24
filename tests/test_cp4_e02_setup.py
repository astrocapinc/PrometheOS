"""The E02 live-host harness prepares a project-local OpenCode setup (no host is started)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import live_host_e02

ROOT = Path(__file__).resolve().parents[1]


def test_e02_prepare_is_project_local(tmp_path: Path, monkeypatch: object) -> None:
    prepared = live_host_e02.prepare(tmp_path)
    config = json.loads((tmp_path / "opencode.json").read_text("utf-8"))
    server = config["mcp"]["prometheos"]
    assert server["type"] == "local" and server["command"][-1] == prepared["project"]["project_id"]
    assert server["environment"]["PROMETHEOS_HOME"] == str(tmp_path / "home")
    assert config["permission"] == {"edit": "deny", "bash": "deny", "webfetch": "deny"}
    skill = tmp_path / ".opencode" / "skills" / "inspect-claim-evidence" / "SKILL.md"
    assert skill.is_file()
    generated = prepared["generated_by_host_setup"]["mcp"]["prometheos"]
    assert generated["type"] == "local" and generated["command"][:3] == [
        "prometheos",
        "mcp",
        "serve",
    ]
    assert server["command"][1:] == ["-m", *generated["command"][1:]]  # same arguments


def test_e02_refuses_to_run_without_owner_approval_flag(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "live_host_e02.py"),
            "--evidence",
            str(tmp_path / "e"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2 and "--i-have-owner-approval" in proc.stderr
    assert not (tmp_path / "e").exists()
