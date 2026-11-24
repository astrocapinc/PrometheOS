"""The intelligence layer on results waiting for interpretation (decision 0110, L2).

A rerun's output is a newer version of a file whose older version holds a claim's bound quantity.
The rules read the same labelled line; the layer (``results.draft_interpretation``) is used only
when the label is missing or repeated, and code reads the number on the line it proposes. Every
reply is a recorded cassette: nothing is sent, and cassette results confer no trust.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from cp7_support import create
from test_cp20_claim_status import claim, imported, line_ev, receipt
from test_cp20_layer import cassette

from prometheos.app import quantities, results_layer
from prometheos.app.apply import text_lines

KIND = "results.draft_interpretation"
OLD = "peak power = 412.5 kW\nenergy = 196.9 MJ\n"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "run.log").write_text(OLD, "utf-8")
    # The owner lowered the folder's sensitivity: a root reads as restricted until then (A07.4).
    environment.svc.ai_root_sensitivity_set(
        environment.project_id, "research", "normal", authority=OWNER_ACT
    )
    return environment


def bound_claim(env: Env, **extra: Any) -> tuple[str, dict[str, str]]:
    """A claim whose quantity 196.9 MJ is bound on line 2 of run.log's first version."""
    files = imported(env, ["run.log"])
    ev = line_ev(files["run.log"], 2)
    binding = {"decimal": "196.9", "unit": "MJ", "source_ref": ev,
               "source_pointer": "text:2:9:17", "name": "uphill energy"}  # fmt: skip
    cid = claim(env, [ev], statement="The uphill energy is 196.9 MJ.", quantities=[binding],
                **extra)  # fmt: skip
    return cid, files["run.log"]


def rerun(env: Env, text: str) -> dict[str, str]:
    """The rerun writes a new version of run.log and reports it in a bare receipt."""
    (env.research / "run.log").write_text(text, "utf-8")
    new = imported(env, ["run.log"])["run.log"]
    receipt(env, [], [new])
    return new


def drafts(env: Env, caller: Any = OWNER) -> list[dict[str, Any]]:
    items = env.svc.now(env.project_id, caller)["awaiting_interpretation"]["items"]
    return [d for item in items for d in item["drafts"]]


def layer_sha(env: Env) -> str:
    (subject,) = results_layer.subjects(env.svc.open(env.project_id), OWNER, None)
    return subject.bundle.input_sha256


def line(line_id: str) -> dict[str, Any]:
    return {"result": {"line_id": line_id}, "abstain": None}


def record(folder: Path, sha: str, first: dict[str, Any], second: dict[str, Any]) -> Path:
    cassette(folder, KIND, sha, 0, first, "vendor-a/model")
    cassette(folder, KIND, sha, 1, second, "vendor-b/model")
    return folder


def suggest(env: Env, folder: Path, caller: Any = AGENT) -> dict[str, Any]:
    (item,) = env.svc.ai_map_suggest(env.project_id, caller, task_kind=KIND,
                                     cassettes=folder)["items"]  # fmt: skip
    return item


# ----- rules first -------------------------------------------------------------------------------


def test_the_rules_read_the_same_labelled_line_and_draft_the_difference(env: Env) -> None:
    cid, _old = bound_claim(env)
    new = rerun(env, "peak power = 420.0 kW\nenergy = 201.3 MJ\n")
    before = env.seq()
    (draft,) = drafts(env)
    assert env.seq() == before  # a draft writes nothing
    assert (draft["claim_id"], draft["status"], draft["found_by"]) == (cid, "drafted", "label")
    assert draft["expected"]["decimal"] == "196.9" and draft["unit"] == "MJ"
    measured = draft["measured"]
    assert (measured["decimal"], measured["line"], measured["version_id"]) == (
        "201.3", 2, new["version_id"])  # fmt: skip
    assert draft["difference"] == "4.4" and draft["relative_difference_percent"] == "2.235"
    assert draft["tolerance"] is None  # never invented
    assert "No tolerance is set" in draft["summary"] and draft["layer_subject"] is None
    # The measured pointer binds as a quantity on the new version's exact bytes.
    lines = text_lines((env.research / "run.log").read_bytes())
    assert lines is not None
    quantities.verify_text(lines, 2, 2, {"decimal": "201.3", "unit": "MJ",
                                         "source_pointer": measured["source_pointer"]})  # fmt: skip


def test_a_negative_difference_keeps_its_sign(env: Env) -> None:
    bound_claim(env)
    rerun(env, "peak power = 420.0 kW\nenergy = 190 MJ\n")
    (draft,) = drafts(env)
    assert draft["difference"] == "-6.9" and draft["relative_difference_percent"] == "-3.504"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("energy = 201300 kJ\n", "unit_differs"),
        ("energy = 1,240 MJ\n", "unreadable_number"),
    ],
)
def test_a_different_unit_or_an_unplain_number_is_never_guessed(
    env: Env, text: str, status: str
) -> None:
    bound_claim(env)
    rerun(env, text)
    (draft,) = drafts(env)
    assert draft["status"] == status
    assert draft["measured"] is None and draft["difference"] is None


