"""C10-WALK (S28): the walk harness (tools/walk_cp10.py) and its guards.

The full walk (the browser half on the real hub, then the MCP half on a second fresh store)
is run by the checkpoint suite's command; here the MCP half runs for real (the official SDK
client against a ``prometheos mcp serve --stdio`` process), the browser half runs when Node and
Playwright's Chromium headless shell are present (skipped, never counted as a pass, otherwise;
the walk command itself fails where they are absent), and the harness'
own judgement is mutation-tested: a store divergence, a missing step, a different authority
outcome or a different read answer each fail the walk.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, ClassVar

import browser_shell
import pytest

REPO = Path(__file__).resolve().parents[1]


def load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("walk_cp10", REPO / "tools" / "walk_cp10.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["walk_cp10"] = module
    spec.loader.exec_module(module)
    return module


walk = load_tool()


def test_the_mcp_half_runs_k1_to_k4_through_the_official_client(tmp_path: Path) -> None:
    out = tmp_path / "walk"
    assert walk.main(["--out", str(out), "--only", "mcp"]) == 0
    doc = json.loads((out / "transcript.json").read_text(encoding="utf-8"))
    assert doc["failures"] == []
    steps = {s["id"]: s for s in doc["mcp"]["steps"]}
    assert list(steps) == walk.SCRIPT_IDS  # every scripted step, in order
    for step in steps.values():
        assert step["store_assertions"] and all(a["passed"] for a in step["store_assertions"])
        assert len(step["response_sha256"]) == 64 and len(step["normalised_response_sha256"]) == 64
    # The agent's authority results: denied twice, and the owner's grant then resume ran once.
    assert steps["K2.2"]["outcome"] == "error:approval_required"
    assert steps["K3.5"]["outcome"] == "error:approval_required"
    assert steps["K2.4"]["outcome"] == "ok" and steps["K2.5"]["outcome"] == "ok"
    # The owner's steps are labelled as the harness' synthetic owner channel (no hub runs).
    assert steps["K2.3"]["via"] == "owner-service" and steps["K3.6"]["via"] == "owner-service"
    # The raw answers are retained for the reviewer.
    assert (out / "evidence" / "mcp" / "K2.2.json").is_file()
    outcome = json.loads((out / "outcome-mcp.json").read_text(encoding="utf-8"))
    assert outcome["claims"]["claim_a"]["state"] == "supported_within_conditions"
    assert (
        outcome["assumptions"]["assumption_drag"]["superseded_by"]["id"] == "<assumption_drag_v2>"
    )
    assert sorted(f[0] for f in outcome["flags"]) == sorted(
        ["claim_a", "action", "anchor_uses_assumption", "anchor_uses_claim"]
    )
    assert all(f[3] for f in outcome["flags"])  # flagged in the commit that applied the correction
    assert outcome["approvals"] == [["apply", "pending", 0], ["apply", "used", 0]]
    assert {r[1] for r in outcome["runs"]} == {"completed"}
    assert ["source_import_plan", "completed", 0] in outcome["runs"]


def test_the_browser_half_runs_when_node_and_the_chromium_shell_are_present(tmp_path: Path) -> None:
    if shutil.which("node") is None or not (REPO / "hub" / "node_modules").is_dir():
        pytest.skip("Node and hub/node_modules are needed for the browser half (not a pass)")
    if browser_shell.find_headless_shell(REPO / "hub") is None:
        pytest.skip("the Playwright Chromium headless shell is needed for the browser half")
    out = tmp_path / "walk"
    code = walk.main(["--out", str(out)])
    doc = json.loads((out / "transcript.json").read_text(encoding="utf-8"))
    assert code == 0, doc["failures"]
    assert doc["equivalence"]["stored outcomes"] == "equal"
    assert doc["equivalence"]["the hub was stopped during the MCP half"].startswith("confirmed")
    browser = [s["id"] for s in doc["hub"]["steps"] if s["via"] == "browser"]
    assert browser == ["K1.5", "K2.3", "K2.8", "K3.4", "K3.6", "K4.6"]
    assert len(list((out / "screenshots").glob("*.png"))) >= 6
    # The walk's browser proved its own loopback-only boundary before it was driven (decision 0028).
    boundary = doc["browser_boundary"]
    assert boundary["all_passed"] is True and boundary["browser"]["name"] == "chromium"
    assert boundary["browser"]["version"].startswith("153.")
    assert set(boundary["controls"].values()) == {"passed"}
    # The browser the walk's journey itself ran in was checked and recorded too (its own launch).
    journeys = boundary["journey_browsers"]
    assert journeys and all(j["version"].startswith("153.") for j in journeys)
    assert all("chrome-headless-shell" in j["image"] for j in journeys)


# ----- the harness' judgement bites (mutation-style negatives) ------------------------------------


def halves() -> tuple[dict[str, Any], dict[str, Any]]:
    def step(step_id: str, outcome: str = "ok", digest: str = "a" * 64) -> dict[str, Any]:
        return {
            "id": step_id,
            "outcome": outcome,
            "normalised_response_sha256": digest,
            "store_assertions": [],
        }

    steps = [step(i) for i in walk.SCRIPT_IDS]
    doc = {"hub": {"steps": copy.deepcopy(steps)}, "mcp": {"steps": copy.deepcopy(steps)}}
    outcomes = {"hub": {"claims": {"claim_a": {"state": "supported"}}, "flags": []}}
    outcomes["mcp"] = copy.deepcopy(outcomes["hub"])
    return doc, outcomes


def test_two_equivalent_halves_are_judged_equal() -> None:
    doc, outcomes = halves()
    equivalence, failures = walk.judge(doc, outcomes)
    assert failures == [] and equivalence["stored outcomes"] == "equal"


def test_a_store_divergence_fails_the_walk() -> None:
    doc, outcomes = halves()
    outcomes["mcp"]["claims"]["claim_a"]["state"] = "superseded"
    _, failures = walk.judge(doc, outcomes)
    assert any("stored outcomes differ" in f and "claim_a" in f for f in failures)
    doc, outcomes = halves()
    outcomes["mcp"]["flags"] = [["claim_a", "correction", "x", True]]
    assert walk.judge(doc, outcomes)[1]


def test_a_different_authority_result_fails_the_walk() -> None:
    doc, outcomes = halves()
    denied = next(s for s in doc["mcp"]["steps"] if s["id"] == "K2.2")
    denied["outcome"] = "error:policy_denied"
    _, failures = walk.judge(doc, outcomes)
    assert any("step outcomes differ" in f and "K2.2" in f for f in failures)


def test_a_missing_step_fails_the_walk() -> None:
    doc, outcomes = halves()
    doc["mcp"]["steps"] = [s for s in doc["mcp"]["steps"] if s["id"] != "K3.6"]
    _, failures = walk.judge(doc, outcomes)
    assert any("steps missing" in f and "K3.6" in f for f in failures)


def test_a_walk_whose_browser_did_not_prove_its_boundary_fails(tmp_path: Path) -> None:
    def evidence(statuses: list[str], all_passed: bool) -> dict[str, Any]:
        return {
            "decision": "0028",
            "launch_args": ["--proxy-server=http://127.0.0.1:9"],
            "observed": {"browser": {"name": "chromium", "version": "153.0.8010.12"}},
            "controls": [{"control": f"control {i}", "status": s} for i, s in enumerate(statuses)],
            "all_passed": all_passed,
        }

    summary, problems = walk.boundary_evidence(tmp_path)  # the browser half left nothing
    assert summary == {"evidence": None} and problems
    path = tmp_path / "browser-boundary.json"
    path.write_text("not json", encoding="utf-8")
    assert walk.boundary_evidence(tmp_path)[1]
    path.write_text(json.dumps(evidence(["passed", "failed"], False)), encoding="utf-8")
    summary, problems = walk.boundary_evidence(tmp_path)
    assert summary["all_passed"] is False and problems  # a control that did not pass
    path.write_text(json.dumps(evidence(["passed", "failed"], True)), encoding="utf-8")
    # A claim of success is not taken on trust when a control's own status says otherwise.
    assert walk.boundary_evidence(tmp_path)[1]
    path.write_text(json.dumps(evidence([], True)), encoding="utf-8")
    assert walk.boundary_evidence(tmp_path)[1]  # no controls ran
    path.write_text(json.dumps(evidence(["passed", "passed"], True)), encoding="utf-8")
    # The controls passed; the walk test itself must have run in a worker the journey guard
    # checked, as Playwright's JSON report shows (review cp10-browser R3-F3).
    assert walk.boundary_evidence(tmp_path)[1]  # no report
    report = {
        "suites": [
            {
                "title": "walk.spec.ts",
                "specs": [
                    {
                        "title": "C10-WALK",
                        "tests": [
                            {
                                "projectName": "chromium",
                                "results": [{"workerIndex": 1, "status": "passed"}],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    (tmp_path / "playwright-report.json").write_text(json.dumps(report), encoding="utf-8")
    summary, problems = walk.boundary_evidence(tmp_path)
    assert problems and "worker 1" in problems[0]  # no journey record: an unguarded walk
    journey = {"project": "chromium", "worker": 1, "version": "153.0.8010.12", "image": "x"}
    path.write_text(
        json.dumps({**evidence(["passed", "passed"], True), "journeys": [journey]}),
        encoding="utf-8",
    )
    summary, problems = walk.boundary_evidence(tmp_path)
    assert problems == [] and summary["browser"]["name"] == "chromium"


def test_a_different_answer_to_a_pure_read_fails_the_walk() -> None:
    doc, outcomes = halves()
    changed = next(s for s in doc["hub"]["steps"] if s["id"] == "K4.4")
    changed["normalised_response_sha256"] = "b" * 64
    _, failures = walk.judge(doc, outcomes)
    assert any("read responses differ" in f and "K4.4" in f for f in failures)


def test_a_failed_store_assertion_fails_the_walk(tmp_path: Path) -> None:
    class Seed:
        names: ClassVar[dict[str, str]] = {}

        def norm(self, value: Any) -> Any:
            return value

        def view(self, value: Any) -> Any:
            return value

    recorder = walk.Recorder("A", "hub", Seed(), tmp_path / "evidence")
    recorder.add(
        "K2.2", "http", {}, {}, "error:approval_required", [walk.check("the agent is told", False)]
    )
    assert recorder.failures and "K2.2" in recorder.failures[0]


# ----- the two scripts stay one script -----------------------------------------------------------


def test_the_browser_spec_and_the_mcp_script_cover_the_same_steps() -> None:
    spec = (REPO / "hub" / "walk" / "walk.spec.ts").read_text(encoding="utf-8")
    ids_in_spec = re.findall(r'id: "(K\d\.\d)"', spec)
    assert ids_in_spec == [s["id"] for s in walk.STEPS]
    source = (REPO / "tools" / "walk_cp10.py").read_text(encoding="utf-8")
    ids_in_python = re.findall(r'rec\.add\(\s*"(K\d\.\d)"', source)
    assert ids_in_python == walk.SCRIPT_IDS
    # The MCP half is the official SDK client against the real stdio server, not the service.
    assert "StdioServerParameters" in source and '"walk_mcp.py"' in source
    assert 'runpy.run_module("prometheos", run_name="__main__")' in (
        REPO / "tools" / "walk_mcp.py"
    ).read_text(encoding="utf-8")


def test_k2_commits_nothing_between_the_owner_grant_and_the_resume() -> None:
    """Decision 0020: both halves check the project sequence, and the script has no step
    between the grant (K2.3) and the resume (K2.4)."""
    ids = [s["id"] for s in walk.STEPS]
    assert ids.index("K2.4") == ids.index("K2.3") + 1
    for text in (
        (REPO / "tools" / "walk_cp10.py").read_text(encoding="utf-8"),
        (REPO / "hub" / "walk" / "walk.spec.ts").read_text(encoding="utf-8"),
    ):
        assert "did not move between the grant and the resume" in text
