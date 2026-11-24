"""C11-WALK (CP11 S11.5): the walk harness (tools/walk_cp11.py) and its guards.

The full walk (the owner in the browser on the real hub with the agent over the real MCP server,
then a second fresh store through MCP alone) is run by the checkpoint suite's command. Here the
MCP half runs for real (the official SDK client against ``prometheos mcp serve --stdio``, the
Zotero stub on loopback), the browser half runs when Node and Playwright's Chromium headless shell
are present (skipped, never counted as a pass, otherwise; the walk command itself fails where they
are absent), and the harness' own judgement is mutation-tested: a store divergence, a missing
step, a different outcome, a different agent answer, a failed or empty store assertion, and a
Zotero stub that saw a write, an Origin header, a key or nothing at all each fail the walk.
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

import browser_shell
import pytest

REPO = Path(__file__).resolve().parents[1]


def load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("walk_cp11", REPO / "tools" / "walk_cp11.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["walk_cp11"] = module
    spec.loader.exec_module(module)
    return module


walk = load_tool()


def test_the_mcp_half_runs_the_whole_script_through_the_official_client(tmp_path: Path) -> None:
    out = tmp_path / "walk"
    assert walk.main(["--out", str(out), "--only", "mcp"]) == 0
    doc = json.loads((out / "transcript.json").read_text(encoding="utf-8"))
    assert doc["failures"] == []
    steps = {s["id"]: s for s in doc["mcp"]["steps"]}
    assert list(steps) == walk.SCRIPT_IDS  # every scripted step, in order
    for step in steps.values():
        assert step["store_assertions"] and all(a["passed"] for a in step["store_assertions"])
        assert len(step["normalised_response_sha256"]) == 64
    # The owner's steps are labelled as the harness' synthetic owner channel (no hub runs); the
    # agent's are MCP calls.
    owner_ids = [s["id"] for s in walk.STEPS if s["actor"] == "owner"]
    agent_ids = [s["id"] for s in walk.STEPS if s["actor"] == "agent"]
    assert {steps[i]["via"] for i in owner_ids} == {"owner-service"}
    assert {steps[i]["via"] for i in agent_ids} == {"mcp"}
    assert (out / "evidence" / "mcp" / "M2.2.json").is_file()
    # The stub of Zotero's local API was read, and only read.
    assert doc["zotero"]["methods"] == ["GET"] and doc["zotero"]["requests"] >= 3
    assert doc["zotero"]["with Origin"] == 0 and doc["zotero"]["with a key or authorisation"] == 0
    outcome = json.loads((out / "outcome-mcp.json").read_text(encoding="utf-8"))
    works = {w["title"]: w for w in outcome["works"]}
    drag = next(w for t, w in works.items() if t.startswith("Aerodynamic"))
    assert drag["access_state"] == "full_text" and drag["full_text_pages"] == 1
    bases = {json.loads(p)[0] for p in drag["provenance"]}
    assert {"zotero", "bibliography"} <= bases and len(bases) >= 3
    bearing = works["Bearing Drag in Rarefied Gas"]
    assert bearing["access_state"] == "abstract_only"
    assert bearing["abstract"]["type"] == "json"  # a Zotero abstract, cited by JSON pointer
    assert "Bearing drag in rarefied gas" not in works  # the deselected preprint is not there
    assert outcome["roots"] == ["papers"]
    # the one thing the agent asked for (A1): committing a Zotero plan of its own, pending (rank 0)
    assert outcome["approvals"] == [["zotero_import_commit", "pending", 0]]
    rows = {r["work"]: r for r in outcome["matrix"]["rows"]}
    assert rows["work:Bearing Drag in Rarefied Gas"]["access_state"] == "abstract_only"
    drag_row = next(r for k, r in rows.items() if k.startswith("work:Aerodynamic"))
    assert drag_row["needs_review"] is False
    cells = {c["column_id"]: c for c in drag_row["cells"]}
    assert {k: v["state"] for k, v in cells.items()} == {
        "method": "proposed",
        "pressure": "proposed",
        "drag-coefficient": "reviewed",
        "key-finding": "reviewed",
    }
    assert [b["needs_review"] for b in outcome["matrix"]["briefs"]] == [True]
    assert sorted(c[0] for c in outcome["corrections"]) == ["applied"]
    assert outcome["entries"] and {e[1] for e in outcome["entries"]} == {"assumption"}
    assert sorted(set(r[0] for r in outcome["runs"])) == [
        "library_import_commit",
        "library_import_plan",
        "matrix_extract",
        "zotero_import_commit",
        "zotero_import_plan",
    ]


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
    assert browser == [s["id"] for s in walk.STEPS if s["actor"] == "owner"]
    agent = [s["id"] for s in doc["hub"]["steps"] if s["via"] == "mcp"]
    assert agent == [s["id"] for s in walk.STEPS if s["actor"] == "agent"]
    assert len(list((out / "screenshots").glob("*.png"))) >= 8
    boundary = doc["browser_boundary"]
    assert boundary["all_passed"] is True and set(boundary["controls"].values()) == {"passed"}
    journeys = boundary["journey_browsers"]
    assert journeys and all("chrome-headless-shell" in j["image"] for j in journeys)
    assert doc["zotero"]["methods"] == ["GET"]


# ----- the harness' judgement bites (mutation-style negatives) ------------------------------------


def halves() -> tuple[dict[str, Any], dict[str, Any]]:
    def step(step_id: str) -> dict[str, Any]:
        return {"id": step_id, "outcome": "ok", "normalised_response_sha256": "a" * 64}

    steps = [step(i) for i in walk.SCRIPT_IDS]
    doc = {"hub": {"steps": copy.deepcopy(steps)}, "mcp": {"steps": copy.deepcopy(steps)}}
    outcomes = {
        "hub": {
            "works": [{"title": "A", "access_state": "full_text", "provenance": ["x"]}],
            "matrix": {
                "rows": [{"work": "<a>", "needs_review": False, "cells": [{"state": "ok"}]}]
            },
        }
    }
    outcomes["mcp"] = copy.deepcopy(outcomes["hub"])
    return doc, outcomes


def test_two_equivalent_halves_are_judged_equal() -> None:
    doc, outcomes = halves()
    equivalence, failures = walk.judge(doc, outcomes)
    assert failures == [] and equivalence["stored outcomes"] == "equal"


def test_a_store_divergence_fails_the_walk() -> None:
    doc, outcomes = halves()
    outcomes["mcp"]["matrix"]["rows"][0]["cells"][0]["state"] = "reviewed"
    _, failures = walk.judge(doc, outcomes)
    assert any("stored outcomes differ" in f and "matrix" in f for f in failures)
    doc, outcomes = halves()
    outcomes["mcp"]["works"][0]["provenance"].append("y")  # one half holds a record more
    assert walk.judge(doc, outcomes)[1]
    doc, outcomes = halves()
    outcomes["hub"]["matrix"]["rows"][0]["needs_review"] = True  # a flag one half never cleared
    assert walk.judge(doc, outcomes)[1]


def test_a_different_step_outcome_fails_the_walk() -> None:
    doc, outcomes = halves()
    next(s for s in doc["mcp"]["steps"] if s["id"] == "M2.2")["outcome"] = "error:policy_denied"
    _, failures = walk.judge(doc, outcomes)
    assert any("step outcomes differ" in f and "M2.2" in f for f in failures)


def test_a_missing_step_fails_the_walk() -> None:
    doc, outcomes = halves()
    doc["hub"]["steps"] = [s for s in doc["hub"]["steps"] if s["id"] != "M1.3"]
    _, failures = walk.judge(doc, outcomes)
    assert any("steps missing" in f and "M1.3" in f for f in failures)


def test_a_different_agent_answer_fails_the_walk() -> None:
    doc, outcomes = halves()
    next(s for s in doc["hub"]["steps"] if s["id"] == "A1")["normalised_response_sha256"] = "b" * 64
    _, failures = walk.judge(doc, outcomes)
    assert any("agent answers differ" in f and "A1" in f for f in failures)


def test_a_different_owner_response_alone_is_not_an_agent_difference() -> None:
    """Owner steps are compared through the stored outcome, not by response digest (the browser
    and the service answer in different shapes)."""
    doc, outcomes = halves()
    next(s for s in doc["hub"]["steps"] if s["id"] == "M1.5")["normalised_response_sha256"] = (
        "c" * 64
    )
    assert walk.judge(doc, outcomes)[1] == []


def test_a_failed_store_assertion_or_an_empty_step_fails_the_walk(tmp_path: Path) -> None:
    class Store:
        names: dict[str, str] = {}  # noqa: RUF012

        def norm(self, value: Any) -> Any:
            return value

    recorder = walk.Recorder("A", "hub", Store(), tmp_path / "evidence")
    recorder.add("M2.2", "mcp", {}, {}, "ok", [walk.check("the row is flagged", False)])
    assert recorder.failures and "M2.2" in recorder.failures[0]
    recorder = walk.Recorder("A", "hub", Store(), tmp_path / "evidence2")
    recorder.add("A4", "mcp", {}, {}, "ok", [])  # a step that asserts nothing
    assert recorder.failures and "A4" in recorder.failures[0]


class FakeStub:
    def __init__(self, requests: list[dict[str, Any]], dropped: list[Any] | None = None) -> None:
        self.requests = requests
        self.dropped = dropped or []


def read() -> dict[str, Any]:
    return {
        "method": "GET",
        "origin": None,
        "has_authorization": False,
        "has_zotero_api_key": False,
    }


def test_the_zotero_stub_must_have_seen_only_reads() -> None:
    report, problems = walk.zotero_report(FakeStub([read(), read()]))
    assert problems == [] and report["methods"] == ["GET"]
    for mutation in (
        {**read(), "method": "POST"},
        {**read(), "origin": "http://127.0.0.1:1"},
        {**read(), "has_authorization": True},
        {**read(), "has_zotero_api_key": True},
    ):
        _, problems = walk.zotero_report(FakeStub([read(), mutation]))
        assert problems, mutation
    _, problems = walk.zotero_report(FakeStub([]))  # the import never read it: not a pass
    assert problems and "no request" in problems[0]
    _, problems = walk.zotero_report(FakeStub([read()], dropped=[read()]))
    assert problems


def test_nulls_in_closed_mcp_models_do_not_hide_or_invent_a_difference() -> None:
    assert walk.without_nulls({"a": None, "b": [{"c": None, "d": 1}]}) == {"b": [{"d": 1}]}
    left = {"where": {"page": 2, "end_line": None}}
    right = {"where": {"page": 2}}
    assert walk.without_nulls(left) == right
    assert walk.without_nulls({"where": {"page": 3}}) != right


def test_snapshot_sets_compare_by_content_not_by_raw_id_order() -> None:
    """The sets of a store are listed in the order of their raw (random) IDs; compared by their
    normalised content they must be equal whichever order the IDs sort in."""

    class Ids:
        def __init__(self, names: dict[str, str]) -> None:
            self.names = names

    def snap(first: str, second: str) -> dict[str, Any]:
        st = Ids({first: "alpha", second: "beta"})
        raw = {key: [] for key in walk.UNORDERED}
        raw["relations"] = sorted([["version_of", first, second], ["version_of", second, first]])
        return walk.settle(walk.Store.norm(st, raw))  # type: ignore[arg-type]

    assert snap("work_aaaa", "work_ffff") == snap("work_ffff", "work_aaaa")


# ----- the two scripts stay one script ------------------------------------------------------------


def test_the_browser_spec_and_the_harness_cover_the_same_steps() -> None:
    spec = (REPO / "hub" / "walk11" / "walk.spec.ts").read_text(encoding="utf-8")
    in_spec = re.findall(r'record\(\{ id: "(M\d\.\d)"', spec)
    owner_steps = [s["id"] for s in walk.STEPS if s["actor"] == "owner"]
    assert in_spec == owner_steps
    source = (REPO / "tools" / "walk_cp11.py").read_text(encoding="utf-8")
    in_python = re.findall(r'"(M\d\.\d)",\s*"owner-service"', source)
    assert in_python == owner_steps  # the same claims, through the service
    recorded = re.findall(r'(?:rec\.add\(|rec\.add\(\n\s*)"(A\d)"', source)
    assert sorted(set(recorded)) == [s["id"] for s in walk.STEPS if s["actor"] == "agent"]
    # The agent is the official SDK client against the real stdio server, not the service.
    assert "McpChat" in source and "from walk_cp18 import" in source
    chat = (REPO / "tools" / "walk_cp18.py").read_text(encoding="utf-8")
    assert "StdioServerParameters" in chat and '"mcp", "serve", "--stdio"' in chat


def test_the_walk_proves_the_stub_never_wrote_and_the_script_names_the_misquotes() -> None:
    spec = (REPO / "hub" / "walk11" / "walk.spec.ts").read_text(encoding="utf-8")
    source = (REPO / "tools" / "walk_cp11.py").read_text(encoding="utf-8")
    for claim in ("0.0042 does not occur verbatim in the quoted evidence",
                  "cell drag-coefficient: 0.042 does not occur verbatim"):  # fmt: skip
        assert claim in spec and claim in source
    assert "zotero_report(stub)" in source