def test_no_draft_without_a_newer_output_of_the_bound_source(env: Env) -> None:
    bound_claim(env)
    (env.research / "other.log").write_text("energy = 1 MJ\n", "utf-8")
    other = imported(env, ["other.log"])["other.log"]
    receipt(env, [], [other])
    items = env.svc.now(env.project_id, OWNER)["awaiting_interpretation"]["items"]
    assert items and all(item["drafts"] == [] for item in items)


def test_a_claim_the_caller_cannot_see_is_not_drafted_for_it(env: Env) -> None:
    cid, _old = bound_claim(env)
    body = env.svc.get(env.project_id, OWNER, cid, None)["body"]
    env.apply_protected([{"op": "revise", "id": cid, "base_rev": 1, "sensitivity": "restricted",
                          "occurred_at": None, "body": body}], caller=OWNER)  # fmt: skip
    rerun(env, "energy = 201.3 MJ\n")
    assert drafts(env, AGENT) == []  # the agent cannot see the restricted claim
    assert [d["status"] for d in drafts(env, OWNER)] == ["drafted"]


# ----- the layer, only when the rules cannot read it ---------------------------------------------

RENAMED = "E_total = 201.3 MJ\nE_regen = 63.6 MJ\npeak = 420.0 kW\n"


def test_a_missing_label_goes_to_the_layer_and_code_reads_the_proposed_line(
    env: Env, tmp_path: Path
) -> None:
    bound_claim(env)
    rerun(env, RENAMED)
    (draft,) = drafts(env)
    assert draft["status"] == "label_not_found" and draft["measured"] is None
    (subject,) = results_layer.subjects(env.svc.open(env.project_id), OWNER, None)
    assert subject.bundle.input["line_ids"] == ["L1", "L2"]  # only lines with a number in MJ
    assert draft["layer_subject"] == subject.bundle.subject
    before = env.seq()
    item = suggest(env, record(tmp_path / "tapes", layer_sha(env), line("L1"), line("L1")))
    assert item["outcome"] == "accepted" and env.seq() == before
    (draft,) = drafts(env)
    assert (draft["status"], draft["found_by"]) == ("drafted", "layer")
    assert draft["measured"]["decimal"] == "201.3" and draft["measured"]["line"] == 1
    assert draft["difference"] == "4.4" and draft["tolerance"] is None
    assert draft["layer"]["agree"] is True and draft["layer"]["hint_only"] is True
    assert draft["layer"]["executors"] == ["cassette"]


def test_a_repeated_label_goes_to_the_layer_too(env: Env, tmp_path: Path) -> None:
    bound_claim(env)
    rerun(env, "energy = 201.3 MJ\nenergy = 63.6 MJ\n")
    (draft,) = drafts(env)
    assert draft["status"] == "label_ambiguous"
    suggest(env, record(tmp_path / "tapes", layer_sha(env), line("L2"), line("L2")))
    (draft,) = drafts(env)
    assert draft["status"] == "drafted" and draft["measured"]["decimal"] == "63.6"


@pytest.mark.parametrize(
    ("second", "outcome", "reason"),
    [
        (line("L3"), "rejected", "unoffered_option"),  # the kW line was not offered
        ({"result": {"line_id": "L1", "value": "201.3"}, "abstain": None}, "rejected", "shape"),
        (line("L2"), "disagreed", "disagreement"),
    ],
)
def test_an_unoffered_line_a_number_of_its_own_or_disagreement_drafts_nothing(
    env: Env, tmp_path: Path, second: dict[str, Any], outcome: str, reason: str
) -> None:
    bound_claim(env)
    rerun(env, RENAMED)
    item = suggest(env, record(tmp_path / "tapes", layer_sha(env), line("L1"), second))
    assert item["outcome"] == outcome and reason in item["reasons"]
    (draft,) = drafts(env)
    assert draft["status"] == "label_not_found" and draft["measured"] is None
    assert (draft["layer"] or {}).get("agree") in (None, False)


def test_one_model_twice_is_no_agreement(env: Env, tmp_path: Path) -> None:
    bound_claim(env)
    rerun(env, RENAMED)
    folder = tmp_path / "tapes"
    for i in (0, 1):
        cassette(folder, KIND, layer_sha(env), i, line("L1"), "vendor-a/model")
    assert suggest(env, folder)["outcome"] != "accepted"
    assert drafts(env)[0]["measured"] is None


def test_a_line_with_several_values_is_left_to_the_reader(env: Env, tmp_path: Path) -> None:
    bound_claim(env)
    rerun(env, "E_total = 201.3 MJ, E_regen = 63.6 MJ\n")
    suggest(env, record(tmp_path / "tapes", layer_sha(env), line("L1"), line("L1")))
    (draft,) = drafts(env)
    assert draft["status"] == "several_values" and draft["measured"] is None


