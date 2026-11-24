"""CP21.1 (A07; decision 0090): `prometheos status` reports a stale agent package (C21-LAUNCH).

The owner's setup command records where it wrote the Claude Code plugin; `status` then compares
that package with this installation (the interpreter it runs, the version that wrote it, the
bundled skills) and names the setup command that writes it again. `status` itself never writes
the package: only the owner's setup command regenerates it (spec/02).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from cp1_support import Env

from prometheos.agent_package import PLUGIN, package_health, record_setup


def _cli(env: Env, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "prometheos", *args],
        env={**os.environ, "PROMETHEOS_HOME": str(env.home), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


def _tree(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def _setup(env: Env, target: Path) -> None:
    target.mkdir(exist_ok=True)
    done = _cli(env, "host", "setup", "--host", "claude-code-plugin", "--target", str(target),
                "--project", env.project_id, "--apply", "--json")  # fmt: skip
    assert done.returncode == 0, done.stderr


def _edit_json(path: Path, change: Any) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def test_status_reports_a_current_package_and_writes_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    target = tmp_path / "marketplace"
    _setup(env, target)
    before = _tree(target)
    done = _cli(env, "status", "--json")
    assert done.returncode == 0, done.stderr
    [package] = json.loads(done.stdout)["agent_packages"]
    assert package["state"] == "current" and package["reasons"] == []
    assert package["target"] == str(target.resolve())
    assert "host setup --host claude-code-plugin" in package["setup_command"]
    assert _tree(target) == before


def test_a_moved_interpreter_an_old_version_and_changed_skills_are_each_named(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    target = tmp_path / "marketplace"
    _setup(env, target)
    folder = target / PLUGIN
    assert [p["state"] for p in package_health(env.home)] == ["current"]

    gone = (tmp_path / "old-venv" / "python.exe").as_posix()
    _edit_json(folder / ".mcp.json", lambda d: d["mcpServers"][PLUGIN].update(command=gone))
    [package] = package_health(env.home)
    assert package["state"] == "stale"
    assert package["reasons"] == ["it runs an interpreter that no longer exists"]

    _edit_json(folder / ".mcp.json", lambda d: d["mcpServers"][PLUGIN].update(command=__file__))
    assert package_health(env.home)[0]["reasons"] == ["it runs a different PrometheOS installation"]

    _edit_json(folder / ".claude-plugin" / "plugin.json", lambda d: d.update(version="0.0.1"))
    skill = next((folder / "skills").glob("*/SKILL.md"))
    skill.write_text(skill.read_text(encoding="utf-8") + "\nold text\n", encoding="utf-8")
    [package] = package_health(env.home)
    assert package["state"] == "stale" and len(package["reasons"]) == 3
    assert package["reasons"][1].startswith("it was written by PrometheOS 0.0.1")
    assert package["reasons"][2].endswith(skill.parent.name)


def test_a_removed_package_is_missing_and_setup_again_makes_it_current(tmp_path: Path) -> None:
    env = Env(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    _setup(env, one)
    _setup(env, two)
    for path in sorted((two / PLUGIN).rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    states = {Path(p["target"]).name: p["state"] for p in package_health(env.home)}
    assert states == {"one": "current", "two": "missing"}
    _setup(env, two)
    assert [p["state"] for p in package_health(env.home)] == ["current", "current"]


def test_recording_the_same_folder_twice_keeps_one_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    record_setup(home, tmp_path / "x", "project_a")
    record_setup(home, tmp_path / "x", "project_b")
    assert package_health(home) == [
        {
            "target": str(tmp_path / "x"),
            "project_id": "project_b",
            "state": "missing",
            "reasons": [],
            "setup_command": package_health(home)[0]["setup_command"],
        }
    ]


def test_a_damaged_record_reads_as_no_packages(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "launcher").mkdir(parents=True)
    (home / "launcher" / "agent-packages.json").write_text("{not json", encoding="utf-8")
    assert package_health(home) == []
