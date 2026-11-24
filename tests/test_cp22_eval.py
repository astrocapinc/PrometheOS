"""CP22.3 (A07; decision 0093): evaluation before trust (C22-EVAL, S70).

A scripted transport stands in for the provider; nothing leaves this computer.
- the one-sided Clopper-Pearson lower bound is exact (checked against closed forms and the
  defining identity);
- the record is fixed before measuring, never re-fixed with a lower target, and a measurement
  needs a fixed record;
- trust is written only from gateway replies bound to their call receipts, on the lower bound
  for every outcome given; replayed cassettes, even recorded ones, and a hand-edited cassette
  confer none;
- trust is bound to the task version, template, wire schema, model and provider set: a changed
  template or a changed provider set finds none.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cp1_support import Env

from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError
from prometheos.intelligence import evaluation as ev
from prometheos.intelligence import spend
from prometheos.intelligence.gateway import Gateway
from prometheos.intelligence.policy import EvaluatedModel, TaskGrant
from prometheos.intelligence.run import CassetteExecutor, GatewayExecutor
from prometheos.intelligence.tasks import CLASSIFY_FILE, Task
from prometheos.providers import transport as tp
from prometheos.providers.config import Budget, ProviderConfig
from prometheos.providers.secrets import MemorySecretStore
from prometheos.store.project import ProjectStore

ROLES = ("source_code", "notes")
MODEL = "mistralai/mistral-small-4"


# ----- the bound ------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 10, 29, 30, 59, 200])
def test_all_correct_gives_the_closed_form_bound(n: int) -> None:
    assert ev.clopper_pearson_lower(n, n) == pytest.approx(0.05 ** (1 / n), abs=1e-9)


def test_the_bound_meets_its_defining_identity_and_known_cases() -> None:
    for successes, n in ((95, 100), (40, 50), (1, 3), (17, 18)):
        low = ev.clopper_pearson_lower(successes, n)
        assert ev.betainc(successes, n - successes + 1, low) == pytest.approx(0.05, abs=1e-9)
        assert 0 < low < successes / n
    assert ev.clopper_pearson_lower(0, 40) == 0.0
    assert ev.clopper_pearson_lower(59, 59) >= 0.95 > ev.clopper_pearson_lower(58, 58)
    assert ev.clopper_pearson_lower(95, 100) == pytest.approx(0.8975, abs=5e-4)


# ----- the golden set, the record -------------------------------------------------------------


def golden(count_per_role: int = 30, salt: str = "") -> list[dict[str, Any]]:
    items = []
    for role in ROLES:
        for i in range(count_per_role):
            text = f"% {role} item {chr(97 + i % 26)}{chr(97 + i // 26)}{salt}"
            payload = {"file": f"{role}/{i}.m", "container": None, "size": len(text),
                       "preview": [{"n": 1, "text": text}], "roles": sorted(ROLES)}  # fmt: skip
            items.append({"item_id": f"{role}-{i}", "subject": f"{role}/{i}.m",
                          "subject_sha256": canonical_sha256({"t": text}), "input": payload,
                          "label": role})  # fmt: skip
    return items


def record(**change: Any) -> ev.EvaluationRecord:
    base: dict[str, Any] = {"task_kind": CLASSIFY_FILE.kind, "task_version": CLASSIFY_FILE.version,
                            "outcome_field": "role", "scoring": "exact_value",
                            "purpose": "acceptance", "golden": golden(),
                            "targets": {"precision_lower_bound": 0.90, "coverage": 0.9},
                            "confidence": 0.95, "method": "clopper_pearson_one_sided",
                            "min_items": 59}  # fmt: skip
    return ev.EvaluationRecord.model_validate(base | change)


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    spend.set_monthly_cap(tmp_path / "home", Decimal("5"))
    return ProjectStore(Env(tmp_path).store_dir)


def test_the_record_is_fixed_first_and_targets_never_drop(store: ProjectStore) -> None:
    with pytest.raises(PrometheosError, match="fix the evaluation record"):
        ev.measure(store, CLASSIFY_FILE, "0" * 64, CassetteExecutor([]))
    digest = ev.fix(store, record(), by="owner")
    assert ev.fix(store, record(), by="owner") == digest  # the same record, stored once
    lower = record(targets={"precision_lower_bound": 0.85, "coverage": 0.9})
    with pytest.raises(PrometheosError, match="never lowered"):
        ev.fix(store, lower, by="owner")
    with pytest.raises(PrometheosError, match="never lowered"):
        ev.fix(store, record(min_items=40), by="owner")
    ev.fix(store, record(targets={"precision_lower_bound": 0.95, "coverage": 0.9}), by="owner")
    with pytest.raises(ValueError):
        record(confidence=0.9)
    with pytest.raises(ValueError):
        record(min_items=61)


# ----- measuring ------------------------------------------------------------------------------


def reply(role: str, provider: str = "Mistral") -> dict[str, Any]:
    content = json.dumps({"result": {"role": role, "quote": None}, "abstain": None})
    return {"id": "g", "model": MODEL, "provider": provider,
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.00002},
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}]}  # fmt: skip


class Scripted:
    def __init__(self, bodies: list[dict[str, Any]]) -> None:
        self.bodies = list(bodies)

    def post(self, url: str, headers: dict[str, str], data: bytes, timeout: float) -> tp.Reply:
        return tp.Reply(200, json.dumps(self.bodies.pop(0)).encode())


PAIR = EvaluatedModel(model=MODEL, provider="mistral", company="mistralai",
                      supported_parameters=["temperature"], lowest_effort=None,
                      price_input_per_mtok_usd="0.15", price_output_per_mtok_usd="0.60",
                      metadata_as_of="2026-10-30")  # fmt: skip


def gateway(
    store: ProjectStore, bodies: list[dict[str, Any]], task: Task = CLASSIFY_FILE
) -> GatewayExecutor:
    secrets = MemorySecretStore()
    secrets.set("openrouter", "sk-or-test-not-real")
    config = ProviderConfig(
        policy="remote-allowed", endpoint="https://openrouter.ai/api/v1", model=MODEL,
        credential_ref="openrouter", price_input_per_mtok_usd="0.15",
        price_output_per_mtok_usd="0.60",
        budget=Budget(max_input_chars=60_000, max_output_tokens=2048, max_calls_per_run=2,
                      spend_cap_usd=None, timeout_seconds=60),
    )  # fmt: skip
    grant = TaskGrant(task_kind=task.kind, models=[PAIR],
                      single_company_reason="one evaluated pair while the spike is pending",
                      task_ceiling_usd="0.01")  # fmt: skip
    home = store.directory.parent.parent / "spend-home"
    spend.set_monthly_cap(home, Decimal("5"))
    gw = Gateway(config, secrets, Scripted(bodies), home, store.project_id)
    return GatewayExecutor(store, gw, grant, Decimal("1"))


def answers(wrong: int = 0, provider: str = "Mistral") -> list[dict[str, Any]]:
    out = [reply(g["label"], provider) for g in golden()]
    for i in range(wrong):
        out[i] = reply("notes", provider)  # a source_code item answered wrongly
    return out


def test_gateway_replies_bound_to_their_receipts_confer_trust(store: ProjectStore) -> None:
    digest = ev.fix(store, record(), by="owner")
    measured = ev.measure(store, CLASSIFY_FILE, digest, gateway(store, answers()))
    assert measured["bound_to_gateway_receipts"] and measured["meets_targets"]
    assert measured["trusted"] is True and measured["models"] == [MODEL]
    assert measured["coverage"] == 1.0 and measured["precision_lower_bound"] >= 0.95
    assert set(measured["per_outcome"]) == set(ROLES)
    assert ev.trusted(store, CLASSIFY_FILE, MODEL, ["Mistral"]) is True
    # a changed provider set, model or template finds no trust (withdrawn until re-measured)
    assert ev.trusted(store, CLASSIFY_FILE, MODEL, ["Mistral", "OtherHost"]) is False
    assert ev.trusted(store, CLASSIFY_FILE, "google/gemini-3.1-flash-lite", ["Mistral"]) is False
    changed = Task(**{**CLASSIFY_FILE.__dict__, "instructions": CLASSIFY_FILE.instructions + "!"})
    assert ev.trusted(store, changed, MODEL, ["Mistral"]) is False


def test_one_wrong_answer_in_an_outcome_withholds_trust(store: ProjectStore) -> None:
    digest = ev.fix(store, record(), by="owner")
    measured = ev.measure(store, CLASSIFY_FILE, digest, gateway(store, answers(wrong=1)))
    assert measured["precision"] > 0.98  # the point estimate would pass ...
    assert measured["per_outcome"]["notes"]["lower_bound"] < 0.90  # ... the bound does not
    assert measured["trusted"] is False
    assert ev.trusted(store, CLASSIFY_FILE, MODEL, ["Mistral"]) is False


def cassettes_from(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"task_kind": CLASSIFY_FILE.kind, "input_sha256": canonical_sha256(g["input"]),
             "sample": 0, "body": body}
            for g, body in zip(golden(), bodies, strict=True)]  # fmt: skip


def test_replayed_and_hand_edited_cassettes_confer_no_trust(store: ProjectStore) -> None:
    # a replay may measure a development record only (an acceptance record: K1)
    digest = ev.fix(store, record(purpose="development"), by="owner")
    replayed = ev.measure(store, CLASSIFY_FILE, digest, CassetteExecutor(cassettes_from(answers())))
    assert replayed["meets_targets"] is True and replayed["trusted"] is False
    assert replayed["bound_to_gateway_receipts"] is False and replayed["executor"] == "cassette"
    edited = cassettes_from(answers())
    edited[0]["body"] = reply("source_code")  # hand-edited to look right
    other = CassetteExecutor(edited, model="cassette/edited")
    assert ev.measure(store, CLASSIFY_FILE, digest, other)["trusted"] is False
    assert ev.trusted(store, CLASSIFY_FILE, "cassette/recorded", []) is False


def test_a_new_template_needs_a_new_measurement(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.intelligence import tasks as registered

    digest = ev.fix(store, record(), by="owner")
    assert ev.measure(store, CLASSIFY_FILE, digest, gateway(store, answers()))["trusted"]
    tuned = Task(**{**CLASSIFY_FILE.__dict__, "instructions": CLASSIFY_FILE.instructions + " "})
    monkeypatch.setitem(registered.REGISTRY, tuned.kind, tuned)  # what the registry holds now
    assert ev.trusted(store, tuned, MODEL, ["Mistral"]) is False
    # the same items again, under the changed prompt: seen, so never trusted (review cp22-a R10-F1)
    again = ev.measure(store, tuned, digest, gateway(store, answers(), tuned))
    assert again["trusted"] is False and "seen by an earlier measurement" in (
        ev.why_not(again, None) or ""
    )
    changed = Task(**{**tuned.__dict__, "version": CLASSIFY_FILE.version + 1})
    monkeypatch.setitem(registered.REGISTRY, changed.kind, changed)
    with pytest.raises(PrometheosError, match="already seen"):
        ev.fix(store, record(task_version=changed.version), by="owner")  # nor as a new record
    # new items for the new version: a new measurement, which may confer trust
    fresh = ev.fix(store, record(task_version=changed.version, golden=golden(salt="-v2")),
                   by="owner")  # fmt: skip
    measured = ev.measure(store, changed, fresh, gateway(store, answers(), changed))
    assert measured["trusted"] is True and measured["template_sha256"] == changed.template_sha256
    assert ev.trusted(store, changed, MODEL, ["Mistral"]) is True


def test_the_eval_tool_replays_cassettes_and_never_trusts(tmp_path: Path) -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "ai_eval", Path(__file__).resolve().parents[1] / "tools" / "ai_eval.py"
    )
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    sys.modules["ai_eval"] = tool
    spec.loader.exec_module(tool)
    development = record(purpose="development").model_dump(mode="json")
    (tmp_path / "record.json").write_text(json.dumps(development), encoding="utf-8")
    tapes = tmp_path / "cassettes"
    tapes.mkdir()
    for i, cassette in enumerate(cassettes_from(answers())):
        (tapes / f"{i:03d}.json").write_text(json.dumps(cassette), encoding="utf-8")
    out = tmp_path / "result.json"
    assert tool.main(["--record", str(tmp_path / "record.json"), "--cassettes", str(tapes),
                      "--out", str(out)]) == 0  # fmt: skip
    measured = json.loads(out.read_text(encoding="utf-8"))
    assert measured["meets_targets"] is True and measured["trusted"] is False
    assert measured["items"] == 60 and measured["executor"] == "cassette"


def test_0102_statements_are_scored_as_a_set_and_labels_may_name_alternatives() -> None:
    from prometheos.intelligence.evaluation import _matches, _value

    proposal = {"statements": [
        {"referent": "data/b.csv", "status": "superseded", "quote": "q", "line": 2, "date": None},
        {"referent": "data/a.csv", "status": "superseded", "quote": "q", "line": 1, "date": None},
    ]}  # fmt: skip
    assert _value(proposal, "statements") == "data/a.csv=superseded;data/b.csv=superseded"
    assert _value({"statements": []}, "statements") == ""
    assert _matches("", "") and not _matches("x=current", "")
    assert _matches("r=final_claimed", "r=current||r=final_claimed")
    assert not _matches("r=draft", "r=current||r=final_claimed")
    assert not _matches(None, "") and not _matches("notes", None)
