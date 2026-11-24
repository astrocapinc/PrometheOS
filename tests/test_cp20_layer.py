"""CP20.4 under A07 (C20-ADOPT with C22-EVAL's rules; decision 0107): the intelligence layer's two
research-map tasks. ``adoption.propose_outline`` reads a note's lines for questions, deliverables
and tracks, each a verified quote; ``adoption.propose_family_class`` proposes what a competing
family's files are, needs two agreeing models, and never moves the family out of ``compete`` or
closes its owner item. Both run on the coding agent (prepare and submit) and on the gateway with
the same validators; here the gateway runs on recorded cassettes, so nothing is sent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER_ACT, Env
from test_cp20_outline import forking_folder

from prometheos.adoption import assist
from prometheos.domain.canonical import canonical_sha256
from prometheos.intelligence import tasks


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    forking_folder(environment.research)
    (environment.research / "NOTES.md").write_text(
        "# Plan\n\nThe thesis chapter on clutch wear is due in March.\n"
        "Track 7 covers the hilly route.\n", "utf-8"
    )  # fmt: skip
    for ctx, litres in (("bus", 31), ("van", 12)):
        folder = environment.research / "vehicles" / ctx
        folder.mkdir(parents=True)
        (folder / "fuel.csv").write_text(f"vehicle,litres\n{ctx},{litres}\n", "utf-8")
    return environment


def scan(env: Env) -> int:
    return int(
        env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")["scan_gen"]
    )


def submit(env: Env, gen: int, kind: str, subject: str, sample: int, response: Any,
           model: str) -> dict[str, Any]:  # fmt: skip
    return env.svc.adoption_assist_submit(env.project_id, AGENT, gen, kind, subject, sample,
                                          response, model)  # fmt: skip


def fuel_family(env: Env, gen: int) -> dict[str, Any]:
    listing = env.svc.adoption_families(env.project_id, AGENT, gen, limit=500)["families"]
    return next(f for f in listing if f["key"].endswith("fuel"))


# ----- the outline from a note --------------------------------------------------------------


def test_outline_items_from_a_note_are_verified_quotes_that_join_the_outline(env: Env) -> None:
    gen = scan(env)
    suggested = env.svc.adoption_assist_tasks(env.project_id, AGENT, gen)["tasks"]
    assert ("propose_outline", "NOTES.md") in {(t["task_kind"], t["subject"]) for t in suggested}
    prepared = env.svc.adoption_assist_prepare(env.project_id, AGENT, gen, "propose_outline",
                                               "NOTES.md")  # fmt: skip
    assert prepared["input"]["lines"][2]["text"].startswith("The thesis chapter")
    bad = submit(env, gen, "propose_outline", "NOTES.md", 1, {"items": [
        {"kind": "deliverable", "line": 3, "quote": "The thesis chapter on clutch wear is due",
         "title": "Clutch wear thesis chapter"},  # words of its own
    ]}, "agent/a")  # fmt: skip
    assert bad["accepted"] is False and "not words from the quote" in bad["problems"][0]
    wrong_line = submit(env, gen, "propose_outline", "NOTES.md", 2, {"items": [
        {"kind": "track", "line": 3, "quote": "Track 7 covers the hilly route.",
         "title": "Track 7"},
    ]}, "agent/a")  # fmt: skip
    assert wrong_line["accepted"] is False
    good = submit(env, gen, "propose_outline", "NOTES.md", 3, {"items": [
        {"kind": "deliverable", "line": 3,
         "quote": "The thesis chapter on clutch wear is due in March.",
         "title": "thesis chapter on clutch wear"},
        {"kind": "track", "line": 4, "quote": "Track 7 covers the hilly route.",
         "title": "Track 7"},
    ]}, "agent/a")  # fmt: skip
    assert good["accepted"] and good["outcome"]["state"] == "accepted"
    outline = env.svc.adoption_outline(env.project_id, AGENT, gen)
    proposed = [i for i in outline["items"] if i["source"] != "rule"]
    assert {(i["kind"], i["title"]) for i in proposed} == {
        ("deliverable", "thesis chapter on clutch wear"),
        ("track", "Track 7"),
    }
    for item in proposed:
        (cite,) = item["cites"]
        assert cite["path"] == "NOTES.md" and cite["quote"] and cite["line"] in (3, 4)
    # The rules' outline is unchanged apart from the proposals.
    assert {i["title"] for i in outline["items"] if i["source"] == "rule" and
            i["kind"] == "track"} == {"track2", "track5"}  # fmt: skip


# ----- a class proposal for a competing family ----------------------------------------------


def test_a_class_proposal_needs_two_models_and_never_answers_the_family(env: Env) -> None:
    gen = scan(env)
    family = fuel_family(env, gen)
    assert (family["class"], family["needs_owner"]) == ("compete", True)
    suggested = env.svc.adoption_assist_tasks(env.project_id, AGENT, gen)["tasks"]
    assert ("propose_family_class", family["key"]) in {
        (t["task_kind"], t["subject"]) for t in suggested
    }
    prepared = env.svc.adoption_assist_prepare(env.project_id, AGENT, gen,
                                               "propose_family_class", family["key"])  # fmt: skip
    members = [m["path"] for m in prepared["input"]["members"]]
    assert members == ["vehicles/bus/fuel.csv", "vehicles/van/fuel.csv"]
    answer = {
        "answer": "variant",
        "current": None,
        "quote": {"path": "vehicles/bus/fuel.csv", "line": 1, "text": "vehicle,litres"},
    }
    short = submit(env, gen, "propose_family_class", family["key"], 1, answer, "model/a")
    # "vehicle,litres" is 14 characters: verified on line 1 of the bus preview.
    assert short["accepted"] and short["outcome"]["state"] == "waiting"  # one model is not enough
    invented = submit(env, gen, "propose_family_class", family["key"], 2,
                      {"answer": "history", "current": "vehicles/car/fuel.csv", "quote": None},
                      "model/b")  # fmt: skip
    assert invented["accepted"] is False  # the current is not a member
    second = submit(
        env,
        gen,
        "propose_family_class",
        family["key"],
        3,
        {"answer": "variant", "current": None, "quote": None},
        "model/b",
    )
    assert second["outcome"]["state"] == "accepted"
    after = fuel_family(env, gen)
    assert after["class_proposal"]["answer"] == "variant"
    assert after["class_proposal"]["models"] == ["model/a", "model/b"]
    # Still the owner's: the class stays compete and the family still asks.
    assert (after["class"], after["class_decision"], after["needs_owner"]) == (
        "compete",
        None,
        True,
    )
    env.svc.adoption_plan_create(env.project_id, AGENT, gen)
    plan = env.svc.adoption_plan(env.project_id, AGENT, gen)
    asking = [s for s in plan["steps"] if s["step"]["kind"] == "resolve_conflict"]
    assert any(family["family_id"] in s["step"]["family_ids"] for s in asking)


def test_models_that_disagree_go_to_the_owner_as_a_conflict(env: Env) -> None:
    gen = scan(env)
    family = fuel_family(env, gen)
    submit(env, gen, "propose_family_class", family["key"], 1,
           {"answer": "variant", "current": None, "quote": None}, "model/a")  # fmt: skip
    split = submit(env, gen, "propose_family_class", family["key"], 2,
                   {"answer": "succession", "current": "vehicles/van/fuel.csv", "quote": None},
                   "model/b")  # fmt: skip
    assert split["outcome"]["state"] == "needs_owner"
    assert fuel_family(env, gen)["class_proposal"] is None


def test_a_changed_member_makes_the_proposal_lapse(env: Env) -> None:
    gen = scan(env)
    family = fuel_family(env, gen)
    for sample, model in ((1, "model/a"), (2, "model/b")):
        submit(env, gen, "propose_family_class", family["key"], sample,
               {"answer": "variant", "current": None, "quote": None}, model)  # fmt: skip
    assert fuel_family(env, gen)["class_proposal"] is not None
    (env.research / "vehicles" / "van" / "fuel.csv").write_text("vehicle,litres\nvan,13\n", "utf-8")
    gen2 = scan(env)
    assert fuel_family(env, gen2)["class_proposal"] is None


def test_a_family_the_rules_settle_gets_no_proposal(env: Env) -> None:
    gen = scan(env)
    listing = env.svc.adoption_families(env.project_id, AGENT, gen, limit=500)["families"]
    variant = next(f for f in listing if f["class"] == "variant")
    from prometheos.domain.models import PrometheosError

    with pytest.raises(PrometheosError) as err:
        env.svc.adoption_assist_prepare(env.project_id, AGENT, gen, "propose_family_class",
                                        variant["key"])  # fmt: skip
    assert err.value.code == "validation_error"


# ----- the same tasks on the gateway's path, from recorded cassettes -------------------------


def cassette(folder: Path, kind: str, input_sha256: str, sample: int, content: dict[str, Any],
             model: str) -> None:  # fmt: skip
    folder.mkdir(exist_ok=True)
    body = {
        "id": "g",
        "model": model,
        "provider": "Recorded",
        "usage": {"prompt_tokens": 50, "completion_tokens": 9},
        "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
    }
    name = canonical_sha256([kind, input_sha256, sample])[:12]
    (folder / f"{name}.json").write_text(json.dumps({
        "task_kind": kind, "input_sha256": input_sha256, "sample": sample, "body": body,
        "model": model,
    }), encoding="utf-8")  # fmt: skip


def outline_bundle(env: Env, gen: int) -> assist.Bundle:
    env.svc.ai_root_sensitivity_set(env.project_id, "research", "normal", authority=OWNER_ACT)
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        return assist._bundle(conn, env.research, gen, "propose_outline", "NOTES.md")
    finally:
        conn.close()


TRACK7 = {"kind": "track", "line": 4, "quote": "Track 7 covers the hilly route.",
          "title": "Track 7"}  # fmt: skip


def run_outline(env: Env, gen: int, tapes: Path) -> dict[str, Any]:
    return env.svc.ai_run(env.project_id, AGENT, task_kind=tasks.PROPOSE_OUTLINE.kind,
                          scan_gen=gen, paths=["NOTES.md"], cassettes=tapes)  # fmt: skip


def test_the_outline_task_runs_on_the_layer_with_the_same_validators(
    env: Env, tmp_path: Path
) -> None:
    gen = scan(env)
    bundle = outline_bundle(env, gen)
    tapes = tmp_path / "tapes"
    cassette(tapes, tasks.PROPOSE_OUTLINE.kind, bundle.input_sha256, 0,
             {"result": {"items": [TRACK7]}, "abstain": None}, "cassette/recorded")  # fmt: skip
    done = run_outline(env, gen, tapes)
    [ran] = done["items"]
    assert ran["outcome"] == "accepted" and done["executor"] == "cassette"
    # A reply is only a proposal: the outline holds it only after the owner accepts it, exactly
    # as for the CP17.4 tasks, and a replay confers no trust.
    outline = env.svc.adoption_outline(env.project_id, AGENT, gen)
    assert "Track 7" not in {i["title"] for i in outline["items"]}


def test_a_layer_reply_quoting_the_wrong_line_is_rejected(env: Env, tmp_path: Path) -> None:
    gen = scan(env)
    bundle = outline_bundle(env, gen)
    tapes = tmp_path / "tapes"
    cassette(tapes, tasks.PROPOSE_OUTLINE.kind, bundle.input_sha256, 0,
             {"result": {"items": [TRACK7 | {"line": 3}]}, "abstain": None},
             "cassette/recorded")  # fmt: skip
    [ran] = run_outline(env, gen, tapes)["items"]
    assert ran["outcome"] == "rejected" and "quote_not_found" in ran["reasons"]


# ----- review cp20-a-r1 R1-F4: invariants that must each fail a test when removed -------------


def test_a_family_quote_that_is_not_in_a_preview_is_rejected(env: Env) -> None:
    gen = scan(env)
    family = fuel_family(env, gen)
    invented = submit(env, gen, "propose_family_class", family["key"], 1, {
        "answer": "variant", "current": None,
        "quote": {"path": "vehicles/bus/fuel.csv", "line": 2, "text": "bus is right for its route"},
    }, "model/a")  # fmt: skip
    assert invented["accepted"] is False
    assert "not in the preview as written" in invented["problems"][0]
    elsewhere = submit(env, gen, "propose_family_class", family["key"], 2, {
        "answer": "variant", "current": None,
        "quote": {"path": "NOTES.md", "line": 3, "text": "The thesis chapter on clutch wear"},
    }, "model/a")  # fmt: skip
    assert elsewhere["accepted"] is False  # a file outside the family


def family_bundle(env: Env, gen: int, key: str) -> assist.Bundle:
    env.svc.ai_root_sensitivity_set(env.project_id, "research", "normal", authority=OWNER_ACT)
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        return assist._bundle(conn, env.research, gen, "propose_family_class", key)
    finally:
        conn.close()


def run_family(env: Env, gen: int, key: str, tapes: Path) -> dict[str, Any]:
    return env.svc.ai_run(env.project_id, AGENT, task_kind=tasks.PROPOSE_FAMILY_CLASS.kind,
                          scan_gen=gen, paths=[key], cassettes=tapes)  # fmt: skip


VARIANT = {"result": {"answer": "variant", "current": None, "quote": None}, "abstain": None}


def test_the_family_task_runs_on_the_layer_with_two_models(env: Env, tmp_path: Path) -> None:
    gen = scan(env)
    family = fuel_family(env, gen)
    bundle = family_bundle(env, gen, family["key"])
    tapes = tmp_path / "tapes"
    cassette(tapes, tasks.PROPOSE_FAMILY_CLASS.kind, bundle.input_sha256, 0, VARIANT, "model/a")
    cassette(tapes, tasks.PROPOSE_FAMILY_CLASS.kind, bundle.input_sha256, 1, VARIANT, "model/b")
    done = run_family(env, gen, family["key"], tapes)
    [item] = done["items"]
    assert item["outcome"] == "accepted", item
    assert item["proposal"]["answer"] == "variant"
    # A proposal changes no class, decision or owner item.
    after = fuel_family(env, gen)
    assert (after["class"], after["class_decision"], after["needs_owner"]) == (
        "compete",
        None,
        True,
    )


def test_the_family_task_never_counts_a_reply_after_a_member_changed(
    env: Env, tmp_path: Path
) -> None:
    gen = scan(env)
    family = fuel_family(env, gen)
    bundle = family_bundle(env, gen, family["key"])
    tapes = tmp_path / "tapes"
    cassette(tapes, tasks.PROPOSE_FAMILY_CLASS.kind, bundle.input_sha256, 0, VARIANT, "model/a")
    cassette(tapes, tasks.PROPOSE_FAMILY_CLASS.kind, bundle.input_sha256, 1, VARIANT, "model/b")
    # A member changes while the first reply is out: the re-read of every member sees it.
    from prometheos.intelligence import run as layer_run

    real = layer_run.CassetteExecutor.sample

    def sample(self: Any, *args: Any, **kwargs: Any) -> Any:
        (env.research / "vehicles" / "van" / "fuel.csv").write_text("vehicle,litres\nvan,99\n")
        return real(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(layer_run.CassetteExecutor, "sample", sample)
        done = run_family(env, gen, family["key"], tapes)
    [item] = done["items"]
    assert item["outcome"] == "rejected" and "digest_changed" in item["reasons"], item


def test_an_outline_title_must_be_whole_words_of_its_quote(env: Env) -> None:
    """Review cp20-a-r1 A4: "rack 7" is inside "Track 7" but is not words of it."""
    gen = scan(env)
    got = submit(env, gen, "propose_outline", "NOTES.md", 1, {"items": [
        {"kind": "track", "line": 4, "quote": "Track 7 covers the hilly route.", "title": "rack 7"},
    ]}, "agent/a")  # fmt: skip
    assert got["accepted"] is False and "not words from the quote" in got["problems"][0]
