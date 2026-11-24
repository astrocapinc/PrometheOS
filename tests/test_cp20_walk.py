"""C20-WALK (CP20.5; decisions 0106, 0107): the walk harness (tools/walk_cp20.py) and its guards.

The full walk (the hub in a browser and the agent over MCP, then a second fresh store through MCP
alone) is the checkpoint suite's ``walk`` command. Here its judgement is mutation-tested: a store
divergence, a missing step, a different outcome or agent answer, every wrong browser observation
and a passed default that changed a record all fail; the spec and the harness hand over the same
signals and name the same fixture strings; and the answers are normalised for ids and times only.
"""

from __future__ import annotations

import copy
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "hub" / "walk20" / "walk.spec.ts"


def load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("walk_cp20", REPO / "tools" / "walk_cp20.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["walk_cp20"] = module
    spec.loader.exec_module(module)
    return module


walk = load_tool()
LINE = walk.LINE


def legs() -> tuple[dict[str, Any], dict[str, Any]]:
    steps = [{"id": i, "outcome": "ok", "answer_sha256": "a" * 64} for i in walk.SCRIPT_IDS]
    doc = {"hub": {"steps": copy.deepcopy(steps)}, "mcp": {"steps": copy.deepcopy(steps)}}
    outcomes = {
        "hub": {
            "claims": [["x", "proposed", "stale", None, ["producer_changed"], []]],
            "approvals": [["apply", "used", "card"]],
            "outline_confirmations": 1,
        }
    }
    outcomes["mcp"] = copy.deepcopy(outcomes["hub"])
    return doc, outcomes  # fmt: skip


def test_equal_legs_pass_and_each_mutation_fails() -> None:
    doc, outcomes = legs()
    assert walk.judge(doc, outcomes)[1] == []
    doc, outcomes = legs()
    outcomes["mcp"]["outline_confirmations"] = 2  # the outline confirmed twice on one leg
    assert any("stored outcomes" in f for f in walk.judge(doc, outcomes)[1])
    doc, outcomes = legs()
    outcomes["hub"]["claims"][0][2] = "holds"  # a different derived status
    assert any("stored outcomes" in f for f in walk.judge(doc, outcomes)[1])
    doc, outcomes = legs()
    doc["hub"]["steps"] = [s for s in doc["hub"]["steps"] if s["id"] != "S4"]
    assert any("missing" in f for f in walk.judge(doc, outcomes)[1])
    doc, outcomes = legs()
    next(s for s in doc["mcp"]["steps"] if s["id"] == "S6")["outcome"] = "unexpected:x=ok"
    assert any("outcomes differ" in f for f in walk.judge(doc, outcomes)[1])
    doc, outcomes = legs()
    next(s for s in doc["hub"]["steps"] if s["id"] == "S5")["answer_sha256"] = "b" * 64
    assert any("agent answers differ" in f for f in walk.judge(doc, outcomes)[1])


HOLDS = "Holds (your agent's check, not yet reviewed)"
RQ1_CLAIMS = [
    {"line": LINE["track2"], "status": "holds", "text": HOLDS},
    {"line": LINE["track5"], "status": "stale", "text": "Stale: do not quote · The script that "
     "writes its evidence changed since the evidence was written."},
    {"line": LINE["uphill"], "status": "holds", "text": HOLDS},
    {"line": LINE["saving_old"], "status": "settling", "text": "Settling: needs a correction or "
     "your decision · A correction is proposed and waits for your decision."},
    {"line": LINE["saving_new"], "status": "unverified", "text": "Unverified: no check recorded · "
     "No assessment checks this claim yet."},
]  # fmt: skip
GOOD: dict[str, dict[str, Any]] = {
    "S1": {"tracks": ["track2", "track5"], "deliverables": [walk.PAPER],
           "questions": list(walk.GOLDEN["questions"]),
           "stale": ["track5/energy.csv is older than its script track5/run_sim.m"],
           "counts": "Confirming records 2 track(s), 3 question(s) and 1 paper(s): 6 new, 0 "
                     "already recorded, and 1 link(s); 0 left out; 1 question(s) placed nowhere.",
           "confirmed": True, "confirm_buttons": 0},
    "S3": {"stage": "Evidence mapped · next: Verified · paper status: Planning",
           "questions": [{"title": walk.RQ1, "claims": RQ1_CLAIMS}],
           "blockers": [f"{LINE['track5']} · stale", f"{LINE['saving_old']} · settling",
                        f"{LINE['saving_new']} · no assessment"],
           "to_confirm": [LINE["track2"], LINE["uphill"]]},
    "S4": {"why": ["study:track5/run_sim.m, which writes track5/energy.csv, changed after it "
                   "wrote the file."],
           "evidence": "energy.csv · study/track5/energy.csv · line 2 · sha256 3c9115661346 · "
                       "10 bytes", "locator": "text"},
    "S7": {"second_default": "If you don't answer: Nothing is withdrawn. (in effect in views "
                             "since Oct 31, 2026, 06:01 PM)",
           "first_default": "If you don't answer: Until you decide, it reads as settling.",
           "granted": True, "cards_after": [walk.SECOND["what"]],
           "after": {"saving_old": "superseded", "saving_new": "corrected", "track5": "stale"},
           "checks": "Fuel saving re-readpass17.4 %17.4 %0.05 percentage points"},
}  # fmt: skip
EXPECTED = {"sha12": "3c9115661346", "bytes": 10}


def changed(step_id: str, path: str, value: Any) -> dict[str, Any]:
    """GOOD[step_id] with one value replaced (``a/b`` walks into dicts, ``a/0`` into lists)."""
    shown = copy.deepcopy(GOOD[step_id])
    *parents, last = path.split("/")
    node: Any = shown
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return shown


BAD = [
    ("S1", "tracks", ["track2"]), ("S1", "deliverables", []), ("S1", "questions/2", "Other?"),
    ("S1", "stale", []), ("S1", "counts", "Working out what confirming records…"),
    ("S1", "confirmed", False), ("S1", "confirm_buttons", 1),
    ("S3", "questions/0/claims/1/status", "holds"), ("S3", "questions/0/claims/2/status",
                                                      "conflicting"),
    ("S3", "questions/0/claims/0/text", "Holds (reviewed by you)"),
    ("S3", "questions/0/claims/1/text", "Stale: do not quote"),
    ("S3", "questions/0/title", "Another question"), ("S3", "stage", "Framed · next: Evidence"),
    ("S3", "blockers", []), ("S3", "to_confirm", [LINE["track2"]]),
    ("S4", "why/0", "A supporting assessment within its scope."),
    ("S4", "evidence", "energy.csv · study/track5/energy.csv · line 2"),
    ("S4", "evidence", "energy.csv · study/track5/energy.csv · line 2 · sha256 000000000000 · "
                       "10 bytes"),
    ("S7", "second_default", "If you don't answer: Nothing is withdrawn."),
    ("S7", "first_default", "If you don't answer: x (in effect in views since today)"),
    ("S7", "granted", False), ("S7", "after/saving_new", "holds"),
    ("S7", "after/saving_old", "settling"), ("S7", "checks", "Fuel saving re-readpass17.4 %"),
    ("S7", "cards_after", []),
]  # fmt: skip


def test_browser_observations_are_judged() -> None:
    for step_id in walk.BROWSER_IDS:
        assert all(c["passed"] for c in walk.browser_checks(step_id, GOOD[step_id], EXPECTED))
        assert not all(c["passed"] for c in walk.browser_checks(step_id, {}, EXPECTED))


@pytest.mark.parametrize(("step_id", "path", "value"), BAD)
def test_every_wrong_observation_fails(step_id: str, path: str, value: Any) -> None:
    shown = changed(step_id, path, value)
    assert not all(c["passed"] for c in walk.browser_checks(step_id, shown, EXPECTED))


def test_s4_needs_the_stored_digest_to_judge_the_shown_one() -> None:
    assert not all(c["passed"] for c in walk.browser_checks("S4", GOOD["S4"], {}))


class FakeStore:
    """Just enough of a store for the default's checks: one record digest, the bodies and the
    statuses the checks read."""

    def __init__(self, study: Path) -> None:
        self.study = study
        self.folder_before = walk.folder_state(study)
        self.record_digest_before = "d" * 64
        self.digest_now = "d" * 64
        self.ids = {"second": "correction_2", "track5": "claim_5"}
        self.bodies = {"correction_2": {"status": "proposed"}, "claim_5": {"state": "proposed"}}

    def record_digest(self) -> str:
        return self.digest_now

    def body(self, record_id: str) -> dict[str, Any]:
        return self.bodies[record_id]

    def status(self) -> dict[str, dict[str, Any]]:
        return {"track5": {"status": "stale"}}


def cards(second: str = "default_in_effect", first: str = "waiting") -> dict[str, Any]:
    return {"cards": [{"card": {"what": walk.SECOND["what"]}, "default_state": second},
                      {"card": {"what": walk.CARD["what"]}, "default_state": first}]}  # fmt: skip


def test_a_passed_default_must_leave_the_records_and_the_folder_unchanged(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("kWh\n1\n", "utf-8")
    st = FakeStore(tmp_path)
    assert all(c["passed"] for c in walk.s7_default_checks(st, cards()))
    assert not all(c["passed"] for c in walk.s7_default_checks(st, cards(second="waiting")))
    assert not all(
        c["passed"] for c in walk.s7_default_checks(st, cards(first="default_in_effect"))
    )
    st.digest_now = "e" * 64  # a record changed while the default passed
    assert not all(c["passed"] for c in walk.s7_default_checks(st, cards()))
    st.digest_now = st.record_digest_before
    st.bodies["correction_2"] = {"status": "applied"}  # the default applied the plan
    assert not all(c["passed"] for c in walk.s7_default_checks(st, cards()))
    st.bodies["correction_2"] = {"status": "proposed"}
    (tmp_path / "a.csv").write_text("kWh\n2\n", "utf-8")  # something wrote into the folder
    assert not all(c["passed"] for c in walk.s7_default_checks(st, cards()))


def test_the_spec_and_the_harness_cover_the_same_steps_signals_and_fixture() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert re.findall(r'record\(\{\s*id: "(S\d)"', text) == walk.BROWSER_IDS
    harness = (REPO / "tools" / "walk_cp20.py").read_text(encoding="utf-8")
    assert set(re.findall(r'signal\("([a-z-]+)"', text)) == set(
        re.findall(r'leg\.wait\("([a-z-]+)"', harness)
    )
    assert set(re.findall(r'waitFor\("([a-z-]+)"', text)) == set(
        re.findall(r'leg\.send\("([a-z-]+)"', harness)
    )
    assert 'import { guardedTest as test } from "../e2e/boundary-guard";' in text
    constants = dict(re.findall(r'^const ([A-Z0-9]+) = "(.*)";$', text, re.MULTILINE))
    assert constants == {
        "PAPER": walk.PAPER, "RQ1": walk.RQ1, "CLUTCH": walk.CLUTCH, "TRACK5": LINE["track5"],
        "OLD": LINE["saving_old"], "NEW": LINE["saving_new"], "CARD": walk.CARD["what"],
        "SECOND": walk.SECOND["what"],
    }  # fmt: skip


def test_the_fixture_has_every_case_the_criterion_names() -> None:
    golden = walk.GOLDEN
    assert golden["tracks"] == ["track2", "track5"]  # per-track forks
    assert (golden["stale_result"], golden["stale_producer"]) == (
        "track5/energy.csv",
        "track5/run_sim.m",
    )
    path, one, two = golden["definitional_pair"]
    assert path == walk.UP and (one, two) == (3, 4)
    assert golden["corrected"] == (walk.OLD, walk.NEW)
    # Every claim's number is a bound quantity re-read from its line of the fixture.
    for _key, _statement, rel, line, value, *_rest in walk.CLAIMS:
        assert value in walk.text_of(rel).splitlines()[line - 1]


def test_ids_and_times_are_normalised_and_nothing_else() -> None:
    one = {"message": "Approval request apreq_6ef54a30-29d0-44a6-bc64-d6395f944010 waits",
           "id": "claim_6ef54a30-29d0-44a6-bc64-d6395f944010", "at": "2026-10-31T14:42:27Z",
           "project_seq": 4}  # fmt: skip
    two = {"message": "Approval request apreq_f0d8a90f-5bb4-486d-9324-1623a64ca16b waits",
           "id": "claim_f0d8a90f-5bb4-486d-9324-1623a64ca16b", "at": "2026-11-01T09:00:00.5Z",
           "project_seq": 4}  # fmt: skip
    assert walk.normalise(one) == walk.normalise(two)
    assert walk.normalise({**one, "project_seq": 5}) != walk.normalise(one)
    assert walk.normalise({**one, "message": "a different answer"}) != walk.normalise(one)
    # Ties broken by random ids are compared as sets; ordered lists keep their order.
    assert walk.normalise({"tracks": ["track5", "track2"]}) == walk.normalise(
        {"tracks": ["track2", "track5"]}
    )
    assert walk.normalise({"checks": ["b", "a"]}) != walk.normalise({"checks": ["a", "b"]})


def test_a_dirty_tree_still_gets_a_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The papers-1 integration run: its own receipts leave the tree dirty, and the walk died
    hashing the diff before running a step."""
    import hashlib
    import subprocess

    out = {"rev-parse": "abc123\n", "status": "?? development/receipts/CP20/\n", "diff": "+x\n"}

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=out[argv[1]], stderr="")

    monkeypatch.setattr(walk.subprocess, "run", run)
    got = walk.candidate()
    assert got == {
        "commit": "abc123",
        "dirty": True,
        "dirty_diff_sha256": hashlib.sha256(b"+x\n").hexdigest(),
    }
    out["status"] = ""
    assert walk.candidate()["dirty_diff_sha256"] is None
