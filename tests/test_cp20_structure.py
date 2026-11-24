"""CP20.2 (A06.1; C20-STRUCTURE, S62; decision 0083): questions, tracks and deliverable stages.

Track entries and the answers, addresses and draws_on relations record the research map; they
go through the request protocol's relation list and promote nothing. A deliverable's stage is
derived (framed, evidence mapped, verified, then the paper status), each step lists what keeps
the paper from it, and verified lists agent-reported support for the owner to confirm. The same
read is served through MCP and HTTP with equal outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env
from cp7_support import create, paper_body
from hub_support import running_hub
from mcp import Client
from test_cp18_events import store_digest
from test_cp20_claim_status import HUB_OWNER, assess, claim, imported, line_ev

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "result.csv").write_text(
        "name = value\nenergy_MJ = 196.9\nenergy_MJ = 63.6\n", "utf-8"
    )
    return environment


def entry(env: Env, category: str, title: str, *, sensitivity: str = "normal") -> str:
    eid = new_id("entry")
    body = {
        "title": title,
        "text": f"{title}.",
        "category": category,
        "evidence": [],
        "claim_refs": [],
        "author_ref": None,
        "origin": "agent",
        "quantities": [],
        "review": "unreviewed",
    }
    caller = AGENT if sensitivity == "normal" else OWNER
    env.apply([create("entry", body, eid, sensitivity=sensitivity)], caller=caller)
    return eid


def paper(env: Env, title: str = "P1: kinematic limits", **overrides: Any) -> str:
    pid = new_id("paper")
    env.apply([create("paper", paper_body(title=title, status="planning", **overrides), pid)])
    return pid


def link(env: Env, source: str, target: str, relation: str, caller: Any = AGENT) -> None:
    env.apply(
        [
            {
                "op": "relation_add",
                "source_ref": {"id": source, "rev": 1},
                "target_ref": {"id": target, "rev": 1},
                "relation": relation,
            }
        ],
        caller=caller,
    )


def research_map(env: Env, caller: Any = OWNER, **kw: Any) -> dict[str, Any]:
    return env.svc.research_map(env.project_id, caller, **kw)


def deliverable(env: Env, pid: str, caller: Any = OWNER) -> dict[str, Any]:
    found = research_map(env, caller, paper_id=pid)["deliverables"]
    assert len(found) == 1
    return found[0]


def blockers(d: dict[str, Any], step: str) -> list[str]:
    return [b["code"] for s in d["steps"] if s["key"] == step for b in s["blockers"]]


# ----- the relations ------------------------------------------------------------------


def test_an_agent_records_the_map_and_nothing_is_promoted(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    question = entry(env, "question", "Does the sun lock cost energy?")
    track = entry(env, "track", "Track 11: mechanism anchor")
    pid = paper(env)
    for source, target, relation in (
        (cid, question, "answers"),
        (question, pid, "addresses"),
        (cid, track, "draws_on"),
        (question, track, "draws_on"),
    ):
        link(env, source, target, relation)  # no approval asked
    assert env.svc.get(env.project_id, OWNER, cid, None)["body"]["state"] == "proposed"
    got = deliverable(env, pid)
    assert [q["entry_id"] for q in got["questions"]] == [question]
    assert [c["claim_id"] for c in got["questions"][0]["claims"]] == [cid]
    assert got["questions"][0]["tracks"] == [track]
    assert got["questions"][0]["claims"][0]["tracks"] == [track]
    tracks = research_map(env)["tracks"]
    assert tracks == [
        {"entry_id": track, "title": "Track 11: mechanism anchor", "claims": 1, "questions": 1,
         # Decision 0108: none by file location (no confirmed outline names its folders).
         "claims_by_path": 0, "folders": []}
    ]  # fmt: skip


def test_each_relation_end_needs_its_category(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    note = entry(env, "note", "A note")
    question = entry(env, "question", "A question")
    track = entry(env, "track", "A track")
    pid = paper(env)
    for source, target, relation in (
        (cid, note, "answers"),  # answers targets a question
        (note, pid, "addresses"),  # only a question addresses
        (cid, note, "draws_on"),  # draws_on targets a track
        (note, track, "draws_on"),  # an entry draws on a track only when it is a question
        (question, note, "addresses"),  # an entry target must be the objective entry
        (question, cid, "answers"),  # the pair itself: answers goes from a claim
        (pid, question, "addresses"),
    ):
        with pytest.raises(PrometheosError) as err:
            link(env, source, target, relation)
        assert err.value.code == "validation_error", (source, target, relation)


def test_a_question_addresses_the_objective_entry_once_the_owner_names_it(env: Env) -> None:
    objective = entry(env, "decision", "Objective: when does a planetary flywheel pay off?")
    question = entry(env, "question", "Where does the Willis constraint bind?")
    with pytest.raises(PrometheosError):
        link(env, question, objective, "addresses")
    project = env.svc.get(env.project_id, OWNER, env.project_id, None)
    settings = dict(project["body"]) | {"objective_entry_ref": {"id": objective, "rev": 1}}
    env.apply_protected(
        [{"op": "project_update", "base_rev": project["rev"], "settings": settings}],
        caller=HUB_OWNER,
    )
    link(env, question, objective, "addresses")
    got = research_map(env)
    assert got["objective"]["entry_id"] == objective
    assert [q["entry_id"] for q in got["objective"]["questions"]] == [question]
    assert got["unplaced_questions"] == []


# ----- stages -------------------------------------------------------------------------


def test_stages_are_derived_in_order_and_name_what_keeps_the_paper_back(env: Env) -> None:
    files = imported(env, ["result.csv"])
    pid = paper(env)
    first = deliverable(env, pid)
    assert first["stage"] is None and first["next"] == "framed"
    assert blockers(first, "framed") == ["no_question"]
    # Vacuously nothing blocks "verified" yet, but the steps are cumulative.
    assert [s["reached"] for s in first["steps"]] == [False, False, False]
    assert first["headline"] == "Not framed: no question addresses this paper yet."

    question = entry(env, "question", "Does the lock cost energy?")
    link(env, question, pid, "addresses")
    framed = deliverable(env, pid)
    assert (framed["stage"], framed["next"]) == ("framed", "evidence_mapped")
    assert blockers(framed, "evidence_mapped") == ["unanswered"]

    bare = claim(env, [])
    link(env, bare, question, "answers")
    assert blockers(deliverable(env, pid), "evidence_mapped") == ["no_evidence"]

    cid = claim(env, [line_ev(files["result.csv"], 2)])
    link(env, cid, question, "answers")
    # Withdraw the bare claim by an applied correction: out of use, it no longer blocks.
    correction = {
        "target_refs": [{"id": bare, "rev": 1}],
        "replacement_refs": [],
        "reason": "It cited nothing.",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": bare, "rev": 1},
                "effect": "withdraw",
                "replacement_ref": None,
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    corr = new_id("correction")
    env.apply([create("correction", correction, corr)], caller=AGENT)
    env.apply_protected([{"op": "correction_apply", "id": corr, "base_rev": 1}])
    mapped = deliverable(env, pid)
    assert (mapped["stage"], mapped["next"]) == ("evidence_mapped", "verified")
    assert blockers(mapped, "verified") == ["unverified"]
    assert mapped["steps"][2]["blockers"][0]["claim"] == cid
    assert mapped["headline"] == "1 of 1 claim under 1 question still to verify."
    statuses = [c["status"] for c in mapped["questions"][0]["claims"]]
    assert sorted(statuses) == ["unverified", "withdrawn"]  # shown, not counted

    assess(env, cid)
    verified = deliverable(env, pid)
    assert (verified["stage"], verified["next"]) == ("verified", None)
    assert verified["paper_status"] == "planning"
    assert verified["to_confirm"] == [
        {"claim_id": cid, "line": "The uphill energy is about 196.9 MJ on the stored cycle."}
    ]
    assert verified["headline"] == "Verified; 1 claim rests on your agent's check alone."


def test_a_paper_claim_not_under_a_question_is_listed_apart(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    pid = paper(env, claim_refs=[{"id": cid, "rev": 1}])
    got = deliverable(env, pid)
    assert got["questions"] == []
    assert [c["claim_id"] for c in got["other_claims"]] == [cid]
    assert got["health"]["unverified"] == 1


def test_the_claim_line_is_its_summary_or_one_cut_line_and_details_hold_the_evidence(
    env: Env,
) -> None:
    files = imported(env, ["result.csv"])
    long = "The uphill energy " + "is very nearly " * 12 + "196.9 MJ."
    cid = claim(env, [line_ev(files["result.csv"], 2)], statement=long)
    question = entry(env, "question", "Q")
    pid = paper(env)
    link(env, cid, question, "answers")
    link(env, question, pid, "addresses")
    item = deliverable(env, pid)["questions"][0]["claims"][0]
    assert len(item["line"]) == 120 and item["line"].endswith("…")
    assert "\n" not in item["line"]
    [evidence] = item["details"]["evidence"]
    assert evidence["path"] == "result.csv" and evidence["root_alias"] == "research"
    assert evidence["sha256"] == files["result.csv"]["sha256"]
    assert item["details"]["statement"] == long


# ----- visibility, purity and equal outcomes --------------------------------------------


def test_hidden_records_never_shape_what_an_agent_reads(env: Env) -> None:
    files = imported(env, ["result.csv"])
    pid = paper(env)
    visible = entry(env, "question", "Visible question")
    hidden = entry(env, "question", "SECRET question", sensitivity="restricted")
    track = entry(env, "track", "SECRET track", sensitivity="restricted")
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    link(env, visible, pid, "addresses")
    link(env, hidden, pid, "addresses", caller=OWNER)
    link(env, cid, visible, "answers")
    link(env, cid, track, "draws_on", caller=OWNER)
    agent = research_map(env, AGENT)
    assert "SECRET" not in json.dumps(agent)
    [d] = agent["deliverables"]
    assert [q["entry_id"] for q in d["questions"]] == [visible]
    assert d["questions"][0]["claims"][0]["tracks"] == []
    owner = deliverable(env, pid, OWNER)
    assert {q["entry_id"] for q in owner["questions"]} == {visible, hidden}
    # The hidden question has no claim: it keeps the owner's view from evidence_mapped only.
    assert blockers(owner, "evidence_mapped") == ["unanswered"]
    assert blockers(d, "evidence_mapped") == []


def test_the_map_changes_nothing_and_mcp_and_http_give_the_same_outcome(env: Env) -> None:
    files = imported(env, ["result.csv"])
    pid = paper(env)
    question = entry(env, "question", "Q1")
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    link(env, question, pid, "addresses")
    link(env, cid, question, "answers")
    assess(env, cid)
    before = store_digest(env.store_dir)
    service = research_map(env, AGENT)

    async def main() -> dict[str, Any]:
        async with Client(build_server(env.svc, env.project_id)) as client:
            reply = await client.call_tool("research_map", {})
            assert not reply.is_error, reply
            return dict(reply.structured_content or {})

    via_mcp = anyio.run(main)
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        reply = hub.request("GET", "/api/v1/knowledge/research-map")
        assert reply.status == 200, reply.body
        via_http = reply.json()
        one = hub.request("GET", f"/api/v1/knowledge/research-map?paper_id={pid}").json()
        missing = hub.request("GET", "/api/v1/knowledge/research-map?paper_id=paper_nope")
    assert store_digest(env.store_dir) == before
    for key in ("objective", "deliverables", "unplaced_questions", "tracks"):
        assert via_mcp[key] == service[key] == via_http[key], key
    assert one["deliverables"] == service["deliverables"]
    assert missing.status == 404
    assert env.svc.get(env.project_id, OWNER, cid, None)["body"]["state"] == "proposed"


# ----- review cp20-2-a regressions ------------------------------------------------------


def revise_entry(env: Env, eid: str, **changes: Any) -> None:
    current = env.svc.get(env.project_id, OWNER, eid, None)
    sensitivity = changes.pop("sensitivity", current["sensitivity"])
    env.apply_protected(
        [
            {
                "op": "revise",
                "id": eid,
                "base_rev": current["rev"],
                "sensitivity": sensitivity,
                "occurred_at": None,
                "body": current["body"] | changes,
            }
        ]
    )


def supersede(env: Env, old: str, new: str) -> None:
    corr = new_id("correction")
    correction = {
        "target_refs": [{"id": old, "rev": 1}],
        "replacement_refs": [{"id": new, "rev": 1}],
        "reason": "The published climb is 63.6 MJ.",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": old, "rev": 1},
                "effect": "supersede",
                "replacement_ref": {"id": new, "rev": 1},
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    env.apply([create("correction", correction, corr)], caller=AGENT)
    env.apply_protected([{"op": "correction_apply", "id": corr, "base_rev": 1}])


def test_question_and_track_categories_are_fixed_at_creation(env: Env) -> None:
    """F1: an agent cannot drop or add a question (or a track) by revising its category."""
    question = entry(env, "question", "Q")
    note = entry(env, "note", "N")
    track = entry(env, "track", "T")
    for eid, to in ((question, "note"), (note, "question"), (track, "note"), (note, "track")):
        current = env.svc.get(env.project_id, OWNER, eid, None)
        with pytest.raises(PrometheosError) as err:
            env.apply(
                [
                    {
                        "op": "revise",
                        "id": eid,
                        "base_rev": current["rev"],
                        "sensitivity": "normal",
                        "occurred_at": None,
                        "body": current["body"] | {"category": to},
                    }
                ],
                caller=AGENT,
            )
        assert err.value.code == "policy_denied", (eid, to)
        assert "fixed at creation" in err.value.message


def test_a_replacement_answers_the_superseded_claims_question(env: Env) -> None:
    """F3 (and F10: superseded is out of use, corrected counts as verified)."""
    files = imported(env, ["result.csv"])
    pid = paper(env)
    question = entry(env, "question", "Is the mine haul a boundary case?")
    old = claim(env, [line_ev(files["result.csv"], 2)])
    new = claim(env, [line_ev(files["result.csv"], 3)])
    link(env, question, pid, "addresses")
    link(env, old, question, "answers")
    track = entry(env, "track", "T6")
    link(env, old, track, "draws_on")
    assess(env, old)
    supersede(env, old, new)
    assess(env, new)
    got = deliverable(env, pid)
    claims = {c["claim_id"]: c for c in got["questions"][0]["claims"]}
    assert claims[old]["status"] == "superseded"
    assert (claims[new]["status"], claims[new]["replaces"]) == ("corrected", old)
    assert (got["stage"], got["next"]) == ("verified", None)
    assert [c["claim_id"] for c in got["to_confirm"]] == [new]
    # The track no longer counts the superseded claim.
    assert research_map(env)["tracks"][0]["claims"] == 0


def test_the_map_relations_spread_no_sensitivity(env: Env) -> None:
    """F4: restricting a question or a track does not restrict the claims linked to it."""
    files = imported(env, ["result.csv"])
    question = entry(env, "question", "Q")
    track = entry(env, "track", "T")
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    link(env, cid, question, "answers")
    link(env, cid, track, "draws_on")
    revise_entry(env, question, sensitivity="restricted")
    revise_entry(env, track, sensitivity="restricted")
    assert env.svc.get(env.project_id, OWNER, cid, None)["sensitivity"] == "normal"
    seen = env.svc.claim_status(env.project_id, AGENT)["claims"]
    assert [c["claim_id"] for c in seen] == [cid]


def test_verified_names_the_papers_claims_outside_its_questions(env: Env) -> None:
    """F5: "verified" never hides an unverified claim the paper cites outside its questions."""
    files = imported(env, ["result.csv"])
    held = claim(env, [line_ev(files["result.csv"], 2)])
    loose = claim(env, [line_ev(files["result.csv"], 3)])
    pid = paper(env, claim_refs=[{"id": held, "rev": 1}, {"id": loose, "rev": 1}])
    question = entry(env, "question", "Q")
    link(env, question, pid, "addresses")
    link(env, held, question, "answers")
    assess(env, held)
    got = deliverable(env, pid)
    assert got["next"] is None
    assert got["headline"] == (
        "Verified under its questions; 1 of the paper's claims is outside them "
        "and not yet verified."
    )


def test_links_join_active_records_only(env: Env) -> None:
    """F7: a link to an archived record would never show and could not be removed."""
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    question = entry(env, "question", "Q")
    env.apply_protected([{"op": "archive", "id": question, "base_rev": 1}])
    with pytest.raises(PrometheosError) as err:
        link(env, cid, question, "answers")
    assert err.value.code == "validation_error"


def test_an_owner_reviewed_claim_is_not_listed_to_confirm(env: Env) -> None:
    """F10: to_confirm lists only claims resting on an agent's check."""
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    pid = paper(env)
    question = entry(env, "question", "Q")
    link(env, question, pid, "addresses")
    link(env, cid, question, "answers")
    body = {
        "claim_ref": {"id": cid, "rev": 1},
        "outcome": "supports",
        "rationale": "I re-read it.",
        "checks": [],
        "author_ref": None,
        "method": "researcher",
        "scope_match": True,
    }
    env.apply_protected([create("assessment", body, new_id("assessment"))], caller=HUB_OWNER)
    got = deliverable(env, pid)
    assert got["next"] is None and got["to_confirm"] == []
    assert got["headline"] == "Verified: every claim under its questions holds."


