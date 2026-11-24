"""CP18.3: PrometheOS launches, schedules and wakes no agent (A05; C18-QUEUE; decision 0035).

- Static: every place the source starts a process or opens a browser is one of a reviewed
  allowlist (system git, the project's own run worker, the pinned TeX engine, the Windows ACL
  tool for private files, and the browser the owner asks ``hub serve --open`` to open), and no
  source file names a scheduler or service registrar.
- Runtime: with every launch primitive patched to refuse, a whole work-item, event and queue
  session runs without any launch, including the MCP server's start hook.
- Manifest: the package declares only the ``prometheos`` console script and no other entry
  point, and the bundled skills declare no hooks or schedules.
"""

from __future__ import annotations

import ast
import tomllib
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER_ACT, Env
from cp7_support import create
from mcp import Client
from test_cp18_events import iso
from test_cp18_work_items import add_work_item, work_body

from prometheos import skills
from prometheos.domain.ids import new_id
from prometheos.mcp_adapter import build_server

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "prometheos"

LAUNCHERS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output", "getoutput",
                   "getstatusoutput"},
    "os": {"system", "popen", "startfile", "execv", "execve", "execvp", "execvpe", "execl",
           "execle", "execlp", "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl",
           "spawnle", "spawnlp", "spawnlpe", "posix_spawn", "posix_spawnp"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "webbrowser": {"open", "open_new", "open_new_tab", "get"},
}  # fmt: skip

# Every launch site, reviewed (module, enclosing function, call): what it starts and why it is
# not an agent, a scheduler or a service.
ALLOWED = {
    ("app/gitsync.py", "_timed", "subprocess.run"): "system git (shutil.which('git'))",
    ("app/runs.py", "spawn_worker", "subprocess.Popen"): "own run worker: python -m prometheos",
    ("app/texengine.py", "run", "subprocess.Popen"): "the pinned Tectonic engine, under limits",
    ("store/private_files.py", "restrict_to_owner", "subprocess.run"): "Windows icacls",
    ("cli.py", "hub_serve", "webbrowser.open"): "the hub URL, only when the owner passes --open",
    ("launcher.py", "_spawn", "subprocess.Popen"): (
        "the home's own hub: python -m prometheos hub serve --launched (A07, CP21; 0088)"
    ),
    ("launcher.py", "_open_paired", "webbrowser.open"): (
        "the hub URL with its launch code in the fragment, on `prometheos start` (0088)"
    ),
    ("library/extract.py", "_run_worker", "subprocess.run"): (
        "the isolated PDF text extraction (python -I extract_worker.py; CP11 S11.1, 0061 section 2)"
    ),
}
SCHEDULERS = ("schtasks", "register-scheduledtask", "new-scheduledtask", "crontab", "launchctl",
              "systemd-run", "systemctl", "at.exe", "taskschd", "win32com.client")  # fmt: skip
# Process creation through other doors (review s2-a F7): none of these may be imported or named.
FORBIDDEN_IMPORTS = {"multiprocessing", "concurrent.futures", "pty", "sched"}
FORBIDDEN_NAMES = ("shellexecute", "createprocess", "winexec", "shell32")
AGENT_EXECUTABLES = {"claude", "claude.exe", "codex", "codex.exe", "opencode", "opencode.exe",
                     "gemini", "gemini.exe", "aider", "cursor-agent", "copilot",
                     "goose"}  # fmt: skip


def _trees() -> list[tuple[str, ast.Module]]:
    return [
        (path.relative_to(SRC).as_posix(), ast.parse(path.read_text("utf-8")))
        for path in sorted(SRC.rglob("*.py"))
    ]


def _innermost(functions: list[Any], line: int) -> Any:
    owners = [f for f in functions if f.lineno <= line <= (f.end_lineno or f.lineno)]
    return min(owners, key=lambda f: (f.end_lineno or f.lineno) - f.lineno) if owners else None


def launch_sites() -> list[tuple[str, str, str, int]]:
    """(module, enclosing function, call, line) for every launch primitive called, through the
    module, an aliased module (``import subprocess as sp``) or a from-import (aliased too)."""
    found = []
    for rel, tree in _trees():
        modules: dict[str, str] = {}  # local name -> launcher module
        imported: dict[str, str] = {}  # local name -> module.attr
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in LAUNCHERS:
                        modules[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module in LAUNCHERS:
                for alias in node.names:
                    if alias.name in LAUNCHERS[node.module]:
                        imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        functions = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func, name = node.func, None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                module = modules.get(func.value.id)
                if module and func.attr in LAUNCHERS[module]:
                    name = f"{module}.{func.attr}"
            elif isinstance(func, ast.Name) and func.id in imported:
                name = imported[func.id]
            if name is not None:
                owner = _innermost(functions, node.lineno)
                found.append((rel, owner.name if owner else "<module>", name, node.lineno))
    return found


def test_every_launch_site_is_reviewed() -> None:
    sites = launch_sites()
    assert sites, "the scan finds the known launch sites"
    unknown = [s for s in sites if s[:3] not in ALLOWED]
    assert unknown == [], f"unreviewed process or browser launch: {unknown}"
    unused = set(ALLOWED) - {s[:3] for s in sites}
    assert unused == set(), f"allowlist entries no longer used: {unused}"


def test_no_other_door_to_start_a_process_or_a_timer() -> None:
    hits = []
    for rel, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hits += [f"{rel}: import {a.name}" for a in node.names
                         if a.name.split(".")[0] in FORBIDDEN_IMPORTS
                         or a.name in FORBIDDEN_IMPORTS]  # fmt: skip
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in FORBIDDEN_IMPORTS or node.module.split(".")[0] in (
                    "multiprocessing",
                    "pty",
                    "sched",
                ):
                    hits.append(f"{rel}: from {node.module}")
                if node.module == "threading" and any(a.name == "Timer" for a in node.names):
                    hits.append(f"{rel}: from threading import Timer")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr == "Timer"
                and isinstance(node.value, ast.Name)
                and node.value.id == "threading"
            ):
                hits.append(f"{rel}: threading.Timer")
        text = (SRC / rel).read_text("utf-8").lower()
        hits += [f"{rel}: {word}" for word in FORBIDDEN_NAMES if word in text]
    assert hits == []


