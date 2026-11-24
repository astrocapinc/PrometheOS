"""C19-WALK (CP19.4, decision 0055): the walk harness (tools/walk_cp19.py) and its guards.

The full walk (the owner in the built desktop app with the agent over the real MCP server, then a
second fresh store through MCP alone) is the checkpoint suite's ``walk`` command: it shows a
window and presses a global hotkey, so it never runs inside the parallel test suite. Here the MCP
leg runs for real (the official SDK client against ``prometheos mcp serve --stdio``), and the
harness' own judgement is mutation-tested: a store divergence, a missing step, a different
outcome, a different agent answer, a failed or empty assertion, every window observation that
would be wrong, and a built app older than the checkout each fail the walk.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "desktop" / "app" / "walk" / "walk19.spec.ts"


def load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("walk_cp19", REPO / "tools" / "walk_cp19.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["walk_cp19"] = module
    spec.loader.exec_module(module)
    return module


walk = load_tool()


def test_the_mcp_leg_runs_the_whole_script_through_the_official_client(tmp_path: Path) -> None:
    out = tmp_path / "walk"
    assert walk.main(["--out", str(out), "--only", "mcp"]) == 0
    doc = json.loads((out / "transcript.json").read_text(encoding="utf-8"))
    assert doc["failures"] == []
    steps = {s["id"]: s for s in doc["mcp"]["steps"]}
    assert list(steps) == walk.SCRIPT_IDS
    for step in steps.values():
        assert step["store_assertions"] and all(a["passed"] for a in step["store_assertions"])
    assert {steps[i]["via"] for i in walk.DESKTOP_OWNER_IDS} == {"owner-service"}
    assert {steps[i]["via"] for i in walk.AGENT_IDS} == {"mcp"}
    assert steps["W3.2"]["outcome"] == "error:approval_required"
    assert steps["W3.3"]["outcome"].startswith("error:")  # the boundary control was refused
    outcome = json.loads((out / "outcome-mcp.json").read_text(encoding="utf-8"))
    assert [e["title"] for e in outcome["entries"]] == [walk.NOTE_TITLE]
    assert [(w["label"], w["status"]) for w in outcome["work_items"]] == [
        ("suggested_item", "proposed")
    ]
    assert outcome["approvals"] == [["event_acknowledge", "used", 0]]
    events = {e["type"]: e for e in outcome["events"]}
    assert set(events) == {"work_item.created", "approval.requested", "approval.decided"}
    assert events["work_item.created"]["acknowledged"] is True
    assert events["work_item.created"]["note"] == walk.ACK_NOTE
    # Relations, flags and authority results are part of the compared outcome.
    authority = outcome["authority"]
    assert [a["state"] for a in authority["approval_requests"]] == ["used"]
    assert authority["approval_requests"][0]["decided_by"] == "hub-owner"
    assert [(c["max_uses"], c["uses"]) for c in authority["capabilities"]] == [(1, 1)]
    assert {"relations", "refs", "view_flags", "audit"} <= set(outcome)


# ----- the harness' judgement bites (mutation-style negatives) ------------------------------


def legs() -> tuple[dict[str, Any], dict[str, Any]]:
    steps = [
        {"id": i, "outcome": "ok", "normalised_response_sha256": "a" * 64} for i in walk.SCRIPT_IDS
    ]
    doc = {"desktop": {"steps": copy.deepcopy(steps)}, "mcp": {"steps": copy.deepcopy(steps)}}
    outcomes = {
        "desktop": {
            "entries": [{"label": "captured_note", "title": walk.NOTE_TITLE}],
            "approvals": [["event_acknowledge", "used", 0]],
        }
    }
    outcomes["mcp"] = copy.deepcopy(outcomes["desktop"])
    return doc, outcomes


def test_two_equivalent_legs_are_judged_equal() -> None:
    doc, outcomes = legs()
    equivalence, failures = walk.judge(doc, outcomes)
    assert failures == [] and equivalence["stored outcomes"] == "equal"


def test_a_store_divergence_fails_the_walk() -> None:
    doc, outcomes = legs()
    outcomes["mcp"]["approvals"][0][1] = "granted"  # granted but never resumed on one leg
    _, failures = walk.judge(doc, outcomes)
    assert any("stored outcomes differ" in f and "approvals" in f for f in failures)
    doc, outcomes = legs()
    outcomes["desktop"]["entries"].append({"label": "<other>", "title": walk.NOTE_TITLE})
    assert walk.judge(doc, outcomes)[1]  # a note saved twice on one leg


@pytest.mark.parametrize("step_id", ["W3.2", "W6.1"])
def test_a_different_step_outcome_fails_the_walk(step_id: str) -> None:
    doc, outcomes = legs()
    next(s for s in doc["mcp"]["steps"] if s["id"] == step_id)["outcome"] = "error:forbidden"
    _, failures = walk.judge(doc, outcomes)
    assert any("step outcomes differ" in f and step_id in f for f in failures)


def test_a_missing_step_fails_the_walk() -> None:
    doc, outcomes = legs()
    doc["desktop"]["steps"] = [s for s in doc["desktop"]["steps"] if s["id"] != "W4.2"]
    _, failures = walk.judge(doc, outcomes)
    assert any("steps missing" in f and "W4.2" in f for f in failures)


@pytest.mark.parametrize("step_id", ["W3.1", "W3.2", "W3.3", "W5.1", "W6.2"])
def test_a_different_agent_answer_fails_the_walk(step_id: str) -> None:
    doc, outcomes = legs()
    step = next(s for s in doc["desktop"]["steps"] if s["id"] == step_id)
    step["normalised_response_sha256"] = "b" * 64
    _, failures = walk.judge(doc, outcomes)
    assert any("agent answers differ" in f and step_id in f for f in failures)


def test_an_owner_steps_own_answer_is_not_compared_across_legs() -> None:
    """The window's observations and the service's answers differ by nature; their store
    assertions and outcomes are what must hold."""
    doc, outcomes = legs()
    next(s for s in doc["desktop"]["steps"] if s["id"] == "W4.1")["normalised_response_sha256"] = (
        "c" * 64
    )
    assert walk.judge(doc, outcomes)[1] == []


def test_a_failed_store_assertion_or_an_empty_step_fails_the_walk(tmp_path: Path) -> None:
    class Store:
        names: dict[str, str] = {}  # noqa: RUF012

        def norm(self, value: Any) -> Any:
            return value

    recorder = walk.Recorder("A", "desktop", Store(), tmp_path / "evidence")
    recorder.add("W5.1", "mcp", {}, {}, "ok", [walk.check("the request ran once", False)])
    assert recorder.failures and "W5.1" in recorder.failures[0]
    recorder = walk.Recorder("A", "desktop", Store(), tmp_path / "evidence2")
    recorder.add("W6.2", "mcp", {}, {}, "ok", [])
    assert recorder.failures and "W6.2" in recorder.failures[0]


GOOD = {
    "W1.1": {"hidden_at_start": True, "tray": True, "visible_after_tray_click": True,
             "paired": True, "url": "http://127.0.0.1:51234/",
             "probe_main_load": "net::ERR_BLOCKED_BY_CLIENT (-20) loading 'http://127.0.0.1:5/'",
             "probe_navigation_stayed": True, "probe_hits": 0, "hooks_without_switch": "undefined",
             "page_globals_without_switch": "undefined undefined undefined undefined",
             "hooks_with_switch": "function"},
    "W2.1": {"hotkey_fired": 1, "saved": True},
    "W4.1": {"notifications": [f"{walk.SUGGESTION}: An agent proposed a work item.",
                               f"{walk.APPROVAL_NOTICE}; the researcher decides it."]},
    "W4.2": {"granted_in_window": True, "plan_sha256": "a" * 64, "plan_digest_shown": True},
    "W6.1": {"last_stop": {"graceful": True, "code": 0}, "bounds_kept": True,
             "notifications_before_quit": [f"{walk.APPROVAL_NOTICE}."],
             "notifications_after_restart": []},
}  # fmt: skip

BAD = [
    ("W1.1", {"hidden_at_start": False}),  # the window showed before the tray click
    ("W1.1", {"paired": False}),
    ("W1.1", {"url": "http://192.0.2.1:51234/"}),  # not loopback
    ("W1.1", {"probe_main_load": "loaded"}),  # the main process reached another origin
    ("W1.1", {"probe_hits": 1}),  # the probe server saw a request
    ("W1.1", {"probe_navigation_stayed": False}),  # the page navigated away from the hub
    ("W1.1", {"hooks_without_switch": "object"}),  # the shipped app exposes its test hooks
    ("W1.1", {"page_globals_without_switch": "undefined function undefined undefined"}),
    ("W1.1", {"hooks_with_switch": "undefined"}),  # the walk did not drive the hooked build
    ("W2.1", {"hotkey_fired": 0}),  # capture opened some other way
    ("W2.1", {"saved": False}),
    ("W4.1", {"notifications": [f"{walk.SUGGESTION}: An agent proposed a work item."]}),
    ("W4.1", {"notifications": [f"{walk.SUGGESTION}: x", f"{walk.SUGGESTION}: x",
                                f"{walk.APPROVAL_NOTICE}."]}),  # announced twice
    ("W4.2", {"granted_in_window": False}),
    ("W4.2", {"plan_digest_shown": False}),  # granted without seeing the exact plan
    ("W6.1", {"last_stop": {"graceful": False, "code": 1}}),  # the core was killed
    ("W6.1", {"bounds_kept": False}),
    ("W6.1", {"notifications_after_restart": [f"{walk.APPROVAL_NOTICE}."]}),  # announced again
]  # fmt: skip


TOOLS = ["approval_get", "approval_list", "approval_resume", "approval_withdraw", "apply"]


def test_the_boundary_control_passes_only_when_the_agent_is_refused() -> None:
    assert all(
        c["passed"] for c in walk.boundary_checks("error:approval_required", True, True, TOOLS)
    )
    for bad in (
        ("ok", True, True, TOOLS),  # the request ran before the owner decided
        ("error:approval_required", False, True, TOOLS),  # the refusal changed the store
        ("error:approval_required", True, False, TOOLS),  # the request left pending
        ("error:approval_required", True, True, [*TOOLS, "approval_grant"]),  # an agent grant
        ("error:approval_required", True, True, [*TOOLS, "approval_override"]),  # not allowed
        ("error:approval_required", True, True, [*TOOLS, "event_decide"]),  # decides elsewhere
        ("error:approval_required", True, True, []),  # no tool listing at all
    ):
        assert not all(c["passed"] for c in walk.boundary_checks(*bad)), bad


def test_the_windows_good_observations_pass() -> None:
    for step_id, shown in GOOD.items():
        checks = walk.window_checks(step_id, shown)
        assert checks and all(c["passed"] for c in checks), (step_id, checks)


@pytest.mark.parametrize(("step_id", "change"), BAD)
def test_every_wrong_window_observation_fails_its_step(step_id: str, change: Any) -> None:
    shown = {**GOOD[step_id], **change}
    assert not all(c["passed"] for c in walk.window_checks(step_id, shown))


def test_a_step_the_window_did_not_record_fails() -> None:
    for step_id in walk.DESKTOP_OWNER_IDS:
        assert not all(c["passed"] for c in walk.window_checks(step_id, {}))


def test_the_spec_and_the_harness_cover_the_same_steps_and_signals() -> None:
    text = SPEC.read_text(encoding="utf-8")
    recorded = re.findall(r'record\(\{ id: "(W\d\.\d)"', text)
    assert recorded == walk.DESKTOP_OWNER_IDS
    harness = (REPO / "tools" / "walk_cp19.py").read_text(encoding="utf-8")
    sent_by_spec = set(re.findall(r'signal\("([a-z-]+)"', text))
    awaited_by_spec = set(re.findall(r'waitFor\("([a-z-]+)"', text))
    sent_by_harness = set(re.findall(r'leg\.send\("([a-z-]+)"', harness))
    awaited_by_harness = set(re.findall(r'leg\.wait\("([a-z-]+)"', harness))
    assert sent_by_spec == awaited_by_harness and sent_by_spec
    assert sent_by_harness == awaited_by_spec and sent_by_harness
    # The leg drives the BUILT app with its own core, never the developer's interpreter.
    assert "executablePath: info.exe" in text and "delete base.PROMETHEOS_PYTHON" in text


def test_a_built_app_older_than_the_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "app"
    unpacked = app / "dist" / "win-unpacked"
    (unpacked / "resources" / "core").mkdir(parents=True)
    (app / "main.js").write_bytes(b"// the checkout's shell\n")
    (unpacked / "PrometheOS.exe").write_bytes(b"MZ")
    (unpacked / "resources" / "core" / "MANIFEST.json").write_text("{}", encoding="utf-8")
    asar = unpacked / "resources" / "app.asar"
    asar.write_bytes(b"header// an older shell\n")
    source = tmp_path / "src" / "prometheos" / "core.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"VERSION = 2\n")
    bundled = unpacked / "resources" / "core" / "Lib" / "site-packages" / "prometheos" / "core.py"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"VERSION = 1\n")  # a stale core
    monkeypatch.setattr(walk, "ROOT", tmp_path)
    monkeypatch.setattr(walk, "APP", app)
    monkeypatch.setattr(walk, "EXE", unpacked / "PrometheOS.exe")
    monkeypatch.setattr(walk, "ASAR", asar)
    with pytest.raises(RuntimeError, match="rebuild"):
        walk.built_app()
    asar.write_bytes(b"header// the checkout's shell\n")
    # A fresh shell over a stale core is refused too (review s3d-a F5).
    with pytest.raises(RuntimeError, match="core is not the checkout's src"):
        walk.built_app()
    bundled.write_bytes(b"VERSION = 2\n")
    assert walk.built_app()["exe_sha256"]
    # A module removed from src but still in the core is refused too (review s3d-a R2-F1).
    leftover = bundled.parent / "removed.py"
    leftover.write_bytes(b"OLD = True\n")
    with pytest.raises(RuntimeError, match=r"removed\.py \(not in src\)"):
        walk.built_app()
    leftover.unlink()
    (bundled.parent / "__pycache__").mkdir()
    (bundled.parent / "__pycache__" / "core.cpython-314.pyc").write_bytes(b"\0")
    assert walk.built_app()["exe_sha256"]  # compiled caches are not source
    (unpacked / "PrometheOS.exe").unlink()
    with pytest.raises(RuntimeError, match="build the app first"):
        walk.built_app()


def test_the_port_probe_tells_a_running_hub_from_a_stopped_one() -> None:
    """Review s3d-a F4/F7: every hub port the desktop leg used is probed; one that still answers
    is NOT STOPPED, and an unknown one is not a confirmation."""
    import socket

    assert walk.port_refused("") == "not checked"
    assert walk.ports_refused([]) == "not checked"
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        open_url = f"http://127.0.0.1:{listening.getsockname()[1]}/"
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            closed_url = f"http://127.0.0.1:{spare.getsockname()[1]}/"  # bound, not listening
        assert walk.port_refused(open_url) == "NOT STOPPED"
        assert walk.ports_refused([closed_url, open_url]) == "NOT STOPPED"
    assert walk.ports_refused([closed_url, open_url]).startswith("confirmed")


def test_the_shells_test_hooks_need_the_explicit_switch_and_no_page_can_reach_them() -> None:
    """Decision 0055 (coordinator's condition on clickTray): the hooks are a main-process global
    created only when PROMETHEOS_DESKTOP_TEST is exactly "1"; the window has no preload, no
    IPC and no Node, so the hub page cannot reach them. The packaged build is checked by the
    walk (W1.1) and the installed one by the shell spec's second test."""
    main = (REPO / "desktop" / "app" / "main.js").read_text(encoding="utf-8")
    # A release build (decision 0072) is stamped and never exposes them, whatever the switch.
    assert 'const RELEASE_BUILD = require("./package.json").prometheosRelease === true;' in main
    gate = 'const TEST_HOOKS = !RELEASE_BUILD && process.env.PROMETHEOS_DESKTOP_TEST === "1";'
    assert gate in main
    assert main.count("global.prometheosShell") == 1
    assert main.count("prometheosShell") == 1  # no other assignment (globalThis., window.)
    hook = main.index("global.prometheosShell")
    assert main.rindex("if (TEST_HOOKS) {", 0, hook) > main.rindex("\n}", 0, hook)
    for absent in ("ipcMain", "contextBridge", "preload", "nodeIntegration: true"):
        assert absent not in main, absent
    assert "contextIsolation: true, sandbox: true, nodeIntegration: false" in main
    shell_spec = (REPO / "desktop" / "app" / "test" / "shell.spec.ts").read_text(encoding="utf-8")
    assert "without the test switch the app exposes no test hooks" in shell_spec


# ----- the snapshot itself bites (review s3d-a F1) ------------------------------------------


def run_mcp_leg(work: Path) -> Any:
    import asyncio

    store = walk.Store(work)
    recorder = walk.Recorder("B", "mcp", store, work / "evidence")
    asyncio.run(walk.mcp_leg(store, recorder))
    assert recorder.failures == []
    return store


def reopened(store: Any, home: Path) -> Any:
    """The same project and names over a copy of the store's home."""
    from prometheos.app.service import Service

    view = walk.Store.__new__(walk.Store)
    view.work, view.due, view.home = home.parent, {}, home
    view.svc = Service(home=home, actor="hub")
    view.owner, view.pid, view.names = store.owner, store.pid, dict(store.names)
    return view


MUTATIONS = {
    "who acknowledged the event": (
        "UPDATE events SET acknowledged_by = 'somebody else' WHERE acknowledged_by IS NOT NULL"
    ),
    "a note's sensitivity": (
        "UPDATE records SET sensitivity = 'restricted' WHERE kind = 'entry'; "
        "UPDATE revisions SET sensitivity = 'restricted' WHERE record_id LIKE 'entry_%'"
    ),
    "an archived work item": "UPDATE records SET lifecycle = 'archived' WHERE kind = 'action'",
    "a re-pointed capability": (
        "INSERT INTO capabilities SELECT lower(hex(randomblob(16))), lower(hex(randomblob(32))), "
        "project_id, operation, digest, max_uses, uses, expires_at, policy_version, label, "
        "synthetic, created_at, revoked_at FROM capabilities; "
        "UPDATE approval_requests SET capability_id = (SELECT id FROM capabilities "
        "WHERE id <> approval_requests.capability_id LIMIT 1); "
        "DELETE FROM capabilities WHERE id NOT IN (SELECT capability_id FROM approval_requests)"
    ),
    "a body's text": "UPDATE bodies SET encoded = replace(encoded, 'Walk note', 'Walk Note')",
}


def test_two_mcp_legs_reach_equal_snapshots_and_every_mutation_differs(tmp_path: Path) -> None:
    """The snapshot is deterministic across two fresh stores, and each mutation of a real store
    that the round-1 snapshot missed now changes it."""
    import sqlite3
    from contextlib import closing

    first = run_mcp_leg(tmp_path / "one")
    second = run_mcp_leg(tmp_path / "two")
    base = walk.snapshot(first)
    assert walk.first_difference(base, walk.snapshot(second)) is None
    assert base["records"] and base["revisions"] and base["bodies"] and base["event_rows"]
    assert any(r.get("acknowledged_by") for r in base["event_rows"])
    assert [a["capability_id"] for a in base["authority"]["approval_requests"]] == [
        "<ack_capability>"
    ]
    for label, sql in MUTATIONS.items():
        home = tmp_path / f"copy-{len(label)}-{abs(hash(label)) % 10_000}" / "home"
        shutil.copytree(first.home, home)
        view = reopened(first, home)
        with closing(sqlite3.connect(view.db_path())) as conn:
            conn.executescript(sql)
            conn.commit()
        assert walk.first_difference(base, walk.snapshot(view)) is not None, label


def test_a_failure_inside_an_exception_group_is_reported_with_its_cause() -> None:
    """Review s3d-a R2-F2: the desktop leg reports the spec's own failure, not "unhandled errors
    in a TaskGroup"."""
    group = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [RuntimeError("the desktop spec ended before it sent owner-granted")],
    )
    message = walk.describe(group)
    assert message == "RuntimeError: the desktop spec ended before it sent owner-granted"
    assert walk.describe(ValueError("plain")) == "ValueError: plain"


