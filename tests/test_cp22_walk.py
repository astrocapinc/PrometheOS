"""C22-WALK (CP22.6, decision 0099): the walk harness (tools/walk_cp22.py) and its guards.

The full walk (the hub in a browser and the agent over MCP, then a second store over MCP alone)
is the checkpoint suite's ``walk`` command. Here its judgement is mutation-tested: a store
divergence, a missing step, a different outcome or agent answer, and every wrong browser
observation fail; the spec and the harness hand over the same signals; and the walk switches on
the replay transport only for itself, with a fake key under a reference no one else uses.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "hub" / "walk22" / "walk.spec.ts"


def load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("walk_cp22", REPO / "tools" / "walk_cp22.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["walk_cp22"] = module
    spec.loader.exec_module(module)
    return module


walk = load_tool()


def legs() -> tuple[dict[str, Any], dict[str, Any]]:
    steps = [{"id": i, "outcome": "ok", "answer_sha256": "a" * 64} for i in walk.SCRIPT_IDS]
    doc = {"hub": {"steps": copy.deepcopy(steps)}, "mcp": {"steps": copy.deepcopy(steps)}}
    outcomes = {"hub": {"policy": [["revoked", 1]], "calls": [["x", "succeeded"]]}}
    outcomes["mcp"] = copy.deepcopy(outcomes["hub"])
    return doc, outcomes


def test_equal_legs_pass_and_each_mutation_fails() -> None:
    doc, outcomes = legs()
    assert walk.judge(doc, outcomes)[1] == []
    doc, outcomes = legs()
    outcomes["mcp"]["calls"].append(["x", "succeeded"])  # one more call left on one leg
    assert any("stored outcomes" in f for f in walk.judge(doc, outcomes)[1])
    doc, outcomes = legs()
    doc["hub"]["steps"] = [s for s in doc["hub"]["steps"] if s["id"] != "K7"]
    assert any("missing" in f for f in walk.judge(doc, outcomes)[1])
    doc, outcomes = legs()
    next(s for s in doc["mcp"]["steps"] if s["id"] == "K6")["outcome"] = "error:forbidden"
    assert any("outcomes differ" in f for f in walk.judge(doc, outcomes)[1])
    doc, outcomes = legs()
    next(s for s in doc["hub"]["steps"] if s["id"] == "K4")["answer_sha256"] = "b" * 64
    assert any("agent answers differ" in f for f in walk.judge(doc, outcomes)[1])


GOOD = {"K1": {"off": True, "says_instead": True, "restricted_roots": 2, "adoption_off": True,
               "adoption_instead": True},
        "K7": {"adoption_tagged": True, "tagged": True, "used": True},
        "K8": {"off": True}}  # fmt: skip
BAD = [("K1", {"off": False}), ("K1", {"says_instead": False}), ("K1", {"restricted_roots": 1}),
       ("K1", {"adoption_off": False}), ("K1", {"adoption_instead": False}),
       ("K7", {"adoption_tagged": False}), ("K7", {"tagged": False}), ("K7", {"used": False}),
       ("K8", {"off": False})]  # fmt: skip


def test_browser_observations_are_judged() -> None:
    for step_id, shown in GOOD.items():
        assert all(c["passed"] for c in walk.browser_checks(step_id, shown))
        assert not all(c["passed"] for c in walk.browser_checks(step_id, {}))


@pytest.mark.parametrize(("step_id", "change"), BAD)
def test_every_wrong_observation_fails(step_id: str, change: dict[str, Any]) -> None:
    assert not all(c["passed"] for c in walk.browser_checks(step_id, {**GOOD[step_id], **change}))


def test_the_spec_and_the_harness_cover_the_same_steps_and_signals() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert re.findall(r'record\(\{\s*id: "(K\d)"', text) == walk.BROWSER_IDS
    harness = (REPO / "tools" / "walk_cp22.py").read_text(encoding="utf-8")
    assert set(re.findall(r'signal\("([a-z-]+)"', text)) == set(
        re.findall(r'leg\.wait\("([a-z-]+)"', harness)
    )
    assert set(re.findall(r'waitFor\("([a-z-]+)"', text)) == set(
        re.findall(r'leg\.send\("([a-z-]+)"', harness)
    )
    assert 'import { guardedTest as test } from "../e2e/boundary-guard";' in text


def test_the_walk_switches_replay_on_for_itself_with_a_fake_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # setenv first registers each variable's absence, so teardown removes what the walk sets
    # (review cp22-a R4-F1: delenv of an absent name restores nothing).
    for name in WALK_VARIABLES:
        monkeypatch.setenv(name, "")
    replies = walk.prepare_environment(tmp_path)
    assert os.environ["PROMETHEOS_TEST_HOOKS"] == "1"
    assert os.environ["PROMETHEOS_TEST_REPLAY"] == str(replies)
    assert (
        walk.KEY_REF == "walk-key" and "not-a-real-key" in os.environ["PROMETHEOS_SECRET_WALK_KEY"]
    )


WALK_VARIABLES = ("PROMETHEOS_TEST_HOOKS", "PROMETHEOS_TEST_REPLAY", "PROMETHEOS_SECRET_WALK_KEY")


def test_r4_f1_the_walk_environment_leaves_with_its_test(tmp_path: Path) -> None:
    before = {name: os.environ.get(name) for name in WALK_VARIABLES}
    with pytest.MonkeyPatch.context() as mp:
        test_the_walk_switches_replay_on_for_itself_with_a_fake_key(tmp_path, mp)
        assert os.environ["PROMETHEOS_TEST_HOOKS"] == "1"
    assert {name: os.environ.get(name) for name in WALK_VARIABLES} == before


def test_d2_under_the_test_switch_the_key_comes_from_the_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app.service import Service
    from prometheos.providers.secrets import EnvSecretStore

    for name in WALK_VARIABLES:
        monkeypatch.setenv(name, "")
    walk.prepare_environment(tmp_path)
    store = Service(tmp_path / "home")._secrets()
    assert isinstance(store, EnvSecretStore)  # never the OS credential store
    assert store.get(walk.KEY_REF) == "sk-or-walk-not-a-real-key"


def test_ids_inside_messages_are_normalised_and_nothing_else() -> None:
    one = {"message": "Approval request apreq_6ef54a30-29d0-44a6-bc64-d6395f944010 waits"}
    two = {"message": "Approval request apreq_f0d8a90f-5bb4-486d-9324-1623a64ca16b waits"}
    assert walk.normalise(one) == walk.normalise(two)
    assert walk.normalise({"message": "a different answer"}) != walk.normalise(one)
