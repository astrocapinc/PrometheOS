"""CP20.3 (A06.1; C20-VERIFY, S63, S64; decision 0084): planned checks, the owner's read-only
verification budget, decision cards and "apply all recommended".

The detector proposes one check per uncertain claim and cause, cheapest first. An agent's start
of a budget-eligible read-only check needs no approval; every other start answers
approval_required. A decision card travels in a request; its default changes only what views
say. "Apply all recommended" is one owner approval applied in one transaction, or not at all.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from cp7_support import create
from mcp import Client
from test_cp18_events import store_digest
from test_cp20_claim_status import HUB_OWNER, assess, claim, imported, line_ev, receipt

from prometheos.app import verification
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "script.m").write_text("energy = 196.9;\n", "utf-8")
    (environment.research / "result.csv").write_text(
        "name = value\nenergy_MJ = 196.9\nenergy_MJ = 63.6\n", "utf-8"
    )
    return environment


def plan(env: Env, caller: Any = AGENT) -> dict[str, Any]:
    return env.svc.verification_plan(env.project_id, caller)


def checks_of(env: Env, cid: str) -> list[dict[str, Any]]:
    return [c for c in plan(env, OWNER)["checks"] if c["claim_id"] == cid]


def transition(env: Env, action_id: str, to: str, caller: Any = AGENT) -> dict[str, Any]:
    current = env.svc.get(env.project_id, OWNER, action_id, None)
    return env.apply(
        [
            {
                "op": "transition",
                "id": action_id,
                "base_rev": current["rev"],
                "to": to,
                "reason": "Checking it now.",
                "evidence": [],
            }
        ],
        caller=caller,
    )


def refused(fn: Any) -> PrometheosError:
    with pytest.raises(PrometheosError) as err:
        fn()
    return err.value


def set_budget(env: Env, **budget: Any) -> None:
    project = env.svc.get(env.project_id, OWNER, env.project_id, None)
    settings = dict(project["body"]) | {"verification_budget": budget}
    env.apply_protected(
        [{"op": "project_update", "base_rev": project["rev"], "settings": settings}],
        caller=HUB_OWNER,
    )


def action_with_check(env: Env, cid: str, kind: str = "read_only", minutes: int | None = 5) -> str:
    aid = new_id("action")
    body = {
        "title": "Check it myself",
        "reason": "The agent wants to re-read it.",
        "source_refs": [],
        "owner_ref": None,
        "due_at": None,
        "dependencies": [],
        "status": "proposed",
        "blocker": None,
        "completion_evidence": [],
        "commitment_item_id": None,
        "verification": {
            "claim_ref": {"id": cid, "rev": 1},
            "kind": kind,
            "estimated_minutes": minutes,
            "expected": None,
        },
    }
    env.apply([create("action", body, aid)], caller=AGENT)
    return aid


# ----- the detector and the plan ---------------------------------------------------------


def test_the_detector_proposes_one_check_per_claim_and_cause_cheapest_first(env: Env) -> None:
    files = imported(env, ["script.m", "result.csv"])
    receipt(env, [files["script.m"]], [files["result.csv"]])
    unverified = claim(env, [line_ev(files["result.csv"], 2)])
    stale = claim(env, [line_ev(files["result.csv"], 3)])
    assess(env, stale)
    (env.research / "script.m").write_text("energy = 63.6;\n", "utf-8")
    imported(env, ["script.m"])
    first = env.svc.verification_propose(env.project_id, AGENT)
    # The unverified claim's evidence also has a newer producer: one check per cause.
    assert len(first["created"]) == 3
    again = env.svc.verification_propose(env.project_id, AGENT)
    assert again["created"] == []  # deduplicated per claim and cause
    [read] = [c for c in checks_of(env, unverified) if c["cause"] == "no_assessment"]
    assert (read["kind"], read["estimated_minutes"], read["cause"]) == (
        "read_only",
        10,
        "no_assessment",
    )
    assert read["planned_by"] == "prometheos" and read["eligible"] is True
    [rerun] = checks_of(env, stale)
    # A rerun takes the producing run's recorded time (11 s -> 1 minute) and needs the owner.
    assert (rerun["kind"], rerun["estimated_minutes"], rerun["cause"]) == (
        "rerun",
        1,
        "producer_newer",
    )
    assert rerun["eligible"] is False and rerun["eligibility"] == "not_read_only"
    ordered = [c["estimated_minutes"] for c in plan(env, OWNER)["checks"]]
    assert ordered == sorted(ordered)
    # Nothing was started: every check is still proposed, and no claim was promoted.
    assert {c["status"] for c in plan(env, OWNER)["checks"]} == {"proposed"}
    assert env.svc.get(env.project_id, OWNER, unverified, None)["body"]["state"] == "proposed"


def test_a_check_the_owner_cancelled_is_not_proposed_again(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    [created] = env.svc.verification_propose(env.project_id, AGENT)["created"]
    transition(env, created, "cancelled", caller=OWNER)
    assert env.svc.verification_propose(env.project_id, AGENT)["created"] == []
    assert checks_of(env, cid) == []


def test_an_agent_pass_never_proposes_a_check_of_a_hidden_claim(env: Env) -> None:
    files = imported(env, ["result.csv"])
    hidden = new_id("claim")
    body = {
        "statement": "A SECRET restricted claim.",
        "scope": {
            "system": "s",
            "operating_regime": "r",
            "comparator": "c",
            "model_fidelity": "m",
            "validation_level": "v",
        },
        "evidence": [line_ev(files["result.csv"], 2)],
        "assessment_refs": [],
        "state": "proposed",
        "superseded_by": None,
    }
    env.apply([create("claim", body, hidden, sensitivity="restricted")], caller=OWNER)
    assert env.svc.verification_propose(env.project_id, AGENT)["created"] == []
    assert "SECRET" not in json.dumps(plan(env, AGENT))
    [owner_made] = env.svc.verification_propose(env.project_id, OWNER)["created"]
    assert env.svc.get(env.project_id, OWNER, owner_made, None)["sensitivity"] == "restricted"
    assert plan(env, AGENT)["checks"] == []


# ----- the budget at the start transition -------------------------------------------------


def test_an_agent_starts_a_planned_read_only_check_without_an_approval(env: Env) -> None:
    files = imported(env, ["result.csv"])
    claim(env, [line_ev(files["result.csv"], 2)])
    [check] = env.svc.verification_propose(env.project_id, AGENT)["created"]
    transition(env, check, "in_progress")  # no approval asked
    assert env.svc.get(env.project_id, OWNER, check, None)["body"]["status"] == "in_progress"
    budget = plan(env)["budget"]
    assert (budget["used_today"], budget["remaining_today"], budget["default"]) == (10, 50, True)


def test_every_other_start_answers_approval_required(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    mine = action_with_check(env, cid)  # agent-created: not planned by PrometheOS
    assert refused(lambda: transition(env, mine, "in_progress")).code == "approval_required"
    rerun = action_with_check(env, cid, kind="rerun", minutes=3)
    assert refused(lambda: transition(env, rerun, "in_progress")).code == "approval_required"
    unestimated = action_with_check(env, cid, minutes=None)
    assert refused(lambda: transition(env, unestimated, "in_progress")).code == "approval_required"
    # An agent cannot adopt one either (proposed -> ready is the owner's).
    assert refused(lambda: transition(env, mine, "ready")).code == "approval_required"
    # The owner adopts it; then the agent may start it within the budget.
    current = env.svc.get(env.project_id, OWNER, mine, None)
    env.apply_protected(
        [
            {
                "op": "transition",
                "id": mine,
                "base_rev": current["rev"],
                "to": "ready",
                "reason": "Adopted.",
                "evidence": [],
            }
        ],
        caller=HUB_OWNER,
    )
    transition(env, mine, "in_progress")


def test_an_agents_revision_of_a_planned_check_makes_it_need_the_owner(env: Env) -> None:
    files = imported(env, ["result.csv"])
    claim(env, [line_ev(files["result.csv"], 2)])
    [check] = env.svc.verification_propose(env.project_id, AGENT)["created"]
    current = env.svc.get(env.project_id, OWNER, check, None)
    body = current["body"] | {
        "verification": current["body"]["verification"] | {"estimated_minutes": 1}
    }
    env.apply(
        [
            {
                "op": "revise",
                "id": check,
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": body,
            }
        ],
        caller=AGENT,
    )
    [item] = [c for c in plan(env)["checks"] if c["action_id"] == check]
    assert (item["eligible"], item["eligibility"]) == (False, "not_adopted")
    assert refused(lambda: transition(env, check, "in_progress")).code == "approval_required"


def test_the_budget_limits_each_check_and_the_day(env: Env) -> None:
    files = imported(env, ["result.csv"])
    for line in (2, 3):
        claim(env, [line_ev(files["result.csv"], line)])
    first, second = env.svc.verification_propose(env.project_id, AGENT)["created"]
    set_budget(env, max_minutes_each=5, max_minutes_per_day=15)
    item = next(c for c in plan(env)["checks"] if c["action_id"] == first)
    assert (item["eligible"], item["eligibility"]) == (False, "over_each")
    set_budget(env, max_minutes_each=10, max_minutes_per_day=15)
    transition(env, first, "in_progress")
    assert refused(lambda: transition(env, second, "in_progress")).code == "approval_required"
    assert plan(env)["budget"]["used_today"] == 10


def test_the_day_is_the_projects_calendar_day(env: Env) -> None:
    files = imported(env, ["result.csv"])
    claim(env, [line_ev(files["result.csv"], 2)])
    [check] = env.svc.verification_propose(env.project_id, AGENT)["created"]
    transition(env, check, "in_progress")
    store = env.svc.open(env.project_id)
    with closing(store.connect(readonly=True)) as conn:
        now = datetime.now(UTC)
        assert verification.used_today(conn, now) == 10
        assert verification.used_today(conn, now + timedelta(days=1)) == 0


def test_an_ordinary_work_item_still_starts_only_from_ready(env: Env) -> None:
    aid = new_id("action")
    body = {
        "title": "Draft the abstract",
        "reason": "Owner's task.",
        "source_refs": [],
        "owner_ref": None,
        "due_at": None,
        "dependencies": [],
        "status": "proposed",
        "blocker": None,
        "completion_evidence": [],
        "commitment_item_id": None,
    }
    env.apply([create("action", body, aid)], caller=AGENT)
    assert refused(lambda: transition(env, aid, "in_progress")).code == "validation_error"


def test_prometheos_starts_no_agent_and_no_simulation(env: Env, monkeypatch: Any) -> None:
    """Static: the modules import nothing that runs a process. Runtime: proposing, planning
    and an agent's start spawn nothing."""
    import subprocess

    from prometheos.app import cards

    for module in (verification, cards):
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "Popen", "spawn", "asyncio.create_subprocess"):
            assert forbidden not in source, (module.__name__, forbidden)

    def no_process(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("PrometheOS started a process")

    files = imported(env, ["result.csv"])
    claim(env, [line_ev(files["result.csv"], 2)])
    monkeypatch.setattr(subprocess, "Popen", no_process)
    [check] = env.svc.verification_propose(env.project_id, AGENT)["created"]
    plan(env)
    transition(env, check, "in_progress")


# ----- decision cards -------------------------------------------------------------------


def card_request(env: Env, ops: list[dict[str, Any]], **card: Any) -> str:
    """An agent's request with a card; it needs the owner, so it waits as an approval."""
    raw = json.dumps(
        {
            "schema_version": 1,
            "project_id": env.project_id,
            "request_id": new_request_id(),
            "expected_seq": env.seq(),
            "operations": ops,
            "card": {
                "what": "Mine haul: the headline survives, only the number changes.",
                "why": "R1 recomputed the uphill demand on the published route: 63.6 MJ.",
                "recommendation": "Quote 0.89 MJ against 63.6 MJ and keep 'negligible'.",
                "default_text": "Views show the corrected number until you decide.",
            }
            | card,
        }
    ).encode("utf-8")
    err = refused(lambda: env.svc.apply(env.project_id, raw, AGENT))
    assert err.code == "approval_required"
    return str(err.details["approval_request_id"])


def withdraw_correction(env: Env, cid: str) -> str:
    corr = new_id("correction")
    correction = {
        "target_refs": [{"id": cid, "rev": 1}],
        "replacement_refs": [],
        "reason": "The stored cycle applied the steepest grade everywhere.",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": cid, "rev": 1},
                "effect": "withdraw",
                "replacement_ref": None,
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    env.apply([create("correction", correction, corr)], caller=AGENT)
    return corr


def test_a_card_waits_with_its_claims_and_makes_them_settling(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    corr = withdraw_correction(env, cid)
    approval = card_request(env, [{"op": "correction_apply", "id": corr, "base_rev": 1}])
    cards = env.svc.decision_cards(env.project_id, AGENT)
    [card] = cards["cards"]
    assert card["approval_request_id"] == approval and cards["recommended"] == [approval]
    assert card["card"]["what"].startswith("Mine haul")
    assert card["default_state"] == "waiting" and card["generated"] is True
    status = env.svc.claim_status(env.project_id, AGENT, claim_ids=[cid])["claims"][0]
    assert status["status"] == "settling"
    assert "card_waiting" in {c["code"] for c in status["causes"]}


def test_a_passed_default_changes_no_record_file_or_anchor(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    corr = withdraw_correction(env, cid)
    card_request(
        env,
        [{"op": "correction_apply", "id": corr, "base_rev": 1}],
        default_at="2026-01-01T00:00:00Z",
    )
    research = {p.name: p.read_bytes() for p in env.research.iterdir() if p.is_file()}
    before = store_digest(env.store_dir)
    later = datetime.now(UTC) + timedelta(days=400)
    store = env.svc.open(env.project_id)
    from prometheos.app import cards

    with closing(store.connect(readonly=True)) as conn:
        [card] = cards.view(conn, AGENT, later)["cards"]
    assert card["default_state"] == "default_in_effect"
    env.svc.decision_cards(env.project_id, OWNER)
    env.svc.claim_status(env.project_id, OWNER)
    env.svc.research_map(env.project_id, OWNER)
    env.svc.verification_plan(env.project_id, OWNER)
    env.svc.now(env.project_id, OWNER) if hasattr(env.svc, "now") else None
    assert store_digest(env.store_dir) == before
    assert {p.name: p.read_bytes() for p in env.research.iterdir() if p.is_file()} == research
    # The correction is still proposed and the claim still stands: nothing was applied.
    assert env.svc.get(env.project_id, OWNER, corr, None)["body"]["status"] == "proposed"
    assert env.svc.get(env.project_id, OWNER, cid, None)["body"]["state"] == "proposed"


# ----- apply all recommended ------------------------------------------------------------


def two_cards(env: Env) -> tuple[list[str], list[str], list[str]]:
    files = imported(env, ["result.csv"])
    claims = [claim(env, [line_ev(files["result.csv"], line)]) for line in (2, 3)]
    corrs = [withdraw_correction(env, c) for c in claims]
    approvals = [
        card_request(env, [{"op": "correction_apply", "id": c, "base_rev": 1}]) for c in corrs
    ]
    return claims, corrs, approvals


def test_apply_all_recommended_is_one_approval_in_one_transaction(env: Env) -> None:
    claims, corrs, approvals = two_cards(env)
    preview = env.svc.recommended_preview(env.project_id, HUB_OWNER, approvals, OWNER_ACT)
    assert preview["approval_request_ids"] == approvals and preview["operations"] == 2
    seq = env.seq()
    done = env.svc.recommended_apply(
        env.project_id, HUB_OWNER, approvals, preview["preview"]["digest"], OWNER_ACT
    )
    assert done["closed"] == approvals
    assert env.seq() == seq + 1  # one transaction
    for corr in corrs:
        assert env.svc.get(env.project_id, OWNER, corr, None)["body"]["status"] == "applied"
    for cid in claims:
        assert env.svc.get(env.project_id, OWNER, cid, None)["body"]["state"] == "withdrawn"
    states = {
        a["approval_request_id"]: a["state"]
        for a in env.svc.approval_list(env.project_id, OWNER)["approvals"]
    }
    assert {states[a] for a in approvals} == {"withdrawn"}
    assert env.svc.decision_cards(env.project_id, OWNER)["cards"] == []


def test_apply_all_recommended_applies_nothing_when_a_base_changed(env: Env) -> None:
    _claims, corrs, approvals = two_cards(env)
    preview = env.svc.recommended_preview(env.project_id, HUB_OWNER, approvals, OWNER_ACT)
    # The second correction is revised after the preview: its base revision moved.
    current = env.svc.get(env.project_id, OWNER, corrs[1], None)
    env.apply(
        [
            {
                "op": "revise",
                "id": corrs[1],
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": current["body"] | {"reason": "Reworded."},
            }
        ],
        caller=AGENT,
    )
    err = refused(
        lambda: env.svc.recommended_apply(
            env.project_id, HUB_OWNER, approvals, preview["preview"]["digest"], OWNER_ACT
        )
    )
    assert err.code == "revision_conflict"
    for corr in corrs:
        assert env.svc.get(env.project_id, OWNER, corr, None)["body"]["status"] == "proposed"
    assert len(env.svc.decision_cards(env.project_id, OWNER)["cards"]) == 2


def test_a_changed_set_needs_a_new_preview(env: Env) -> None:
    _claims, _corrs, approvals = two_cards(env)
    preview = env.svc.recommended_preview(env.project_id, HUB_OWNER, approvals[:1], OWNER_ACT)
    err = refused(
        lambda: env.svc.recommended_apply(
            env.project_id, HUB_OWNER, approvals, preview["preview"]["digest"], OWNER_ACT
        )
    )
    assert err.code == "revision_conflict"


# ----- surfaces -------------------------------------------------------------------------


def test_mcp_reads_match_the_service_and_an_agent_starts_through_mcp(env: Env) -> None:
    files = imported(env, ["result.csv"])
    claim(env, [line_ev(files["result.csv"], 2)])

    async def main() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        async with Client(build_server(env.svc, env.project_id)) as client:
            proposed = await client.call_tool("verification_propose", {})
            planned = await client.call_tool("verification_plan", {})
            cards = await client.call_tool("decision_cards", {})
            for reply in (proposed, planned, cards):
                assert not reply.is_error, reply
            return (
                dict(proposed.structured_content or {}),
                dict(planned.structured_content or {}),
                dict(cards.structured_content or {}),
            )

    proposed, planned, cards = anyio.run(main)
    assert len(proposed["created"]) == 1
    assert planned["checks"] == plan(env)["checks"]
    assert cards["cards"] == env.svc.decision_cards(env.project_id, AGENT)["cards"] == []
    transition(env, proposed["created"][0], "in_progress")


def test_the_budget_start_is_recorded_by_prometheos_only(env: Env) -> None:
    files = imported(env, ["result.csv"])
    claim(env, [line_ev(files["result.csv"], 2)])
    [check] = env.svc.verification_propose(env.project_id, AGENT)["created"]
    transition(env, check, "in_progress")
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        rows = conn.execute(
            "SELECT actor FROM audit WHERE record_id = ? AND rev = 1", (check,)
        ).fetchall()
        starts = conn.execute(
            "SELECT COUNT(*) FROM audit WHERE operation = ?", (verification.BUDGET_START,)
        ).fetchone()[0]
    assert {r[0] for r in rows} == {verification.DETECTOR_ACTOR}
    assert starts == 1


def test_planned_checks_do_not_flood_the_inbox(env: Env) -> None:
    """Found in the CP20.3 demo: 25 planned checks raised 25 inbox events. The detector's
    work-item events are written acknowledged; the checks show under "Checks planned"."""
    files = imported(env, ["result.csv"])
    for line in (2, 3):
        claim(env, [line_ev(files["result.csv"], line)])
    created = env.svc.verification_propose(env.project_id, AGENT)["created"]
    assert len(created) == 2
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        rows = conn.execute(
            "SELECT acknowledged_by FROM events WHERE type = 'work_item.created'"
        ).fetchall()
    assert len(rows) == 2 and all(r[0] == verification.DETECTOR_ACTOR for r in rows)


# ----- review cp20-3-a round 1 ----------------------------------------------------------


def test_a_cancelled_check_comes_back_only_by_the_owner(env: Env) -> None:
    """F1: the owner cancels a planned check; the agent cannot revive and start it."""
    files = imported(env, ["result.csv"])
    claim(env, [line_ev(files["result.csv"], 2)])
    [check] = env.svc.verification_propose(env.project_id, AGENT)["created"]
    transition(env, check, "cancelled", caller=OWNER)
    assert refused(lambda: transition(env, check, "proposed")).code == "approval_required"


def test_the_recommended_set_is_the_owners_alone(env: Env) -> None:
    """F2: the service refuses an agent caller, whatever route reaches it."""
    _claims, _corrs, approvals = two_cards(env)
    err = refused(lambda: env.svc.recommended_preview(env.project_id, AGENT, approvals, None))
    assert err.code == "policy_denied"
    preview = env.svc.recommended_preview(env.project_id, HUB_OWNER, approvals, OWNER_ACT)
    digest = preview["preview"]["digest"]
    err = refused(lambda: env.svc.recommended_apply(env.project_id, AGENT, approvals, digest, None))
    assert err.code == "policy_denied"
    # The preview names what approving does, from PrometheOS, not from the card.
    assert preview["preview"]["protected_effects"]


def test_a_budget_change_is_named_and_its_folder_must_be_approved(env: Env) -> None:
    """F4: a budget change reads as one; the output folder sits in an approved root; an agent
    can only request it."""
    project = env.svc.get(env.project_id, OWNER, env.project_id, None)
    settings = dict(project["body"]) | {
        "verification_budget": {"max_minutes_each": 20, "max_minutes_per_day": 90}
    }
    ops = [{"op": "project_update", "base_rev": project["rev"], "settings": settings}]
    raw = json.dumps(
        {
            "schema_version": 1,
            "project_id": env.project_id,
            "request_id": new_request_id(),
            "expected_seq": env.seq(),
            "operations": ops,
        }
    ).encode("utf-8")
    effects = env.svc.review_request(env.project_id, raw, OWNER)["protected_effects"]
    assert (
        "verification budget change: 15 min each, 60 min a day -> 20 min each, 90 min a day"
        in effects
    )
    assert refused(lambda: env.svc.apply(env.project_id, raw, AGENT)).code == "approval_required"
    outside = dict(project["body"]) | {
        "verification_budget": {
            "max_minutes_each": 15,
            "max_minutes_per_day": 60,
            "output_root_alias": "nowhere",
            "output_path": "reports",
        }
    }
    err = refused(
        lambda: env.apply_protected(
            [{"op": "project_update", "base_rev": project["rev"], "settings": outside}]
        )
    )
    assert err.code == "validation_error"


def test_a_check_takes_at_least_a_minute(env: Env) -> None:
    """F5: a zero estimate would let unlimited checks fit the daily budget."""
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assert refused(lambda: action_with_check(env, cid, minutes=0)).code == "validation_error"


def test_an_agents_decoy_action_does_not_silence_the_detector(env: Env) -> None:
    """F6: only PrometheOS's own checks count as already planned."""
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    decoy = action_with_check(env, cid)
    current = env.svc.get(env.project_id, OWNER, decoy, None)
    env.apply(
        [
            {
                "op": "revise",
                "id": decoy,
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": current["body"] | {"labels": ["verification", "cause:no_assessment"]},
            }
        ],
        caller=AGENT,
    )
    assert len(env.svc.verification_propose(env.project_id, AGENT)["created"]) == 1


def test_a_card_whose_plan_moved_says_so_and_is_not_recommended(env: Env) -> None:
    """F9: the plan's base revision moved; the card is stale and left out of the set."""
    _claims, corrs, approvals = two_cards(env)
    current = env.svc.get(env.project_id, OWNER, corrs[0], None)
    env.apply(
        [
            {
                "op": "revise",
                "id": corrs[0],
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": current["body"] | {"reason": "Reworded."},
            }
        ],
        caller=AGENT,
    )
    cards = env.svc.decision_cards(env.project_id, OWNER)
    stale = {c["approval_request_id"]: c["plan_current"] for c in cards["cards"]}
    assert stale == {approvals[0]: False, approvals[1]: True}
    assert cards["recommended"] == [approvals[1]]


def test_adoption_before_the_last_revision_does_not_count(env: Env) -> None:
    """Mutation gap 1: the owner adopts, then the agent revises the check: ineligible again."""
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    mine = action_with_check(env, cid)
    current = env.svc.get(env.project_id, OWNER, mine, None)
    env.apply_protected(
        [
            {
                "op": "transition",
                "id": mine,
                "base_rev": current["rev"],
                "to": "ready",
                "reason": "Adopted.",
                "evidence": [],
            }
        ],
        caller=HUB_OWNER,
    )
    current = env.svc.get(env.project_id, OWNER, mine, None)
    body = current["body"] | {
        "verification": current["body"]["verification"] | {"estimated_minutes": 2}
    }
    env.apply(
        [
            {
                "op": "revise",
                "id": mine,
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": body,
            }
        ],
        caller=AGENT,
    )
    assert refused(lambda: transition(env, mine, "in_progress")).code == "approval_required"


def test_the_day_follows_the_projects_time_zone(env: Env) -> None:
    """Mutation gap 2: a project 14 hours ahead of UTC starts its day at 10:00 UTC."""
    project = env.svc.get(env.project_id, OWNER, env.project_id, None)
    settings = dict(project["body"]) | {"timezone": "Pacific/Kiritimati"}
    env.apply_protected(
        [{"op": "project_update", "base_rev": project["rev"], "settings": settings}],
        caller=HUB_OWNER,
    )
    files = imported(env, ["result.csv"])
    claim(env, [line_ev(files["result.csv"], 2)])
    [check] = env.svc.verification_propose(env.project_id, AGENT)["created"]
    transition(env, check, "in_progress")
    store = env.svc.open(env.project_id)
    with closing(store.connect(readonly=True)) as conn:
        at = conn.execute(
            "SELECT recorded_at FROM audit WHERE operation = ?", (verification.BUDGET_START,)
        ).fetchone()[0]
        started = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        local_midnight = (started + timedelta(hours=14)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(hours=14)
        assert verification.used_today(conn, local_midnight + timedelta(hours=23)) == 10
        assert verification.used_today(conn, local_midnight + timedelta(hours=24, minutes=1)) == 0


def test_an_agent_sees_no_owner_rank_or_expired_card(env: Env) -> None:
    """Mutation gap 3: the rank filter and expiry."""
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    corr = withdraw_correction(env, cid)
    raw = json.dumps(
        {
            "schema_version": 1,
            "project_id": env.project_id,
            "request_id": new_request_id(),
            "expected_seq": env.seq(),
            "operations": [{"op": "correction_apply", "id": corr, "base_rev": 1}],
            "card": {
                "what": "SECRET owner-rank card",
                "why": "w",
                "recommendation": "r",
                "default_text": "d",
            },
        }
    ).encode("utf-8")
    assert refused(lambda: env.svc.apply(env.project_id, raw, OWNER)).code == "approval_required"
    assert env.svc.decision_cards(env.project_id, AGENT)["cards"] == []
    assert len(env.svc.decision_cards(env.project_id, OWNER)["cards"]) == 1
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn, conn:
        conn.execute("UPDATE approval_requests SET expires_at = '2000-01-01T00:00:00Z'")
    assert env.svc.decision_cards(env.project_id, OWNER)["cards"] == []


# ----- review cp20-3-a round 2 ----------------------------------------------------------


def test_a_claim_changed_after_the_cards_makes_its_card_stale(env: Env) -> None:
    """R2-1 (S64): the claim behind a card's correction is revised; that card is stale and left
    out, and the refreshed preview applies the rest."""
    claims, corrs, approvals = two_cards(env)
    current = env.svc.get(env.project_id, OWNER, claims[0], None)
    env.apply(
        [
            {
                "op": "revise",
                "id": claims[0],
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": current["body"] | {"statement": "The uphill energy is about 197 MJ."},
            }
        ],
        caller=AGENT,
    )
    cards = env.svc.decision_cards(env.project_id, OWNER)
    assert {c["approval_request_id"]: c["plan_current"] for c in cards["cards"]} == {
        approvals[0]: False,
        approvals[1]: True,
    }
    assert cards["recommended"] == [approvals[1]]
    preview = env.svc.recommended_preview(
        env.project_id, HUB_OWNER, cards["recommended"], OWNER_ACT
    )
    env.svc.recommended_apply(
        env.project_id, HUB_OWNER, cards["recommended"], preview["preview"]["digest"], OWNER_ACT
    )
    assert env.svc.get(env.project_id, OWNER, corrs[1], None)["body"]["status"] == "applied"
    assert env.svc.get(env.project_id, OWNER, corrs[0], None)["body"]["status"] == "proposed"


@pytest.mark.parametrize(
    "path",
    ["../outside", "C:/Windows", "/etc", "reports/../..", "a//b", "con", "a/nul.txt", "a\x00b"],
)
def test_the_output_folder_stays_inside_its_root(path: str) -> None:
    """R2-2: an absolute or climbing path is refused."""
    from pydantic import ValidationError

    from prometheos.domain.bodies import VerificationBudget

    with pytest.raises(ValidationError):
        VerificationBudget(output_root_alias="research", output_path=path)
    assert VerificationBudget(output_root_alias="research", output_path="reports/checks")


def test_a_moved_replacement_also_makes_the_card_stale(env: Env) -> None:
    """Round 3 note: a supersede card is stale when its replacement claim moved."""
    files = imported(env, ["result.csv"])
    old = claim(env, [line_ev(files["result.csv"], 2)])
    new = claim(env, [line_ev(files["result.csv"], 3)])
    corr = new_id("correction")
    correction = {
        "target_refs": [{"id": old, "rev": 1}],
        "replacement_refs": [{"id": new, "rev": 1}],
        "reason": "The published climb.",
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
    card_request(env, [{"op": "correction_apply", "id": corr, "base_rev": 1}])
    current = env.svc.get(env.project_id, OWNER, new, None)
    env.apply(
        [
            {
                "op": "revise",
                "id": new,
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": current["body"] | {"statement": "The climb is 63.6 MJ."},
            }
        ],
        caller=AGENT,
    )
    cards = env.svc.decision_cards(env.project_id, OWNER)
    assert [c["plan_current"] for c in cards["cards"]] == [False]
    assert cards["recommended"] == []
