"""The intelligence layer on decision cards (decision 0110, L3: ``cards.word``).

The layer proposes plain wording for a card's what / why / recommendation from its facts. A
number appears only as a bound quantity placeholder that code fills, and the card's default and
its date are never touched. Every reply is a recorded cassette: nothing is sent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env
from test_cp20_claim_status import assess, claim, imported, line_ev
from test_cp20_layer import cassette
from test_cp20_verify import card_request, withdraw_correction

from prometheos.app import card_layer

KIND = "cards.word"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "result.csv").write_text(
        "name = value\nenergy_MJ = 196.9\nenergy_MJ = 63.6\n", "utf-8"
    )
    return environment


@pytest.fixture
def card(env: Env) -> dict[str, str]:
    files = imported(env, ["result.csv"])
    ev = line_ev(files["result.csv"], 2)
    binding = {"decimal": "196.9", "unit": "", "source_ref": ev,  # the line holds a bare number
               "source_pointer": "text:2:12:17", "name": "uphill energy"}  # fmt: skip
    cid = claim(env, [ev], quantities=[binding])
    assess(env, cid)
    corr = withdraw_correction(env, cid)
    approval = card_request(env, [{"op": "correction_apply", "id": corr, "base_rev": 1}],
                            default_at="2026-12-01T00:00:00Z")  # fmt: skip
    return {"claim": cid, "approval": approval}


def subject_sha(env: Env, approval: str) -> str:
    (subject,) = [s for s in card_layer.subjects(env.svc.open(env.project_id), OWNER, None)
                  if s.bundle.subject == approval]  # fmt: skip
    return subject.bundle.input_sha256


def wording(what: str, why: str, recommendation: str) -> dict[str, Any]:
    return {"result": {"what": what, "why": why, "recommendation": recommendation},
            "abstain": None}  # fmt: skip


def run(env: Env, folder: Path, caller: Any = AGENT) -> dict[str, Any]:
    (item,) = env.svc.ai_map_suggest(env.project_id, caller, task_kind=KIND,
                                     cassettes=folder)["items"]  # fmt: skip
    return item


def stored_plan(env: Env, approval: str) -> dict[str, Any]:
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT plan FROM approval_requests WHERE id = ?", (approval,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


def the_card(env: Env, caller: Any = OWNER) -> dict[str, Any]:
    (found,) = env.svc.decision_cards(env.project_id, caller)["cards"]
    return found


GOOD = wording(
    "Withdraw the uphill-energy claim of {q:q1}?",
    "The stored cycle used the steepest grade everywhere, so {q:q1} overstates the climb.",
    "Apply the correction: the claim is withdrawn and views stop quoting it.",
)


def test_the_layer_words_the_card_with_bound_numbers_filled_by_code(
    env: Env, card: dict[str, str], tmp_path: Path
) -> None:
    approval = card["approval"]
    (subject,) = card_layer.subjects(env.svc.open(env.project_id), OWNER, None)
    assert subject.bundle.input["quantities"] == ["q1"]
    assert subject.bundle.input["claims"][0]["quantities"][0]["name"] == "uphill energy"
    before, plan = env.seq(), stored_plan(env, approval)
    folder = tmp_path / "tapes"
    cassette(folder, KIND, subject_sha(env, approval), 0, GOOD, "vendor-a/model")
    assert run(env, folder)["outcome"] == "accepted"
    assert env.seq() == before and stored_plan(env, approval) == plan  # nothing written
    shown = the_card(env)
    assert shown["wording"]["what"] == "Withdraw the uphill-energy claim of 196.9?"
    assert "196.9 overstates" in shown["wording"]["why"]
    assert shown["wording"]["hint_only"] is True and shown["wording"]["executors"] == ["cassette"]
    # The card, its default and its date are untouched.
    assert shown["card"] == plan["card"]
    assert shown["card"]["default_text"] == "Views show the corrected number until you decide."
    assert shown["card"]["default_at"] == "2026-12-01T00:00:00Z"


@pytest.mark.parametrize(
    "bad",
    [
        wording("Withdraw the claim of 196.9 MJ?", "Because.", "Apply it."),  # a digit of its own
        wording("Withdraw the claim of {q:q9}?", "Because.", "Apply it."),  # an unoffered id
        wording("Withdraw it?", "Because of {q:q1} and 3 grades.", "Apply it."),
        # Review map-ai-spread-b R1-F5: a number word or a numeral that is not a digit.
        wording("Withdraw it?", "It overstates the climb by twelve percent.", "Apply it."),
        wording("Withdraw it?", "About \u00bd of the climb is wrong.", "Apply it."),
        wording("Withdraw it?", "About a quarter of the climb is wrong.", "Apply it."),  # R2-F2
    ],
)
def test_a_number_of_its_own_or_an_unoffered_placeholder_is_rejected(
    env: Env, card: dict[str, str], tmp_path: Path, bad: dict[str, Any]
) -> None:
    folder = tmp_path / "tapes"
    cassette(folder, KIND, subject_sha(env, card["approval"]), 0, bad, "vendor-a/model")
    item = run(env, folder)
    assert item["outcome"] == "rejected" and "unbound_number" in item["reasons"]
    assert the_card(env)["wording"] is None


def test_a_reply_that_sets_the_default_fails_the_shape_check(
    env: Env, card: dict[str, str], tmp_path: Path
) -> None:
    bad = GOOD | {"result": GOOD["result"] | {"default_text": "Nothing happens."}}
    folder = tmp_path / "tapes"
    cassette(folder, KIND, subject_sha(env, card["approval"]), 0, bad, "vendor-a/model")
    item = run(env, folder)
    assert (item["outcome"], item["reasons"]) == ("rejected", ["shape"])
    shown = the_card(env)
    assert shown["wording"] is None
    assert shown["card"]["default_text"] == "Views show the corrected number until you decide."


def test_an_abstention_shows_no_wording(env: Env, card: dict[str, str], tmp_path: Path) -> None:
    folder = tmp_path / "tapes"
    abstain = {"result": None, "abstain": {"reason": "not_enough_evidence"}}
    cassette(folder, KIND, subject_sha(env, card["approval"]), 0, abstain, "vendor-a/model")
    assert run(env, folder)["outcome"] == "abstained"
    assert the_card(env)["wording"] is None


def test_a_changed_claim_drops_the_old_wording(
    env: Env, card: dict[str, str], tmp_path: Path
) -> None:
    folder = tmp_path / "tapes"
    cassette(folder, KIND, subject_sha(env, card["approval"]), 0, GOOD, "vendor-a/model")
    run(env, folder)
    assert the_card(env)["wording"] is not None
    cid = card["claim"]
    body = env.svc.get(env.project_id, OWNER, cid, None)["body"]
    env.apply([{"op": "revise", "id": cid, "base_rev": 1, "sensitivity": "normal",
                "occurred_at": None, "body": body | {"statement": "Another statement."}}],
              caller=AGENT)  # fmt: skip
    assert the_card(env)["wording"] is None


def test_a_restricted_claim_keeps_its_card_away_from_the_executor(
    env: Env, card: dict[str, str], tmp_path: Path
) -> None:
    cid = card["claim"]
    body = env.svc.get(env.project_id, OWNER, cid, None)["body"]
    env.apply_protected([{"op": "revise", "id": cid, "base_rev": 1, "sensitivity": "restricted",
                          "occurred_at": None, "body": body}], caller=OWNER)  # fmt: skip
    (tmp_path / "tapes").mkdir()
    item = run(env, tmp_path / "tapes", OWNER)
    assert (item["outcome"], item["reasons"]) == ("denied", ["policy_denied"])


def test_claim_ids_narrow_the_run_to_cards_about_them(env: Env, card: dict[str, str]) -> None:
    store = env.svc.open(env.project_id)
    assert len(card_layer.subjects(store, OWNER, [card["claim"]])) == 1
    other = "claim_00000000-0000-4000-8000-000000000000"
    assert card_layer.subjects(store, OWNER, [other]) == []


def test_r1_f8_a_correction_the_caller_cannot_see_is_not_followed(
    env: Env, card: dict[str, str]
) -> None:
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        plan = json.loads(conn.execute("SELECT plan FROM approval_requests").fetchone()[0])
    finally:
        conn.close()
    corr = plan["operations"][0]["id"]
    body = env.svc.get(env.project_id, OWNER, corr, None)["body"]
    env.apply_protected([{"op": "revise", "id": corr, "base_rev": 1, "sensitivity": "restricted",
                          "occurred_at": None, "body": body}], caller=OWNER)  # fmt: skip
    store = env.svc.open(env.project_id)
    owner = [s.bundle.input["claims"] for s in card_layer.subjects(store, OWNER, None)]
    agent = [s.bundle.input["claims"] for s in card_layer.subjects(store, AGENT, None)]
    assert len(owner[0]) == 1 and agent in ([], [[]])
