"""C18-WALK (CP18.4): the walk harness (tools/walk_cp18.py) and its guards.

The full walk (the owner in the browser on the real hub with the agent over the real MCP
server, then a second fresh store through MCP alone) is run by the checkpoint suite's command;
here the MCP half runs for real (the official SDK client against ``prometheos mcp serve
--stdio``), the browser half runs when Node and Playwright's Chromium headless shell are present
(skipped, never counted as a pass, otherwise; the walk command itself fails where they are
absent), and the harness' own judgement is mutation-tested: a store divergence, a missing step,
a different outcome, a different agent answer, and an agent message that starts work or presses
the researcher each fail the walk.
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
    spec = importlib.util.spec_from_file_location("walk_cp18", REPO / "tools" / "walk_cp18.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["walk_cp18"] = module
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
    assert {steps[i]["via"] for i in ("W1.1", "W2.1", "W5.1", "W5.2")} == {"owner-service"}
    assert {steps[i]["via"] for i in ("W3.1", "W4.1", "W4.2", "W4.3", "W5.3")} == {"mcp"}
    # The conversation is retained in full for the reviewer.
    conversation = doc["mcp"]["conversation"]
    assert conversation["W3.2"].rstrip().endswith("which one?")
    assert "Submit paper P1 to journal X" in conversation["W3.3"]
    assert (out / "evidence" / "mcp" / "W4.3.json").is_file()
    outcome = json.loads((out / "outcome-mcp.json").read_text(encoding="utf-8"))
    items = {i["label"]: i for i in outcome["work_items"]}
    assert items["p1"]["status"] == "done"
    assert items["p1"]["completion_evidence"][0]["version_id"] == "<output_version>"
    assert {items[k]["status"] for k in ("referee", "reply", "tidy")} == {"ready"}
    deadline = sorted(
        (e["subject"]["id"], e["data"]["days_before"], e["acknowledged"])
        for e in outcome["events"]
        if e["type"] == "deadline.approaching"
    )
    assert deadline == [("<p1>", 14, True), ("<referee>", 7, False), ("<reply>", 2, False)]
    # Only the agent's own changes are open work-item events; the owner's are acknowledged.
    changed = [e for e in outcome["events"] if e["type"].startswith("work_item.")]
    assert all(e["acknowledged"] == (e["note"] == "own change") for e in changed)
    assert sum(not e["acknowledged"] for e in changed) == 2
    assert outcome["approvals"] == []  # nothing asked for the owner's approval
    assert outcome["runs"] == [
        ["source_import_commit", "completed", 0],
        ["source_import_plan", "completed", 0],
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
    assert browser == ["W1.1", "W1.2", "W1.3", "W1.4", "W2.1", "W5.1", "W5.2"]
    agent = [s["id"] for s in doc["hub"]["steps"] if s["via"] == "mcp"]
    assert agent == ["W3.1", "W4.1", "W4.2", "W4.3", "W4.4", "W5.3"]
    assert len(list((out / "screenshots").glob("*.png"))) >= 6
    boundary = doc["browser_boundary"]
    assert boundary["all_passed"] is True and set(boundary["controls"].values()) == {"passed"}
    journeys = boundary["journey_browsers"]
    assert journeys and all("chrome-headless-shell" in j["image"] for j in journeys)


# ----- the harness' judgement bites (mutation-style negatives) ------------------------------------


def halves() -> tuple[dict[str, Any], dict[str, Any]]:
    def step(step_id: str) -> dict[str, Any]:
        return {"id": step_id, "outcome": "ok", "normalised_response_sha256": "a" * 64}

    steps = [step(i) for i in walk.SCRIPT_IDS]
    doc = {"hub": {"steps": copy.deepcopy(steps)}, "mcp": {"steps": copy.deepcopy(steps)}}
    outcomes = {
        "hub": {
            "work_items": [{"label": "p1", "status": "done"}],
            "events": [{"type": "deadline.approaching", "acknowledged": True}],
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
    outcomes["mcp"]["work_items"][0]["status"] = "in_progress"
    _, failures = walk.judge(doc, outcomes)
    assert any("stored outcomes differ" in f and "work_items" in f for f in failures)
    doc, outcomes = halves()
    outcomes["mcp"]["events"].append({"type": "deadline.approaching", "acknowledged": False})
    assert walk.judge(doc, outcomes)[1]  # a duplicate event on one half


def test_a_different_step_outcome_fails_the_walk() -> None:
    doc, outcomes = halves()
    next(s for s in doc["mcp"]["steps"] if s["id"] == "W4.3")["outcome"] = "error:validation_error"
    _, failures = walk.judge(doc, outcomes)
    assert any("step outcomes differ" in f and "W4.3" in f for f in failures)


def test_a_missing_step_fails_the_walk() -> None:
    doc, outcomes = halves()
    doc["hub"]["steps"] = [s for s in doc["hub"]["steps"] if s["id"] != "W2.1"]
    _, failures = walk.judge(doc, outcomes)
    assert any("steps missing" in f and "W2.1" in f for f in failures)


def test_a_different_agent_answer_fails_the_walk() -> None:
    doc, outcomes = halves()
    next(s for s in doc["hub"]["steps"] if s["id"] == "W3.1")["normalised_response_sha256"] = (
        "b" * 64
    )
    _, failures = walk.judge(doc, outcomes)
    assert any("agent answers differ" in f and "W3.1" in f for f in failures)


def test_a_failed_store_assertion_or_an_empty_step_fails_the_walk(tmp_path: Path) -> None:
    class Store:
        names: dict[str, str] = {}  # noqa: RUF012

        def norm(self, value: Any) -> Any:
            return value

    recorder = walk.Recorder("A", "hub", Store(), tmp_path / "evidence")
    recorder.add("W4.3", "mcp", {}, {}, "ok", [walk.check("the item is done", False)])
    assert recorder.failures and "W4.3" in recorder.failures[0]
    recorder = walk.Recorder("A", "hub", Store(), tmp_path / "evidence2")
    recorder.add("W4.4", "mcp", {}, {}, "ok", [])  # a step that asserts nothing
    assert recorder.failures and "W4.4" in recorder.failures[0]


QUEUE = {
    "timezone": "Asia/Riyadh",
    "entries": [
        {
            "entry": "work_item", "ref": "action_1", "title": "Submit paper P1 to journal X",
            "status": "ready", "due_at": "2026-10-25T08:48:00Z", "labels": ["paper P1"],
            "description_chars": 10,
        },
        {
            "entry": "event", "ref": "evt_1", "subject_title": "Submit paper P1 to journal X",
            "reason": "A work item's deadline is within 14 days.",
        },
    ],
}  # fmt: skip


def test_the_agent_message_follows_the_skill_and_passes_the_text_rules() -> None:
    message = walk.summary(QUEUE)
    assertions = walk.message_assertions(message, QUEUE)
    assert all(a["passed"] for a in assertions), assertions
    assert "Sun 25 Oct 2026, 11:48 (Asia/Riyadh)" in message


@pytest.mark.parametrize(
    "mutation",
    [
        "I will start with the most urgent item now.\n",
        "The deadline is close, so let me begin the submission right away.\n",
        "I'll get going on P1 and remind you every time until it is done.\n",
        "Which one?\nShould I also tidy the scripts?\n",  # a second question
    ],
)
def test_an_agent_message_that_starts_work_or_presses_fails_the_walk(mutation: str) -> None:
    message = walk.summary(QUEUE) + mutation
    assertions = walk.message_assertions(message, QUEUE)
    assert not all(a["passed"] for a in assertions), mutation


def test_an_agent_message_that_leaves_out_an_item_fails_the_walk() -> None:
    message = walk.summary(QUEUE).replace("Submit paper P1 to journal X", "a paper")
    assert not all(a["passed"] for a in walk.message_assertions(message, QUEUE))


# ----- the two scripts stay one script -----------------------------------------------------------


def test_the_browser_spec_and_the_harness_cover_the_same_steps() -> None:
    spec = (REPO / "hub" / "walk18" / "walk.spec.ts").read_text(encoding="utf-8")
    in_spec = re.findall(r'id: "(W\d\.\d)"', spec) + (["W1.x"] if "id: `W1." in spec else [])
    owner_steps = [s["id"] for s in walk.STEPS if s["actor"] == "owner"]
    assert in_spec == ["W2.1", "W5.1", "W5.2", "W1.x"]
    assert [i for i in owner_steps if not i.startswith("W1.")] == ["W2.1", "W5.1", "W5.2"]
    assert len(walk.ITEMS) == len([i for i in owner_steps if i.startswith("W1.")])
    source = (REPO / "tools" / "walk_cp18.py").read_text(encoding="utf-8")
    in_python = re.findall(r'rec\.add\(\s*"(W\d\.\d)"', source)
    assert sorted(in_python) == sorted(
        i for i in walk.SCRIPT_IDS if not i.startswith("W1.")
    )  # W1.x is recorded per item (owner_add_step)
    # The agent is the official SDK client against the real stdio server, not the service.
    assert "StdioServerParameters" in source and '"mcp", "serve", "--stdio"' in source


def test_the_walks_browser_runs_in_another_zone_than_the_project() -> None:
    """CP18-F1 (decision 0048): the browser's zone differs from the project's, by a different
    UTC offset, so the walk's checks fail if the hub used the browser's zone anywhere."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    spec = (REPO / "hub" / "walk18" / "walk.spec.ts").read_text(encoding="utf-8")
    (browser_zone,) = re.findall(r'const BROWSER_ZONE = "([^"]+)"', spec)
    assert "browser.newContext({ timezoneId: BROWSER_ZONE })" in spec
    assert "timezoneId: fx.zone" not in spec
    moment = datetime(2026, 11, 2, 12, tzinfo=UTC)
    offset = moment.astimezone(ZoneInfo(browser_zone)).utcoffset()
    assert offset != moment.astimezone(ZoneInfo(walk.ZONE)).utcoffset()


