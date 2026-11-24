"""The research compass (decision 0113): ranked directions from the record, each with why, what
to do and when it is done; deterministic, visibility-checked and pure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env, entry_body
from cp7_support import create, paper_body
from test_cp20_claim_status import SCOPE, imported, line_ev, receipt

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    (e.research / "result.csv").write_text("energy_MJ = 196.9\n", "utf-8")
    (e.research / "run.log").write_text("done\n", "utf-8")
    return e


def _claim(env: Env, item: dict[str, str], sensitivity: str = "normal") -> str:
    cid = new_id("claim")
    body = {
        "statement": "The uphill energy is 196.9 MJ on the stored cycle.",
        "scope": SCOPE,
        "evidence": [line_ev(item, 1)],
        "assessment_refs": [],
        "state": "proposed",
        "superseded_by": None,
    }
    caller = OWNER if sensitivity != "normal" else AGENT
    env.apply([create("claim", body, cid, sensitivity=sensitivity)], caller=caller)
    return cid


def _paper(env: Env, claims: list[str], **extra: Any) -> str:
    pid = new_id("paper")
    refs = [{"id": c, "rev": 1} for c in claims]
    env.apply([create("paper", paper_body(claim_refs=refs, **extra), pid)])
    return pid


def _compass(env: Env, caller: Any = AGENT, **kw: Any) -> dict[str, Any]:
    result: dict[str, Any] = env.svc.research_compass(env.project_id, caller, **kw)
    return result


def test_directions_come_in_the_fixed_order_with_their_records(env: Env) -> None:
    files = imported(env, ["result.csv", "run.log"])
    cid = _claim(env, files["result.csv"])
    pid = _paper(env, [cid], contribution="A predictor-free hold captures most of the benefit.")
    receipt(env, [], [files["run.log"]])
    got = _compass(env)
    kinds = [d["kind"] for d in got["directions"]]
    assert kinds == sorted(kinds, key=["interpret", "resolve", "frame", "verify", "rerun",
                                       "trace", "answer", "novelty"].index)  # fmt: skip
    assert kinds[0] == "interpret"
    frame = next(d for d in got["directions"] if d["kind"] == "frame" and d["paper_id"] == pid)
    assert frame["skill"] == "frame-paper" and "Not framed" in frame["why"]
    verify = next(d for d in got["directions"] if d["kind"] == "verify")
    assert verify["refs"] == [cid] and verify["paper_id"] == pid
    novelty = next(d for d in got["directions"] if d["kind"] == "novelty")
    assert "No novelty check is recorded" in novelty["why"]
    assert "predictor-free hold" in novelty["why"] and pid in novelty["do"]
    assert got["generation"] == "deterministic"


def _note(env: Env, title: str, text: str, sensitivity: str = "normal") -> None:
    body = entry_body(title=title, text=text)
    body["category"] = "finding"
    caller = OWNER if sensitivity != "normal" else AGENT
    env.apply([create("entry", body, sensitivity=sensitivity)], caller=caller)


def _novelty_for(env: Env, pid: str, caller: Any = AGENT) -> list[dict[str, Any]]:
    return [
        d
        for d in _compass(env, caller, limit=50)["directions"]
        if d["kind"] == "novelty" and d["paper_id"] == pid
    ]


def test_a_recent_novelty_note_rests_the_direction(env: Env) -> None:
    pid = _paper(env, [])
    # Look-alike titles do not count (review frame-rerun-a R2-A2).
    _note(env, "Novelty checklist (todo)", f"Has NOT checked {pid} yet.")
    [novelty] = _novelty_for(env, pid)
    assert "No novelty check is recorded" in novelty["why"]
    # A hidden note does not count for an agent, but does for the owner.
    _note(env, "Novelty check: hidden", f"Searched for {pid}.", sensitivity="restricted")
    assert _novelty_for(env, pid) and not _novelty_for(env, pid, OWNER)
    # A visible note recorded today rests the direction for 30 days (R2-A4).
    _note(env, "Novelty check: the paper", f"Searched for {pid}: nothing new.")
    assert _novelty_for(env, pid) == []


def test_trace_names_hand_macros_and_answer_directions(env: Env) -> None:
    """R2-F1: a hand-written macro alone still names where it is defined."""
    (env.research / "paper.tex").write_text(
        "\\documentclass{article}\n\\newcommand{\\Peak}{425}\n\\begin{document}\n"
        "Peak \\Peak~kJ.\n\\end{document}\n",
        "utf-8",
    )
    files = imported(env, ["paper.tex"])
    pid = _paper(env, [], manuscript_source_id=files["paper.tex"]["source_id"])
    question = new_id("entry")
    env.apply(
        [create("entry", entry_body(title="Does the hold pay?", category="question"), question)],
        caller=AGENT,
    )
    env.apply(
        [
            {
                "op": "relation_add",
                "source_ref": {"id": question, "rev": 1},
                "target_ref": {"id": pid, "rev": 1},
                "relation": "addresses",
            }
        ],
        caller=AGENT,
    )
    directions = _compass(env, limit=50)["directions"]
    [trace] = [d for d in directions if d["kind"] == "trace"]
    assert "\\Peak = 425 (defined at paper.tex:2" in trace["why"]
    [answer] = [d for d in directions if d["kind"] == "answer"]
    assert answer["refs"] == [question] and "\u201cDoes the hold pay?\u201d" in answer["title"]


def test_record_text_is_quoted_in_directions(env: Env) -> None:
    files = imported(env, ["run.log"])
    (env.research / "run.log").write_text("again\n", "utf-8")
    receipt(env, [], [files["run.log"]])
    [interpret] = [d for d in _compass(env)["directions"] if d["kind"] == "interpret"]
    assert interpret["title"].startswith("Read the result \u201c")


def test_limit_counts_and_paper_filter(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = _claim(env, files["result.csv"])
    p1 = _paper(env, [cid])
    p2 = _paper(env, [])
    got = _compass(env, limit=2)
    assert len(got["directions"]) == 2 and got["truncated"] and got["total"] > 2
    only = _compass(env, paper_id=p2)
    assert {d["paper_id"] for d in only["directions"]} == {p2}
    assert all(d["kind"] != "interpret" for d in only["directions"])
    assert p1  # the other paper exists but is filtered out


def test_hidden_records_raise_no_direction_for_an_agent(env: Env) -> None:
    files = imported(env, ["result.csv"])
    hidden = _claim(env, files["result.csv"], sensitivity="restricted")
    pid = new_id("paper")
    refs = [{"id": hidden, "rev": 1}]
    env.apply(
        [create("paper", paper_body(claim_refs=refs), pid, sensitivity="restricted")],
        caller=OWNER,
    )
    agent = json.dumps(_compass(env, limit=50))
    assert hidden not in agent and pid not in agent
    assert pid in json.dumps(_compass(env, OWNER, limit=50))
    with pytest.raises(PrometheosError):
        _compass(env, paper_id=pid)


def test_the_read_changes_nothing(env: Env) -> None:
    from test_cp18_events import store_digest

    files = imported(env, ["result.csv"])
    _paper(env, [_claim(env, files["result.csv"])])
    before = store_digest(env.store_dir)
    _compass(env)
    assert store_digest(env.store_dir) == before