def test_no_launch_site_names_an_agent_executable() -> None:
    """Each reviewed launch function's string literals, and every ``shutil.which`` lookup in the
    source, name no coding-agent executable (``claude-code`` and ``opencode`` elsewhere name
    skill-projection hosts, folders the owner's own agent reads)."""
    allowed_functions = {(m, f) for m, f, _ in ALLOWED}
    hits = []
    for rel, tree in _trees():
        functions = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for function in functions:
            if (rel, function.name) not in allowed_functions:
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value.lower() in AGENT_EXECUTABLES:
                        hits.append(f"{rel}:{node.lineno} {node.value}")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "which"
            ):
                args = [a.value for a in node.args if isinstance(a, ast.Constant)]
                if args != ["git"]:
                    hits.append(f"{rel}:{node.lineno} which({args})")
    assert hits == []


def test_no_source_names_a_scheduler_or_service_registrar() -> None:
    hits = []
    for path in sorted(SRC.rglob("*")):
        if path.suffix not in (".py", ".md", ".json", ".toml", ".yaml", ".yml"):
            continue
        text = path.read_text("utf-8", errors="replace").lower()
        hits += [
            f"{path.relative_to(SRC).as_posix()}: {word}" for word in SCHEDULERS if word in text
        ]
    assert hits == []


def test_the_events_queue_and_detector_modules_launch_nothing() -> None:
    for module in ("app/events.py", "app/outbox.py", "domain/events.py"):
        source = (SRC / module).read_text("utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not imports & {"subprocess", "webbrowser", "asyncio", "threading", "sched"}, module


def test_the_package_manifest_declares_no_agent_hook_or_schedule() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))["project"]
    assert project.get("scripts") == {"prometheos": "prometheos.cli:main"}
    assert "gui-scripts" not in project and "entry-points" not in project
    for name in skills.BUNDLED:
        text = (
            resources.files("prometheos")
            .joinpath(f"skills_data/{name}/SKILL.md")
            .read_text("utf-8")
        )
        meta, _ = skills.parse_frontmatter(text)
        assert not {"hooks", "schedule", "cron", "trigger", "triggers"} & set(meta), name


class Refused(Exception):
    pass


@pytest.fixture
def no_launch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch every launch primitive to record and refuse."""
    import asyncio
    import os
    import subprocess
    import webbrowser

    attempts: list[str] = []

    def refuse(name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            attempts.append(f"{name} {args[:1]}")
            raise Refused(name)

        return call

    for module, names in (
        (subprocess, LAUNCHERS["subprocess"]),
        (os, LAUNCHERS["os"]),
        (asyncio, LAUNCHERS["asyncio"]),
        (webbrowser, LAUNCHERS["webbrowser"]),
    ):
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, refuse(f"{module.__name__}.{name}"))
    return attempts


def test_a_whole_queue_session_launches_nothing(tmp_path: Path, no_launch: list[str]) -> None:
    env = Env(tmp_path)
    now = datetime.now(UTC)
    item = add_work_item(env, due_at=iso(now + timedelta(days=2)))
    suggestion = new_id("action")
    env.apply([create("action", work_body(due_at=None), suggestion)], caller=AGENT)
    env.svc.events_detect_at_start(env.project_id)  # what a starting server runs
    env.svc.jobs_tick(env.project_id)

    async def session() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            queue = await client.call_tool("work_queue", {})
            listed = await client.call_tool("event_list", {})
            ids = [e["event_id"] for e in listed.structured_content["items"]]
            asked = await client.call_tool("event_acknowledge", {"event_ids": ids[:1]})
            return queue.structured_content, ids, asked

    queue, ids, asked = anyio.run(session)
    assert item in {e["ref"] for e in queue["entries"]}
    assert asked.is_error and "approval_required" in asked.content[0].text  # type: ignore[union-attr]
    approval = next(
        a for a in env.svc.approval_list(env.project_id, AGENT, state="pending")["approvals"]
        if a["operation"] == "event_acknowledge"
    )  # fmt: skip
    env.svc.approval_decide(
        env.project_id, approval["approval_request_id"], grant=True,
        plan_sha256=approval["plan_sha256"], owner=OWNER_ACT,
    )  # fmt: skip
    env.svc.approval_resume(env.project_id, AGENT, approval["approval_request_id"])
    env.svc.event_acknowledge(env.project_id, ids[1:], authority=OWNER_ACT)
    still_open = {e["event_id"] for e in env.svc.event_list(env.project_id, AGENT)["items"]}
    assert not still_open & set(ids)  # open now: only the approval request's own events
    assert {e["type"] for e in env.svc.event_list(env.project_id, AGENT)["items"]} <= {
        "approval.requested",
        "approval.decided",
    }
    assert no_launch == []
