"""C21-WALK (CP21.4, decision 0091): the walk harness (tools/walk_cp21.py) and its guards.

The full walk (``prometheos start`` from a uv tool install, the owner in a browser at the home's
own host name, a core restart, the launcher and pairing steps, then a second fresh store through
MCP alone) is the checkpoint suite's ``walk`` command; it never runs inside the parallel test
suite. Here the harness' own judgement is mutation-tested: a store divergence, a missing step, a
different outcome or agent answer, a missing launcher step and every wrong browser observation
each fail the walk; the spec and the harness hand over the same signals; the browser boundary
admits the home's host name only when it is one; and the walk never changes C19-WALK's module.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "hub" / "walk21" / "walk.spec.ts"


def load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("walk_cp21", REPO / "tools" / "walk_cp21.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["walk_cp21"] = module
    spec.loader.exec_module(module)
    return module


walk = load_tool()


def legs() -> tuple[dict[str, Any], dict[str, Any]]:
    steps = [
        {"id": i, "outcome": "ok", "normalised_response_sha256": "a" * 64} for i in walk.SCRIPT_IDS
    ]
    launcher = [
        {"id": s["id"], "outcome": "ok", "normalised_response_sha256": "d" * 64}
        for s in walk.LAUNCHER_STEPS
    ]
    doc = {
        "hub": {"steps": copy.deepcopy(steps) + launcher},
        "mcp": {"steps": copy.deepcopy(steps)},
    }
    outcomes = {"hub": {"entries": [{"label": "captured_note", "title": walk.walk_cp19.NOTE_TITLE}],
                        "approvals": [["event_acknowledge", "used", 0]]}}  # fmt: skip
    outcomes["mcp"] = copy.deepcopy(outcomes["hub"])
    return doc, outcomes


def test_two_equivalent_legs_are_judged_equal() -> None:
    doc, outcomes = legs()
    equivalence, failures = walk.judge(doc, outcomes)
    assert failures == [] and equivalence["stored outcomes"] == "equal"
    assert equivalence["launcher and pairing steps (hub leg only)"] == "5 of 5 recorded"


def test_a_store_divergence_fails_the_walk() -> None:
    doc, outcomes = legs()
    outcomes["mcp"]["approvals"][0][1] = "granted"
    _, failures = walk.judge(doc, outcomes)
    assert any("stored outcomes differ" in f for f in failures)


@pytest.mark.parametrize("step_id", ["W1.1", "W3.2", "W6.1"])
def test_a_different_step_outcome_fails_the_walk(step_id: str) -> None:
    doc, outcomes = legs()
    next(s for s in doc["mcp"]["steps"] if s["id"] == step_id)["outcome"] = "error:forbidden"
    _, failures = walk.judge(doc, outcomes)
    assert any("step outcomes differ" in f and step_id in f for f in failures)


@pytest.mark.parametrize("step_id", ["W4.2", "L3"])
def test_a_missing_scripted_or_launcher_step_fails_the_walk(step_id: str) -> None:
    doc, outcomes = legs()
    doc["hub"]["steps"] = [s for s in doc["hub"]["steps"] if s["id"] != step_id]
    _, failures = walk.judge(doc, outcomes)
    assert failures, step_id


@pytest.mark.parametrize("step_id", ["W3.1", "W3.2", "W3.3", "W5.1", "W6.2"])
def test_a_different_agent_answer_fails_the_walk(step_id: str) -> None:
    doc, outcomes = legs()
    next(s for s in doc["hub"]["steps"] if s["id"] == step_id)["normalised_response_sha256"] = (
        "b" * 64
    )
    _, failures = walk.judge(doc, outcomes)
    assert any("agent answers differ" in f and step_id in f for f in failures)


GOOD: dict[str, dict[str, Any]] = {
    "W1.1": {"paired": True, "hash_after": "", "href_has_code": False,
             "request_lines_with_code": 0, "url_host": "h0123456789abcdef.localhost:41234",
             "device_cookie": {"host_only": True, "http_only": True, "same_site": "Strict"}},
    "W2.1": {"saved": True},
    "W4.1": {"shown_on_approvals_page": True},
    "W4.2": {"granted_in_hub": True, "plan_digest_shown": True},
    "W6.1": {"resumed_with_device": True, "paired_again": False},
    "L4": {"resumed_with_device": True, "paired_again": False},
    "L5": {"signed_out": True, "still_signed_out_after_reload": True,
           "request_lines_with_code": 0},
}  # fmt: skip
BAD: list[tuple[str, dict[str, Any]]] = [
    ("W1.1", {"paired": False}),
    ("W1.1", {"hash_after": "#pair=abc"}),
    ("W1.1", {"href_has_code": True}),
    ("W1.1", {"request_lines_with_code": 1}),
    ("W1.1", {"url_host": "127.0.0.1:41234"}),
    ("W1.1", {"device_cookie": None}),
    ("W1.1", {"device_cookie": {"host_only": False, "http_only": True, "same_site": "Strict"}}),
    ("W1.1", {"device_cookie": {"host_only": True, "http_only": False, "same_site": "Strict"}}),
    ("W1.1", {"device_cookie": {"host_only": True, "http_only": True, "same_site": "Lax"}}),
    ("W2.1", {"saved": False}),
    ("W4.1", {"shown_on_approvals_page": False}),
    ("W4.2", {"plan_digest_shown": False}),
    ("W6.1", {"resumed_with_device": False}),
    ("W6.1", {"paired_again": True}),
    ("L4", {"paired_again": True}),
    ("L5", {"signed_out": False}),
    ("L5", {"still_signed_out_after_reload": False}),
    ("L5", {"request_lines_with_code": 2}),
]


def test_the_browsers_good_observations_pass() -> None:
    for step_id, shown in GOOD.items():
        checks = walk.browser_checks(step_id, shown)
        assert checks and all(c["passed"] for c in checks), (step_id, checks)


@pytest.mark.parametrize(("step_id", "change"), BAD)
def test_every_wrong_browser_observation_fails_its_step(step_id: str, change: Any) -> None:
    shown = {**GOOD[step_id], **change}
    assert not all(c["passed"] for c in walk.browser_checks(step_id, shown))


def test_a_step_the_browser_did_not_record_fails() -> None:
    for step_id in walk.BROWSER_IDS:
        assert not all(c["passed"] for c in walk.browser_checks(step_id, {}))


def test_the_spec_and_the_harness_cover_the_same_steps_and_signals() -> None:
    text = SPEC.read_text(encoding="utf-8")
    recorded = re.findall(r'record\(\{\s*id: "([WL][\d.]+)"', text)
    assert recorded == walk.BROWSER_IDS
    harness = (REPO / "tools" / "walk_cp21.py").read_text(encoding="utf-8")
    sent_by_spec = set(re.findall(r'signal\("([a-z-]+)"', text))
    awaited_by_spec = set(re.findall(r'waitFor\("([a-z-]+)"', text))
    sent_by_harness = set(re.findall(r'leg\.send\("([a-z-]+)"', harness))
    awaited_by_harness = set(re.findall(r'leg\.wait\("([a-z-]+)"', harness))
    assert sent_by_spec == awaited_by_harness and sent_by_spec
    assert sent_by_harness == awaited_by_spec and sent_by_harness
    # The spec runs inside the boundary's journey guard, like every other browser suite.
    assert 'import { guardedTest as test } from "../e2e/boundary-guard";' in text


def test_launch_codes_never_reach_the_evidence() -> None:
    call = {"stdout": "x", "body": {"url": "http://h.localhost:1/#pair=SECRETCODE123"}}
    cleaned = json.dumps(walk.without_secrets(call, ["SECRETCODE123", ""]))
    assert "SECRETCODE123" not in cleaned and "<launch-code>" in cleaned


def test_the_walk_never_changes_c19_walks_module() -> None:
    spec = importlib.util.spec_from_file_location(
        "walk_cp19_fresh", REPO / "tools" / "walk_cp19.py"
    )
    assert spec and spec.loader
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert walk.walk_cp19 is not fresh and walk.walk_cp19.__name__ != "walk_cp19"
    assert not any(s["id"].startswith("L") for s in fresh.STEPS)
    assert fresh.NOTE_TITLE != walk.walk_cp19.NOTE_TITLE
    loaded = sys.modules.get("walk_cp19")
    if loaded is not None:
        assert not any(s["id"].startswith("L") for s in loaded.STEPS)


def _boundary_args(host: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PROMETHEOS_BOUNDARY_HOME_HOST"}
    if host is not None:
        env["PROMETHEOS_BOUNDARY_HOME_HOST"] = host
    script = (
        "import('./e2e/boundary.ts').then(m => console.log(JSON.stringify(m.BOUNDARY_ARGS)))"
        ".catch(e => { console.error(String(e)); process.exit(3); })"
    )
    return subprocess.run(["node", "-e", script], cwd=REPO / "hub", env=env,
                          capture_output=True, text=True, timeout=60, check=False)  # fmt: skip


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_boundary_admits_one_home_host_name_and_nothing_else() -> None:
    plain = _boundary_args(None)
    assert plain.returncode == 0, plain.stderr
    rule = next(a for a in json.loads(plain.stdout) if a.startswith("--host-resolver-rules="))
    assert rule.endswith("EXCLUDE ::1")  # unchanged in every other suite
    home = _boundary_args("h0123456789abcdef.localhost")
    assert home.returncode == 0, home.stderr
    rule = next(a for a in json.loads(home.stdout) if a.startswith("--host-resolver-rules="))
    assert rule.endswith("EXCLUDE ::1, EXCLUDE h0123456789abcdef.localhost")
    for hostile in (
        "example.com",
        "*.localhost",
        "h0123.localhost",
        "h0123456789abcdef.localhost, EXCLUDE *",
    ):
        refused = _boundary_args(hostile)
        assert refused.returncode == 3 and "not a home host name" in refused.stderr, hostile