def test_a_stale_or_missing_artefact_blocks_and_names_no_file(env: Env) -> None:
    """F9 and F10: a stale claim blocks "verified" with a reason that names no file; tampered
    evidence blocks "evidence mapped"; the details show the computed availability (F6)."""
    import sqlite3
    from contextlib import closing

    from test_cp20_claim_status import receipt

    (env.research / "script.m").write_text("energy = 196.9;\n", "utf-8")
    files = imported(env, ["script.m", "result.csv"])
    receipt(env, [files["script.m"]], [files["result.csv"]])
    pid = paper(env)
    question = entry(env, "question", "Q")
    link(env, question, pid, "addresses")
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    link(env, cid, question, "answers")
    assess(env, cid)
    (env.research / "script.m").write_text("energy = 63.6;\n", "utf-8")
    imported(env, ["script.m"])
    got = deliverable(env, pid)
    [blocked] = got["steps"][2]["blockers"]
    assert blocked["code"] == "stale"
    assert "script.m" not in json.dumps(blocked)
    item = got["questions"][0]["claims"][0]
    assert "script.m" not in item["reason"]
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn, conn:
        conn.execute(
            "UPDATE payloads SET data = ? WHERE sha256 = ?",
            (b"tampered", files["result.csv"]["sha256"]),
        )
    tampered = deliverable(env, pid)
    assert "evidence_missing" in blockers(tampered, "evidence_mapped")
    evidence = tampered["questions"][0]["claims"][0]["details"]["evidence"][0]
    assert evidence["availability"] != "retained"