def test_the_cleanup_finds_the_walks_processes_and_their_helpers_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 0056 (review s3d-a F7/R2-F2, s13-followups-a F1/F2): the cleanup takes the
    processes whose own command line names this walk's home or project. Electron's helpers carry
    --user-data-dir inside the home, so they are found, also as orphans whose main process has
    exited. Parent IDs are never followed (Windows reuses them), so a foreign program whose dead
    launcher's ID went to the app, another instance of the app and its helpers stay untouched."""
    exe = str(walk.EXE)
    home = Path(r"C:\walk\store-a\home")
    data = f'--user-data-dir="{home / "desktop" / "electron"}"'
    other = r'--user-data-dir="C:\other\home\desktop\electron"'
    rows = [
        {"ProcessId": 10, "ParentProcessId": 1, "ExecutablePath": exe,
         "CommandLine": f'"{exe}" --project project_ours --hidden'},
        {"ProcessId": 11, "ParentProcessId": 10, "ExecutablePath": exe,
         "CommandLine": f'"{exe}" --type=gpu-process {data}'},
        {"ProcessId": 12, "ParentProcessId": 99, "ExecutablePath": exe,  # an orphaned helper
         "CommandLine": f'"{exe}" --type=utility {data}'},
        {"ProcessId": 13, "ParentProcessId": 10, "ExecutablePath": r"C:\Windows\notepad.exe",
         "CommandLine": "notepad.exe"},  # a foreign program with a reused parent ID
        {"ProcessId": 20, "ParentProcessId": 1, "ExecutablePath": exe,
         "CommandLine": f'"{exe}" --project project_other'},
        {"ProcessId": 21, "ParentProcessId": 20, "ExecutablePath": exe,
         "CommandLine": f'"{exe}" --type=renderer {other}'},
        {"ProcessId": 30, "ParentProcessId": 1, "ExecutablePath": r"C:\other\tool.exe",
         "CommandLine": "tool.exe"},
    ]  # fmt: skip

    class Done:
        stdout = json.dumps(rows)

    monkeypatch.setattr(walk.subprocess, "run", lambda *a, **k: Done())
    ours = sorted(p["pid"] for p in walk.app_processes(home, "project_ours"))
    assert ours == [10, 11, 12]
    # Without a home (the check before the MCP leg): every process of the built app counts.
    assert sorted(p["pid"] for p in walk.app_processes()) == [10, 11, 12, 20, 21]
