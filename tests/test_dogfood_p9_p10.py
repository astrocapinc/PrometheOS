"""Dogfood P9 and P10 (KERS papers session, 2026-10-19).

P9: the plugin's MCP server and hook ran the bundled core without ``-I``, so the user's own
site-packages (an older ``mcp`` installed by another tool) shadowed PrometheOS's and the server
died at import. P10: ``--json`` output written to a pipe used the Windows ANSI code page, so an
em dash arrived as byte 0x97 and the document was not UTF-8.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from prometheos import agent_package, cli, skills
from prometheos.app import runs
from prometheos.domain.models import PrometheosError
from prometheos.interpreter import CHILD_FLAGS, PYTHON_FLAGS

PROJECT = "project_00000000-0000-4000-8000-000000000009"


def _base_python() -> str:
    """The interpreter behind the test venv: a plain CPython, like the bundled core, so its
    user site is live (a venv disables it)."""
    return str(getattr(sys, "_base_executable", sys.executable))


def test_the_flags_keep_a_shadowing_user_site_out(tmp_path: Path) -> None:
    base = _base_python()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env["PYTHONUSERBASE"] = str(tmp_path / "userbase")
    where = subprocess.run(
        [base, "-c", "import site; print(site.ENABLE_USER_SITE, site.getusersitepackages())"],
        env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    enabled, user_site = where.stdout.strip().split(" ", 1)
    if enabled != "True":
        pytest.skip("this interpreter has no user site to shadow anything")
    Path(user_site).mkdir(parents=True)
    (Path(user_site) / "p9_shadow_probe.py").write_text("SHADOW = True\n", "utf-8")
    probe = "import p9_shadow_probe; print('shadowed')"
    # Control: without the flags the user site is on the path, as it was for the plugin.
    shadowed = subprocess.run([base, "-c", probe], env=env, capture_output=True, text=True)
    assert shadowed.returncode == 0 and shadowed.stdout.strip() == "shadowed"
    isolated = subprocess.run([base, *PYTHON_FLAGS, "-c", probe], env=env, capture_output=True,
                              text=True)  # fmt: skip
    assert isolated.returncode != 0 and "ModuleNotFoundError" in isolated.stderr


def test_the_plugin_server_and_hook_start_the_core_isolated() -> None:
    files = agent_package.plugin_files(PROJECT, {})
    server = json.loads(files["prometheos/.mcp.json"])["mcpServers"]["prometheos"]
    assert server["command"] == Path(sys.executable).as_posix()
    assert server["args"][: len(PYTHON_FLAGS) + 2] == [*PYTHON_FLAGS, "-m", "prometheos"]
    hooks = json.loads(files["prometheos/hooks/hooks.json"])["hooks"]["SessionStart"]
    argv = shlex.split(hooks[0]["hooks"][0]["command"])
    assert argv[: len(PYTHON_FLAGS) + 3] == [
        Path(sys.executable).as_posix(),
        *PYTHON_FLAGS,
        "-m",
        "prometheos",
    ]


def test_the_run_worker_starts_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    class FakeProcess:
        pid = 4242

    def fake_popen(argv: list[str], **_: Any) -> FakeProcess:
        seen.append(argv)
        return FakeProcess()

    monkeypatch.setattr(runs.subprocess, "Popen", fake_popen)
    assert runs.spawn_worker(tmp_path, PROJECT, "oprun_x") == 4242
    (argv,) = seen
    # The user site stays out; PYTHONPATH is kept, so the worker is the parent's PrometheOS.
    assert argv[: len(CHILD_FLAGS) + 3] == [sys.executable, *CHILD_FLAGS, "-m", "prometheos"]
    assert "-s" in CHILD_FLAGS and "-I" not in CHILD_FLAGS


def _pipe() -> tuple[io.BytesIO, io.TextIOWrapper]:
    buffer = io.BytesIO()
    return buffer, io.TextIOWrapper(buffer, encoding="cp1252", write_through=True)


def test_json_written_to_a_pipe_is_utf8_whatever_the_code_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {"title": "Kinematic limits — ω and χ_brake"}
    out, stdout = _pipe()  # not a terminal
    err, stderr = _pipe()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(cli, "app", lambda: (cli._emit(document, True), sys.stderr.write("ω\n")))
    cli.main()
    assert json.loads(out.getvalue().decode("utf-8")) == document
    assert err.getvalue().decode("utf-8").rstrip() == "ω"  # the line ending is the platform's


def test_main_runs_without_standard_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """pythonw and detached starts have no stdout/stderr, or a closed one (review F3)."""
    closed = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    closed.close()
    ran: list[bool] = []
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", closed)
    monkeypatch.setattr(cli, "app", lambda: ran.append(True))
    cli.main()
    assert ran == [True]


def _apply(target: Path) -> dict[str, Any]:
    plan = skills.host_plan("claude-code-plugin", target, PROJECT)
    skills.host_apply(plan)
    return plan


def test_setup_upgrades_its_own_files_and_refuses_the_owners_edits(tmp_path: Path) -> None:
    """Review kers-p9-a F1: a new PrometheOS could not rewrite a package an earlier one wrote
    (P9's command, or any bundled skill text that changed), so the fix never reached an
    installed plugin. The setup manifest records what setup wrote; exactly those files are
    upgraded, and a file the owner changed is still refused."""
    target = tmp_path / "agent"
    target.mkdir()
    _apply(target)
    manifest_path = target / "prometheos" / skills.SETUP_MANIFEST
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert manifest["kind"] == skills.SETUP_MANIFEST_KIND
    mcp = target / "prometheos" / ".mcp.json"
    assert manifest["files"]["prometheos/.mcp.json"] == skills._sha256(mcp.read_text("utf-8"))
    # An earlier PrometheOS wrote a different command and recorded it.
    old = mcp.read_text("utf-8").replace('"-I",', "")
    mcp.write_text(old, "utf-8", newline="\n")
    manifest["files"]["prometheos/.mcp.json"] = skills._sha256(old)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", "utf-8", newline="\n")
    plan = _apply(target)
    actions = {Path(f["path"]).name: f["action"] for f in plan["files"]}
    assert actions[".mcp.json"] == "upgrade"
    assert '"-I"' in mcp.read_text("utf-8")
    # The owner edits a skill: setup refuses rather than overwrite it.
    skill = target / "prometheos" / "skills" / "work-items" / "SKILL.md"
    skill.write_text(skill.read_text("utf-8") + "\nmy note\n", "utf-8", newline="\n")
    with pytest.raises(PrometheosError) as refused:
        skills.host_plan("claude-code-plugin", target, PROJECT)
    assert refused.value.code == "revision_conflict" and "delete the file" in refused.value.message


def test_a_package_without_a_manifest_is_never_overwritten(tmp_path: Path) -> None:
    """The owner's case: a package from before the manifest. Setup refuses and names the whole
    folder, since no file in it can be told apart from an edit (review R2-3)."""
    target = tmp_path / "agent"
    (target / "prometheos").mkdir(parents=True)
    (target / "prometheos" / ".mcp.json").write_text("{}\n", "utf-8")
    with pytest.raises(PrometheosError) as refused:
        skills.host_plan("claude-code-plugin", target, PROJECT)
    assert refused.value.code == "revision_conflict"
    assert "delete that folder" in refused.value.message
    assert refused.value.details["folder"] == str(target / "prometheos")


@pytest.mark.parametrize(
    "text",
    ["[]\n", '"x"\n', "[" * 100_000, "{not json", '{"kind": 1}'],
    ids=["list", "string", "deep", "syntax", "wrong-kind"],
)
def test_an_invalid_manifest_is_no_manifest_and_never_a_crash(tmp_path: Path, text: str) -> None:
    """Review R2-2: a corrupt manifest is not trusted, and setup refuses instead of crashing."""
    target = tmp_path / "agent"
    target.mkdir()
    _apply(target)
    manifest_path = target / "prometheos" / skills.SETUP_MANIFEST
    manifest_path.write_text(text, "utf-8")
    mcp = target / "prometheos" / ".mcp.json"
    mcp.write_text(mcp.read_text("utf-8") + " ", "utf-8")  # differs from what setup writes
    with pytest.raises(PrometheosError) as refused:
        skills.host_plan("claude-code-plugin", target, PROJECT)
    assert refused.value.code == "revision_conflict"


def test_a_valid_manifest_with_no_files_still_lets_setup_run(tmp_path: Path) -> None:
    target = tmp_path / "agent"
    target.mkdir()
    _apply(target)
    manifest_path = target / "prometheos" / skills.SETUP_MANIFEST
    manifest_path.write_text(json.dumps({"kind": skills.SETUP_MANIFEST_KIND, "files": {}}), "utf-8")
    plan = _apply(target)
    actions = {Path(f["path"]).name: f["action"] for f in plan["files"]}
    assert actions[skills.SETUP_MANIFEST] == "upgrade"
    assert json.loads(manifest_path.read_text("utf-8"))["files"]


def test_a_child_of_an_isolated_process_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review R2-4: a child never sees more of the environment than its parent did."""
    from types import SimpleNamespace

    from prometheos import interpreter

    monkeypatch.setattr(interpreter, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=1)))
    assert interpreter.child_flags() == PYTHON_FLAGS
    monkeypatch.setattr(interpreter, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=0)))
    assert interpreter.child_flags() == CHILD_FLAGS