def test_the_deadlines_sit_mid_day_so_both_stores_read_the_same_reasons() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 14, 23, 59, 30, tzinfo=UTC)
    due = walk.deadlines(now)
    assert due == {
        "p1": "2026-10-25T11:59:00Z",
        "referee": "2026-10-20T11:59:00Z",
        "reply": "2026-10-16T11:59:00Z",
    }
    assert walk.local(due["p1"]) == ("2026-10-25", "14:59")


def test_snapshot_sets_compare_by_content_not_by_raw_id_order() -> None:
    """Regression (int5 attempt 1): detector state sorted by raw random IDs before normalisation
    listed the same rows in different orders in two stores, failing an equal walk at random."""

    class Ids:
        def __init__(self, names: dict[str, str]) -> None:
            self.names = names
            self.due = {"reply": "2026-10-16T11:59:00Z", "referee": "2026-10-20T11:59:00Z"}

    def store_rows(reply: str, referee: str) -> list[list[Any]]:
        rows = [
            ["deadline", f"{reply}|2026-10-16T11:59:00Z|14", False],
            ["deadline", f"{referee}|2026-10-20T11:59:00Z|14", False],
        ]
        return sorted(rows)  # the raw-ID order: differs with the random IDs

    def snap(reply: str, referee: str) -> dict[str, Any]:
        st = Ids({reply: "reply", referee: "referee"})
        raw = {
            "detector_state": store_rows(reply, referee),
            "events": [],
            "approvals": [],
            "runs": [],
            "outputs": {"sources": [], "versions": []},
        }
        return walk.settle(walk.Store.norm(st, raw))  # type: ignore[arg-type]

    # Control: normalised without settling, the two raw orders differ (the defect).
    raw_a = {"detector_state": store_rows("action_aaaa", "action_ffff")}
    raw_b = {"detector_state": store_rows("action_ffff", "action_aaaa")}
    st_a = Ids({"action_aaaa": "reply", "action_ffff": "referee"})
    st_b = Ids({"action_ffff": "reply", "action_aaaa": "referee"})
    assert walk.Store.norm(st_a, raw_a) != walk.Store.norm(st_b, raw_b)  # type: ignore[arg-type]
    a = snap("action_aaaa", "action_ffff")  # reply sorts first by raw ID
    b = snap("action_ffff", "action_aaaa")  # referee sorts first by raw ID
    assert a == b
    assert a["detector_state"][0][1].startswith("<referee>")