def test_a_restricted_input_is_never_sent(env: Env, tmp_path: Path) -> None:
    cid, _old = bound_claim(env)
    body = env.svc.get(env.project_id, OWNER, cid, None)["body"]
    env.apply_protected([{"op": "revise", "id": cid, "base_rev": 1, "sensitivity": "restricted",
                          "occurred_at": None, "body": body}], caller=OWNER)  # fmt: skip
    rerun(env, RENAMED)
    (tmp_path / "tapes").mkdir()
    item = suggest(env, tmp_path / "tapes", OWNER)
    assert (item["outcome"], item["reasons"]) == ("denied", ["policy_denied"])


def test_a_proposal_for_another_bundle_is_not_shown(env: Env, tmp_path: Path) -> None:
    """A proposal belongs to its exact bundle: a second rerun with other lines is a new bundle,
    so it shows no draft from the first one's proposal (the first keeps its own)."""
    bound_claim(env)
    rerun(env, RENAMED)
    suggest(env, record(tmp_path / "tapes", layer_sha(env), line("L1"), line("L1")))
    assert drafts(env)[0]["found_by"] == "layer"
    rerun(env, "E_sum = 201.3 MJ\nE_regen = 63.6 MJ\nE_x = 5.0 MJ\n")
    found = {d["found_by"] for d in drafts(env)}
    assert found == {None, "layer"}  # the first rerun keeps its own draft; the second has none


def test_outside_a_standing_policy_nothing_is_sent(env: Env) -> None:
    from prometheos.domain.models import PrometheosError

    bound_claim(env)
    rerun(env, RENAMED)
    with pytest.raises(PrometheosError) as err:
        env.svc.ai_map_suggest(env.project_id, AGENT, task_kind=KIND, dry_run=True)
    assert err.value.code == "capability_unavailable"


def test_an_adoption_task_is_not_a_record_task(env: Env, tmp_path: Path) -> None:
    from prometheos.domain.models import PrometheosError

    (tmp_path / "tapes").mkdir()
    with pytest.raises(PrometheosError) as err:
        env.svc.ai_map_suggest(env.project_id, AGENT, task_kind="adoption.classify_file",
                               cassettes=tmp_path / "tapes")  # fmt: skip
    assert err.value.code == "validation_error"


# ----- review map-ai-spread-b round 1 ------------------------------------------------------------


def test_r1_f1_a_restricted_folder_or_a_restricted_citing_record_keeps_the_lines_home(
    env: Env, tmp_path: Path
) -> None:
    """The bundle carries file lines, so the file's own sensitivity counts (A07.4): its root, and
    every record citing it."""
    bound_claim(env)
    new = rerun(env, RENAMED)
    env.svc.ai_root_sensitivity_set(env.project_id, "research", "restricted", authority=OWNER_ACT)
    (tmp_path / "none").mkdir()
    item = suggest(env, tmp_path / "none")
    assert (item["outcome"], item["reasons"]) == ("denied", ["policy_denied"])
    env.svc.ai_root_sensitivity_set(env.project_id, "research", "normal", authority=OWNER_ACT)
    note = {
        "title": "Private reading", "text": "A note on the new output.", "category": "note",
        "evidence": [line_ev(new, 1)], "claim_refs": [], "author_ref": None, "origin": "typed",
        "quantities": [], "review": "unreviewed",
    }  # fmt: skip
    env.apply_protected([create("entry", note, sensitivity="restricted")], caller=OWNER)
    item = suggest(env, tmp_path / "none")
    assert (item["outcome"], item["reasons"]) == ("denied", ["policy_denied"])


def own_claim(env: Env, text: str, line: int, start: int, value: str, unit: str) -> None:
    (env.research / "run.log").write_text(text, "utf-8")
    first = imported(env, ["run.log"])["run.log"]
    ev = line_ev(first, line)
    end = start + len(value) + (len(unit) + 1 if unit else 0)
    binding = {"decimal": value, "unit": unit, "source_ref": ev,
               "source_pointer": f"text:{line}:{start}:{end}"}  # fmt: skip
    claim(env, [ev], statement=f"The value is {value} {unit}.", quantities=[binding])


def test_r1_f2_a_label_repeated_in_the_old_version_is_never_read_as_the_bound_line(
    env: Env,
) -> None:
    own_claim(env, "Run A\nPeak: 5.0 kW\nRun B\nPeak: 7.0 kW\n", 4, 6, "7.0", "kW")
    rerun(env, "Run A\nPeak: 5.1 kW\n")
    (draft,) = drafts(env)
    assert draft["status"] == "label_ambiguous" and draft["measured"] is None
    assert draft["layer_subject"] is not None  # the layer may propose; code would read it


def test_r1_f3_the_relative_difference_is_rounded_once() -> None:
    assert results_layer._difference("3", "4.23456") == ("1.23456", "41.15")


@pytest.mark.parametrize(("text", "status"), [("count = 13 kJ\n", "unit_differs"),
                                              ("count = 13\n", "drafted")])  # fmt: skip
def test_r1_f4_a_unitless_value_that_now_carries_a_unit_is_not_compared(
    env: Env, text: str, status: str
) -> None:
    own_claim(env, "count = 12\n", 1, 8, "12", "")
    rerun(env, text)
    (draft,) = drafts(env)
    assert draft["status"] == status