def test_an_unknown_or_hidden_paper_is_not_found_alike(env: Env) -> None:
    """F8 and F10: the agent cannot tell a hidden paper from a missing one, through MCP too."""
    hidden = new_id("paper")
    env.apply(
        [create("paper", paper_body(title="SECRET paper"), hidden, sensitivity="restricted")],
        caller=OWNER,
    )
    question = entry(env, "question", "Visible question")
    link(env, question, hidden, "addresses", caller=OWNER)
    errors = []
    for pid in (hidden, new_id("paper")):
        with pytest.raises(PrometheosError) as err:
            research_map(env, AGENT, paper_id=pid)
        errors.append((err.value.code, err.value.message))
    assert errors[0] == errors[1] == ("not_found", "paper not found")
    agent = research_map(env, AGENT)
    assert "SECRET" not in json.dumps(agent) and agent["deliverables"] == []

    async def main() -> bool:
        async with Client(build_server(env.svc, env.project_id)) as client:
            reply = await client.call_tool("research_map", {"paper_id": hidden})
            return bool(reply.is_error) and "not_found" in str(reply.content)

    assert anyio.run(main)


def test_a_correction_closure_stops_at_the_question_and_links_hide_restricted_ends(
    env: Env,
) -> None:
    """Review r2 R2-3 and r3 R3-1: a question citing a corrected claim is flagged, but the
    claims answering that question are not (the map relations stay out of the closure); and
    knowledge links never shows an agent a relation whose other end it may not see."""
    import sqlite3
    from contextlib import closing

    files = imported(env, ["result.csv"])
    corrected = claim(env, [line_ev(files["result.csv"], 2)])
    other = claim(env, [line_ev(files["result.csv"], 3)])
    question = new_id("entry")
    body = {
        "title": "Q",
        "text": "Q.",
        "category": "question",
        "evidence": [],
        "claim_refs": [{"id": corrected, "rev": 1}],
        "author_ref": None,
        "origin": "agent",
        "quantities": [],
        "review": "unreviewed",
    }
    env.apply([create("entry", body, question)], caller=AGENT)
    link(env, other, question, "answers")
    corr = new_id("correction")
    correction = {
        "target_refs": [{"id": corrected, "rev": 1}],
        "replacement_refs": [],
        "reason": "Withdrawn.",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": corrected, "rev": 1},
                "effect": "withdraw",
                "replacement_ref": None,
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    env.apply([create("correction", correction, corr)], caller=AGENT)
    env.apply_protected([{"op": "correction_apply", "id": corr, "base_rev": 1}])
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:

        def flagged(record_id: str) -> int:
            row = conn.execute("SELECT COUNT(*) FROM view_flags WHERE record_id = ?", (record_id,))
            return int(row.fetchone()[0])

        assert flagged(question) > 0
        assert flagged(other) == 0
    assert [r["relation"] for r in env.svc.links(env.project_id, AGENT, other)["relations"]] == [
        "answers"
    ]
    revise_entry(env, question, sensitivity="restricted")
    assert env.svc.links(env.project_id, AGENT, other)["relations"] == []
    assert len(env.svc.links(env.project_id, OWNER, other)["relations"]) == 1
