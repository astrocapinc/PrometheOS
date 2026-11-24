"""The intelligence layer on the research map (decision 0109): L1 ``map.link_claim_question``.

Every reply here is a recorded cassette: nothing is sent, and cassette results confer no trust.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env
from cp7_support import create
from test_cp20_claim_status import claim, imported, line_ev
from test_cp20_layer import cassette

from prometheos.app import map_layer
from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError

KIND = "map.link_claim_question"
STATEMENT = "The bus saves 17.4 % fuel on the urban cycle."


def question(env: Env, title: str) -> str:
    qid = new_id("entry")
    body = {
        "title": title, "text": title, "category": "question", "evidence": [], "claim_refs": [],
        "author_ref": None, "origin": "imported", "quantities": [], "review": "reviewed",
    }  # fmt: skip
    env.apply([create("entry", body, qid)], caller=AGENT)
    return qid


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "summary.csv").write_text("name = value\nsaving = 17.4\n", "utf-8")
    return environment


@pytest.fixture
def world(env: Env) -> dict[str, Any]:
    fuel = question(env, "Does a flywheel KERS cut the fuel use of a city bus?")
    damper = question(env, "Which damper keeps the ring stable?")
    files = imported(env, ["summary.csv"])
    cid = claim(env, [line_ev(files["summary.csv"], 2)], statement=STATEMENT)
    return {"fuel": fuel, "damper": damper, "claim": cid}


def bundle_sha(env: Env, cid: str) -> str:
    (subject,) = [
        s for s in map_layer.subjects(env.svc.open(env.project_id), OWNER, None)
        if s.bundle.subject == cid
    ]  # fmt: skip
    return subject.bundle.input_sha256


def reply(question_id: str, quote: str) -> dict[str, Any]:
    return {"result": {"question_id": question_id, "quote": quote}, "abstain": None}


def record(folder: Path, sha: str, first: dict[str, Any], second: dict[str, Any]) -> Path:
    cassette(folder, KIND, sha, 0, first, "vendor-a/model")
    cassette(folder, KIND, sha, 1, second, "vendor-b/model")
    return folder


def unplaced(env: Env) -> dict[str, dict[str, Any]]:
    found = env.svc.research_map(env.project_id, OWNER)["unplaced_claims"]
    return {c["claim_id"]: c for c in found}


def test_two_agreeing_models_propose_the_question_and_the_owner_places_the_claim(
    env: Env, world: dict[str, Any], tmp_path: Path
) -> None:
    cid = world["claim"]
    fuel = reply(world["fuel"], "saves 17.4 % fuel")
    folder = record(tmp_path / "tapes", bundle_sha(env, cid), fuel,
                    reply(world["fuel"], "fuel on the urban cycle"))  # fmt: skip
    before = env.seq()
    done = env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)
    (item,) = done["items"]
    assert item["outcome"] == "accepted" and item["proposal"]["question_id"] == world["fuel"]
    assert env.seq() == before  # a proposal writes no record
    suggestion = unplaced(env)[cid]["suggestion"]
    assert suggestion["agree"] is True and suggestion["question_id"] == world["fuel"]
    assert suggestion["question_title"].startswith("Does a flywheel KERS")
    assert suggestion["executors"] == ["cassette"]
    env.apply(
        [{"op": "relation_add", "source_ref": {"id": cid, "rev": 1},
          "target_ref": {"id": world["fuel"], "rev": 1}, "relation": "answers"}],
        caller=OWNER,
    )  # fmt: skip
    assert cid not in unplaced(env)


@pytest.mark.parametrize(
    ("second", "reason"),
    [
        ("unoffered", "unoffered_option"),
        ("invented quote", "quote_not_found"),
    ],
)
def test_a_reply_outside_the_offer_or_the_claims_words_is_rejected(
    env: Env, world: dict[str, Any], tmp_path: Path, second: str, reason: str
) -> None:
    cid = world["claim"]
    bad = (
        reply("entry_00000000-0000-4000-8000-000000000000", "saves 17.4 % fuel")
        if second == "unoffered"
        else reply(world["fuel"], "reduces consumption by a lot")
    )
    folder = record(tmp_path / "tapes", bundle_sha(env, cid),
                    reply(world["fuel"], "saves 17.4 % fuel"), bad)  # fmt: skip
    (item,) = env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)["items"]
    assert item["outcome"] == "rejected" and reason in item["reasons"]
    assert unplaced(env)[cid]["suggestion"] is None


def test_models_that_disagree_make_no_proposal(
    env: Env, world: dict[str, Any], tmp_path: Path
) -> None:
    cid = world["claim"]
    fuel = reply(world["fuel"], "saves 17.4 % fuel")
    folder = record(tmp_path / "tapes", bundle_sha(env, cid), fuel,
                    reply(world["damper"], "on the urban cycle"))  # fmt: skip
    (item,) = env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)["items"]
    assert item["outcome"] == "disagreed"
    models = ["vendor-a/model", "vendor-b/model"]
    assert unplaced(env)[cid]["suggestion"] == {"agree": False, "models": models}


def test_a_changed_claim_drops_the_old_proposal(
    env: Env, world: dict[str, Any], tmp_path: Path
) -> None:
    cid = world["claim"]
    fuel = reply(world["fuel"], "saves 17.4 % fuel")
    folder = record(tmp_path / "tapes", bundle_sha(env, cid), fuel,
                    reply(world["fuel"], "saves 17.4 % fuel"))  # fmt: skip
    env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)
    assert unplaced(env)[cid]["suggestion"]["agree"] is True
    body = env.svc.get(env.project_id, OWNER, cid, None)["body"]
    env.apply([{"op": "revise", "id": cid, "base_rev": 1, "sensitivity": "normal",
                "occurred_at": None, "body": body | {"statement": "The bus saves 17.0 % fuel."}}],
              caller=AGENT)  # fmt: skip
    assert unplaced(env)[cid]["suggestion"] is None


def test_a_restricted_claim_is_never_sent(env: Env, world: dict[str, Any], tmp_path: Path) -> None:
    cid = world["claim"]
    body = env.svc.get(env.project_id, OWNER, cid, None)["body"]
    env.apply_protected([{"op": "revise", "id": cid, "base_rev": 1, "sensitivity": "restricted",
                          "occurred_at": None, "body": body}], caller=OWNER)  # fmt: skip
    (tmp_path / "tapes").mkdir()
    (item,) = env.svc.ai_map_suggest(env.project_id, OWNER, cassettes=tmp_path / "tapes")["items"]
    assert (item["outcome"], item["reasons"]) == ("denied", ["policy_denied"])


def test_outside_a_standing_policy_nothing_is_sent(env: Env, world: dict[str, Any]) -> None:
    with pytest.raises(PrometheosError) as err:
        env.svc.ai_map_suggest(env.project_id, AGENT, dry_run=True)
    assert err.value.code == "capability_unavailable"


def test_questions_are_offered_by_title_and_restricted_ones_never(
    env: Env, world: dict[str, Any]
) -> None:
    hidden = question(env, "A restricted question")
    body = env.svc.get(env.project_id, OWNER, hidden, None)["body"]
    env.apply_protected([{"op": "revise", "id": hidden, "base_rev": 1, "sensitivity": "restricted",
                          "occurred_at": None, "body": body}], caller=OWNER)  # fmt: skip
    (subject,) = map_layer.subjects(env.svc.open(env.project_id), OWNER, None)
    offered = subject.bundle.input["question_ids"]
    assert offered == [world["fuel"], world["damper"]]  # by title, the restricted one left out
    assert subject.bundle.input["claim"]["statement"] == STATEMENT


# ----- review map-ai-spread-a round 1 ------------------------------------------------------------

ABSTAIN = {"result": None, "abstain": {"reason": "outside_the_options"}}


def test_r1_f1_two_abstained_runs_never_read_as_agreement(
    env: Env, world: dict[str, Any], tmp_path: Path
) -> None:
    """Each run abstained (one model answered, the other did not): the latest sample of each
    index is the judge, so no proposal is shown however the accepted samples line up."""
    cid, sha = world["claim"], bundle_sha(env, world["claim"])
    first, second = tmp_path / "x", tmp_path / "y"
    cassette(first, KIND, sha, 0, reply(world["fuel"], "saves 17.4 % fuel"), "vendor-a/model")
    cassette(first, KIND, sha, 1, ABSTAIN, "vendor-b/model")
    cassette(second, KIND, sha, 0, reply(world["fuel"], "saves 17.4 % fuel"), "vendor-c/model")
    cassette(second, KIND, sha, 1, ABSTAIN, "vendor-d/model")
    for folder in (first, second):
        (item,) = env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)["items"]
        assert item["outcome"] == "abstained"
    assert unplaced(env)[cid]["suggestion"] is None


def test_r1_f1_a_sample_from_outside_the_evaluated_list_is_only_a_hint(
    env: Env, world: dict[str, Any], tmp_path: Path
) -> None:
    import json

    cid, sha = world["claim"], bundle_sha(env, world["claim"])
    folder = record(tmp_path / "tapes", sha, reply(world["fuel"], "saves 17.4 % fuel"),
                    reply(world["fuel"], "saves 17.4 % fuel"))  # fmt: skip
    for path in folder.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["in_list"] = data["sample"] != 1
        path.write_text(json.dumps(data), encoding="utf-8")
    env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)
    suggestion = unplaced(env)[cid]["suggestion"]
    assert suggestion["agree"] is True and suggestion["hint_only"] is True


def test_r1_f1_one_model_twice_is_no_agreement(
    env: Env, world: dict[str, Any], tmp_path: Path
) -> None:
    cid, sha = world["claim"], bundle_sha(env, world["claim"])
    folder = tmp_path / "tapes"
    for i in (0, 1):
        cassette(folder, KIND, sha, i, reply(world["fuel"], "saves 17.4 % fuel"), "vendor-a/model")
    (item,) = env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)["items"]
    assert item["outcome"] != "accepted"
    assert unplaced(env)[cid]["suggestion"] is None


def test_r1_f2_the_proposal_carries_the_questions_current_revision(
    env: Env, world: dict[str, Any], tmp_path: Path
) -> None:
    fuel = world["fuel"]
    body = env.svc.get(env.project_id, OWNER, fuel, None)["body"]
    env.apply([{"op": "revise", "id": fuel, "base_rev": 1, "sensitivity": "normal",
                "occurred_at": None, "body": body | {"text": "Revised wording."}}],
              caller=AGENT)  # fmt: skip
    cid, sha = world["claim"], bundle_sha(env, world["claim"])
    folder = record(tmp_path / "tapes", sha, reply(fuel, "saves 17.4 % fuel"),
                    reply(fuel, "the urban cycle"))  # fmt: skip
    env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)
    assert unplaced(env)[cid]["suggestion"]["question_rev"] == 2


def test_r1_f3_a_placed_claims_replacement_is_not_unplaced(tmp_path: Path) -> None:
    import test_cp20_structure as s

    env = Env(tmp_path)
    (env.research / "result.csv").write_text("name = value\na = 1\nb = 2\n", "utf-8")
    files = s.imported(env, ["result.csv"])
    pid = s.paper(env)
    q = s.entry(env, "question", "Is the mine haul a boundary case?")
    old = s.claim(env, [s.line_ev(files["result.csv"], 2)])
    new = s.claim(env, [s.line_ev(files["result.csv"], 3)])
    s.link(env, q, pid, "addresses")
    s.link(env, old, q, "answers")
    s.assess(env, old)
    s.supersede(env, old, new)
    view = env.svc.research_map(env.project_id, OWNER)
    under = {c["claim_id"] for d in view["deliverables"] for x in d["questions"]
             for c in x["claims"]}  # fmt: skip
    assert new in under
    assert new not in {c["claim_id"] for c in view["unplaced_claims"]}


def test_r1_f4_a_used_up_key_refuses_the_map_run(env: Env, world: dict[str, Any]) -> None:
    import json

    import test_cp22_review as R

    from prometheos.providers import transport as tp

    class KeyEmpty(R.Recorder):
        def get(self, url: str, headers: dict[str, str], timeout: float) -> tp.Reply:
            return tp.Reply(200, json.dumps({"data": {"limit": 5, "limit_remaining": 0}}).encode())

    second = R.PAIR | {"model": "openai/gpt-test", "provider": "azure", "company": "openai"}
    doc = R.policy(tasks=[{"task_kind": KIND, "models": [R.PAIR, second],
                           "single_company_reason": None, "task_ceiling_usd": "0.05"}])  # fmt: skip
    transport = KeyEmpty([])
    R.setup(env.svc, env.project_id, env.store_dir, transport, doc)
    with pytest.raises(PrometheosError) as empty:
        env.svc.ai_map_suggest(env.project_id, OWNER)
    assert empty.value.code == "budget_exceeded" and empty.value.details["cap"] == "key"
    assert transport.bodies == []  # nothing was sent


@pytest.mark.parametrize("quote", ["the", "s 1", "aves 17.4"])
def test_r1_f12_a_quote_is_two_or_more_whole_words(
    env: Env, world: dict[str, Any], tmp_path: Path, quote: str
) -> None:
    cid, sha = world["claim"], bundle_sha(env, world["claim"])
    folder = record(tmp_path / "tapes", sha, reply(world["fuel"], "saves 17.4 % fuel"),
                    reply(world["fuel"], quote))  # fmt: skip
    (item,) = env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)["items"]
    assert item["outcome"] == "rejected" and "quote_not_found" in item["reasons"]
    assert unplaced(env)[cid]["suggestion"] is None


def test_r2_f1_samples_no_run_judged_together_are_never_paired(
    env: Env, world: dict[str, Any], tmp_path: Path
) -> None:
    """Run 1 abstains, run 2 is rejected, run 3 reuses run 1's cached first sample and disagrees.
    The latest sample per index would pair vendor-c (rejected run) with vendor-e (disagreeing
    run) as an agreement; the latest run's own verdict says the models disagree."""
    cid, sha = world["claim"], bundle_sha(env, world["claim"])
    bad = reply("entry_00000000-0000-4000-8000-000000000000", "saves 17.4 % fuel")
    x, y, z = tmp_path / "x", tmp_path / "y", tmp_path / "z"
    cassette(x, KIND, sha, 0, reply(world["fuel"], "saves 17.4 % fuel"), "vendor-a/model")
    cassette(x, KIND, sha, 1, ABSTAIN, "vendor-b/model")
    cassette(y, KIND, sha, 0, reply(world["damper"], "saves 17.4 % fuel"), "vendor-c/model")
    cassette(y, KIND, sha, 1, bad, "vendor-d/model")
    cassette(z, KIND, sha, 0, reply(world["fuel"], "saves 17.4 % fuel"), "vendor-a/model")
    cassette(z, KIND, sha, 1, reply(world["damper"], "saves 17.4 % fuel"), "vendor-e/model")
    outcomes = [
        env.svc.ai_map_suggest(env.project_id, AGENT, cassettes=folder)["items"][0]["outcome"]
        for folder in (x, y, z)
    ]
    assert outcomes == ["abstained", "rejected", "disagreed"]
    suggestion = unplaced(env)[cid]["suggestion"]
    assert suggestion is not None and suggestion["agree"] is False
