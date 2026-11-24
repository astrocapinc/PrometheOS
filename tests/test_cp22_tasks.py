"""CP22.2 (A07; decision 0092): tasks, validators, the result cache and a task run.

Recorded reply bodies (cassettes) and a scripted transport stand in for a model; nothing leaves
this computer. Covered (C22-TASKS, S69; the batch parts of C22-GATEWAY, S68):
- the wire schema is the strict model's in the accepted subset, and the validator applies the
  strict model;
- a wrong quote, a changed digest, an unbound number, an unoffered id and two disagreeing samples
  are each rejected with their reason and none becomes a proposal; a valid reply becomes one
  proposal; abstaining is its own outcome;
- a cache hit returns the stored sample and creates nothing; a new sample never alters one;
- restricted input is denied before any executor; a batch stops cleanly at a cap, at the key's
  limit and at an unknown outcome, keeps finished subjects, and never repeats the unknown one;
- a reply from outside the evaluated list is a hint only.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cp1_support import Env
from pydantic import model_validator

from prometheos.adoption import assist
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import StrictModel
from prometheos.intelligence import results, spend
from prometheos.intelligence.gateway import Gateway
from prometheos.intelligence.policy import EvaluatedModel, TaskGrant
from prometheos.intelligence.run import CassetteExecutor, GatewayExecutor, Subject, run
from prometheos.intelligence.tasks import (
    CLASSIFY_FILE,
    STATUS_STATEMENTS,
    Abstain,
    Task,
)
from prometheos.intelligence.validate import check, unbound_numbers
from prometheos.intelligence.wire import wire_schema
from prometheos.providers import transport as tp
from prometheos.providers.config import Budget, ProviderConfig
from prometheos.providers.secrets import MemorySecretStore
from prometheos.store.project import ProjectStore

README = [
    "# Thermal model",
    "The current model is models/thermal_v3.slx; thermal_v2.slx is superseded since 2026-03-01.",
    "Run the sweep with scripts/sweep.m.",
]


def status_bundle(lines: list[str] = README) -> assist.Bundle:
    data = "\n".join(lines).encode()
    payload = {"document": "notes/README.md",
               "lines": [{"n": i, "text": t} for i, t in enumerate(lines, start=1)],
               "candidates": ["models/thermal_v2.slx", "models/thermal_v3.slx"]}  # fmt: skip
    return assist.Bundle("extract_status_statements", "notes/README.md",
                         hashlib.sha256(data).hexdigest(), payload, canonical_sha256(payload),
                         assist.template_sha256("extract_status_statements"))  # fmt: skip


def classify_bundle(text: str = "% sweep the flywheel speed") -> assist.Bundle:
    payload = {"file": "scripts/sweep.m", "container": None, "size": len(text),
               "preview": [{"n": 1, "text": text}],
               "roles": sorted(assist.Zone.__args__)}  # type: ignore[attr-defined]  # fmt: skip
    return assist.Bundle("classify_file", "scripts/sweep.m",
                         hashlib.sha256(text.encode()).hexdigest(), payload,
                         canonical_sha256(payload),
                         assist.template_sha256("classify_file"))  # fmt: skip


GOOD_STATUS = {"result": {"statements": [{
    "quote": "thermal_v2.slx is superseded since 2026-03-01", "line": 2,
    "referent": "models/thermal_v2.slx", "status": "superseded", "date": "2026-03-01"}]},
    "abstain": None}  # fmt: skip


def body(content: Any, model: str = "mistralai/mistral-small-4", provider: str = "Mistral",
         cost: float | None = 0.0001, finish: str = "stop") -> dict[str, Any]:  # fmt: skip
    text = content if isinstance(content, str) else json.dumps(content)
    usage: dict[str, Any] = {"prompt_tokens": 200, "completion_tokens": 40}
    if cost is not None:
        usage["cost"] = cost
    return {"id": "gen", "model": model, "provider": provider, "usage": usage,
            "choices": [{"message": {"content": text}, "finish_reason": finish}]}  # fmt: skip


def cassette(task: Task, bundle: assist.Bundle, sample: int, content: Any,
             **extra: Any) -> dict[str, Any]:  # fmt: skip
    return {"task_kind": task.kind, "input_sha256": bundle.input_sha256, "sample": sample,
            "body": body(content)} | extra  # fmt: skip


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(Env(tmp_path).store_dir)


def subject(
    bundle: assist.Bundle, sensitivity: str = "normal", digest: str | None = "same"
) -> Subject:
    return Subject(bundle, bundle.subject_sha256 if digest == "same" else digest, sensitivity,
                   [f"study:{bundle.subject}"])  # fmt: skip


# ----- the wire schema and the strict model -----------------------------------------------------


def test_the_wire_schema_is_the_accepted_subset_and_the_strict_model_still_decides() -> None:
    wire = STATUS_STATEMENTS.wire
    text = json.dumps(wire)
    assert "$ref" not in text and "$defs" not in text and "oneOf" not in text
    for keyword in ("minLength", "maxLength", "pattern", "maximum", "minimum", "maxItems", "title"):
        assert keyword not in text, keyword

    def objects(node: Any) -> list[dict[str, Any]]:
        if isinstance(node, dict):
            found = [node] if node.get("type") == "object" else []
            return found + [o for v in node.values() for o in objects(v)]
        if isinstance(node, list):
            return [o for v in node for o in objects(v)]
        return []

    for obj in objects(wire):
        assert obj["additionalProperties"] is False and obj["required"] == list(obj["properties"])
    # A reply the wire schema allows but the strict model refuses (a quote too short) is shape.
    short = {"result": {"statements": [{"quote": "short", "line": 2, "referent": None,
                                        "status": "superseded", "date": None}]},
             "abstain": None}  # fmt: skip
    assert check(STATUS_STATEMENTS, status_bundle(), json.dumps(short),
                 current_digest=status_bundle().subject_sha256).reasons == ("shape",)  # fmt: skip
    assert wire_schema(CLASSIFY_FILE.output) == CLASSIFY_FILE.wire


def test_the_template_digest_binds_instructions_version_and_wire() -> None:
    other = Task(**{**CLASSIFY_FILE.__dict__, "instructions": CLASSIFY_FILE.instructions + " "})
    assert other.template_sha256 != CLASSIFY_FILE.template_sha256
    newer = Task(**{**CLASSIFY_FILE.__dict__, "version": CLASSIFY_FILE.version + 1})
    assert newer.template_sha256 != CLASSIFY_FILE.template_sha256


# ----- the validators (S69: each mutation rejected with its reason) ------------------------


def test_a_valid_reply_is_accepted_and_abstaining_is_its_own_outcome() -> None:
    bundle = status_bundle()
    verdict = check(STATUS_STATEMENTS, bundle, json.dumps(GOOD_STATUS),
                    current_digest=bundle.subject_sha256)  # fmt: skip
    assert verdict.outcome == "accepted" and verdict.output == GOOD_STATUS
    abstained = {"result": None, "abstain": {"reason": "not_enough_evidence"}}
    assert check(STATUS_STATEMENTS, bundle, json.dumps(abstained),
                 current_digest=bundle.subject_sha256).outcome == "abstained"  # fmt: skip
    both = {"result": GOOD_STATUS["result"], "abstain": {"reason": "not_enough_evidence"}}
    assert check(STATUS_STATEMENTS, bundle, json.dumps(both),
                 current_digest=bundle.subject_sha256).reasons == ("shape",)  # fmt: skip


def mutate(**change: Any) -> dict[str, Any]:
    reply = json.loads(json.dumps(GOOD_STATUS))
    reply["result"]["statements"][0].update(change)
    return reply


@pytest.mark.parametrize(
    ("reply", "digest", "reason"),
    [
        (mutate(quote="thermal_v2.slx was replaced long ago"), None, "quote_not_found"),
        (mutate(line=3), None, "quote_not_found"),
        (GOOD_STATUS, "0" * 64, "digest_changed"),
        (mutate(referent="models/thermal_v9.slx"), None, "unoffered_option"),
        (mutate(status="withdrawn"), None, "claim_not_bound"),
        ("not json at all", None, "shape"),
        ({"result": {"statements": []}, "abstain": None, "extra": 1}, None, "shape"),
    ],
)
def test_each_mutation_is_rejected_with_its_reason(reply: Any, digest: str | None,
                                                    reason: str) -> None:  # fmt: skip
    bundle = status_bundle()
    text = reply if isinstance(reply, str) else json.dumps(reply)
    verdict = check(STATUS_STATEMENTS, bundle, text, current_digest=digest or bundle.subject_sha256)
    assert verdict.outcome == "rejected" and reason in verdict.reasons and verdict.output is None


class Worded(StrictModel):
    question_id: str
    text: str


class WordedOutput(StrictModel):
    result: Worded | None
    abstain: Abstain | None

    @model_validator(mode="after")
    def _one(self) -> WordedOutput:
        if (self.result is None) == (self.abstain is None):
            raise ValueError("exactly one")
        return self


WORDED = Task(kind="test.word_card", version=1, output=WordedOutput, instructions="word it",
              agreement=1, effect="card_text", max_output_tokens=100,
              generated_text=("result.text",),
              offered=(("result.question_id", "questions"),))  # fmt: skip


def worded_bundle() -> assist.Bundle:
    payload = {"questions": ["q_speed", "q_loss"], "quantities": ["qt_loss"]}
    return assist.Bundle("word_card", "claim_1", "c" * 64, payload, canonical_sha256(payload), "t")


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        ({"question_id": "q_loss", "text": "Loss falls to 12 percent"}, "unbound_number"),
        ({"question_id": "q_loss", "text": "Loss falls to {q:qt_other}"}, "unbound_number"),
        ({"question_id": "q_unknown", "text": "Loss falls to {q:qt_loss}"}, "unoffered_option"),
    ],
)
def test_generated_text_takes_only_bound_numbers_and_offered_ids(result: dict[str, Any],
                                                                 reason: str) -> None:  # fmt: skip
    bundle = worded_bundle()
    reply = json.dumps({"result": result, "abstain": None})
    verdict = check(WORDED, bundle, reply, current_digest="c" * 64)
    assert verdict.outcome == "rejected" and reason in verdict.reasons
    good = json.dumps({"result": {"question_id": "q_loss", "text": "Loss falls to {q:qt_loss}"},
                       "abstain": None})  # fmt: skip
    assert check(WORDED, bundle, good, current_digest="c" * 64).outcome == "accepted"
    assert unbound_numbers("no digits here", frozenset()) is False


# ----- the run: cassettes, agreement, cache ------------------------------------------------------


def test_two_agreeing_samples_make_one_proposal_and_disagreement_none(store: ProjectStore) -> None:
    agreeing, split = status_bundle(), status_bundle([*README, "Spare line."])
    other = mutate(quote="The current model is models/thermal_v3.slx",
                   referent="models/thermal_v3.slx",
                   status="current", date=None)  # fmt: skip
    # each sample from a different recorded model: CP17.4's agreement rule (review cp22-a R4-F2)
    tapes = CassetteExecutor([
        cassette(STATUS_STATEMENTS, agreeing, 0, GOOD_STATUS, model="cassette/a"),
        cassette(STATUS_STATEMENTS, agreeing, 1, GOOD_STATUS, model="cassette/b"),
        cassette(STATUS_STATEMENTS, split, 0, GOOD_STATUS, model="cassette/a"),
        cassette(STATUS_STATEMENTS, split, 1, other, model="cassette/b"),
    ])  # fmt: skip
    done = run(store, STATUS_STATEMENTS, [subject(agreeing), subject(split)], tapes)
    first, second = done["items"]
    assert first["outcome"] == "accepted" and first["proposal"] == GOOD_STATUS["result"]
    assert first["hint_only"] is False and len(first["samples"]) == 2
    assert second["outcome"] == "disagreed" and second["proposal"] is None
    assert done["executor"] == "cassette" and done["new_samples"] == 4
    rows = results.listing(store)
    assert {r["executor"] for r in rows} == {"cassette"} and len(rows) == 4


def test_r4_f2_two_agreeing_samples_of_one_model_are_no_proposal(store: ProjectStore) -> None:
    bundle = status_bundle()
    tapes = CassetteExecutor([
        cassette(STATUS_STATEMENTS, bundle, 0, GOOD_STATUS),
        cassette(STATUS_STATEMENTS, bundle, 1, GOOD_STATUS),
    ])  # fmt: skip
    [item] = run(store, STATUS_STATEMENTS, [subject(bundle)], tapes)["items"]
    assert item["outcome"] == "rejected" and item["reasons"] == ["one_model"]


def test_a_cache_hit_creates_nothing_and_a_sample_is_never_altered(store: ProjectStore) -> None:
    bundle = classify_bundle()
    reply = {
        "result": {"role": "source_code", "quote": "% sweep the flywheel speed"},
        "abstain": None,
    }
    first = run(store, CLASSIFY_FILE, [subject(bundle)],
                CassetteExecutor([cassette(CLASSIFY_FILE, bundle, 0, reply)]))  # fmt: skip
    assert first["items"][0]["outcome"] == "accepted" and first["items"][0]["hint_only"] is True
    changed = {"result": {"role": "notes", "quote": None}, "abstain": None}
    again = run(store, CLASSIFY_FILE, [subject(bundle)],
                CassetteExecutor([cassette(CLASSIFY_FILE, bundle, 0, changed)]))  # fmt: skip
    assert again["cached_samples"] == 1 and again["new_samples"] == 0
    assert again["items"][0]["proposal"] == reply["result"]  # the stored sample, unaltered
    assert len(results.listing(store)) == 1


def test_restricted_input_is_denied_before_any_executor(store: ProjectStore) -> None:
    class Refuses:
        name = "gateway"

        def models(self) -> list[str]:
            return ["m/x"]

        def params_sha256(self, model: str) -> str:
            return "p"

        def sample(self, *args: Any) -> Any:
            raise AssertionError("restricted input reached an executor")

    done = run(store, CLASSIFY_FILE, [subject(classify_bundle(), "restricted"),
                                      subject(classify_bundle("% other"), "private")],
               Refuses())  # fmt: skip
    assert [i["outcome"] for i in done["items"]] == ["denied", "denied"]
    assert done["items"][0]["reasons"] == ["policy_denied"]


def test_a_changed_file_rejects_the_reply_as_stale(store: ProjectStore) -> None:
    bundle = classify_bundle()
    reply = {"result": {"role": "source_code", "quote": None}, "abstain": None}
    done = run(store, CLASSIFY_FILE, [subject(bundle, digest="f" * 64)],
               CassetteExecutor([cassette(CLASSIFY_FILE, bundle, 0, reply)]))  # fmt: skip
    assert done["items"][0]["outcome"] == "rejected"
    assert done["items"][0]["reasons"] == ["digest_changed"]


# ----- the run on the gateway (S68 at batch level) ------------------------------------------------


A = EvaluatedModel(model="mistralai/mistral-small-4", provider="mistral", company="mistralai",
                   supported_parameters=["temperature", "seed"], lowest_effort=None,
                   price_input_per_mtok_usd="0.15", price_output_per_mtok_usd="0.60",
                   metadata_as_of="2026-10-30")  # fmt: skip
B = A.model_copy(update={"model": "google/gemini-3.1-flash-lite", "provider": "google-vertex",
                         "company": "google"})  # fmt: skip
GRANT = TaskGrant(task_kind=CLASSIFY_FILE.kind, models=[A, B], single_company_reason=None,
                  task_ceiling_usd="0.05")  # fmt: skip


class Scripted:
    def __init__(self, answers: list[Any]) -> None:
        self.answers, self.sent = list(answers), 0

    def post(self, url: str, headers: dict[str, str], data: bytes, timeout: float) -> tp.Reply:
        self.sent += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]


def ok(role: str = "source_code", **extra: Any) -> tp.Reply:
    return tp.Reply(200, json.dumps(body({"result": {"role": role, "quote": None},
                                          "abstain": None}, **extra)).encode())  # fmt: skip


def gateway_executor(store: ProjectStore, home: Path, transport: Scripted,
                     cap: str = "0.10") -> GatewayExecutor:  # fmt: skip
    secrets = MemorySecretStore()
    secrets.set("openrouter", "sk-or-test-not-real")
    config = ProviderConfig(
        policy="remote-allowed", endpoint="https://openrouter.ai/api/v1", model=A.model,
        credential_ref="openrouter", price_input_per_mtok_usd="0.15",
        price_output_per_mtok_usd="0.60",
        budget=Budget(max_input_chars=60_000, max_output_tokens=2048, max_calls_per_run=2,
                      spend_cap_usd=None, timeout_seconds=60),
    )  # fmt: skip
    gateway = Gateway(config, secrets, transport, home, store.project_id)
    return GatewayExecutor(store, gateway, GRANT, Decimal(cap))


def subjects(count: int) -> list[Subject]:
    return [subject(classify_bundle(f"% script number {chr(97 + i)}")) for i in range(count)]


def test_a_batch_stops_cleanly_at_the_rate_limit_and_keeps_finished_items(
    store: ProjectStore, tmp_path: Path
) -> None:
    spend.set_monthly_cap(tmp_path / "home", Decimal("1"))
    transport = Scripted([ok(), ok(), tp.Reply(429, b"{}"), ok()])
    done = run(
        store, CLASSIFY_FILE, subjects(5), gateway_executor(store, tmp_path / "home", transport)
    )
    assert [i["outcome"] for i in done["items"]] == ["accepted", "accepted", "not_run", "not_run",
                                                     "not_run"]  # fmt: skip
    assert done["stopped"]["reason"] == "rate_limited" and transport.sent == 3
    conn = sqlite3.connect(store.db_path)
    try:
        calls = conn.execute("SELECT state, served_model, data_policy, bundle_digest, reply_body "
                             "FROM provider_calls").fetchall()  # fmt: skip
    finally:
        conn.close()
    # every call that left is recorded, the refused one too, each with its bundle's digest
    assert [c[0] for c in calls] == ["succeeded", "succeeded", "failed"]
    assert all('"zdr": true' in c[2] and c[3] for c in calls)
    assert all(c[1] == A.model and c[4] for c in calls[:2])


def test_an_unknown_outcome_is_held_and_never_repeated(store: ProjectStore, tmp_path: Path) -> None:
    spend.set_monthly_cap(tmp_path / "home", Decimal("1"))
    batch = subjects(3)
    transport = Scripted([ok(), tp.OutcomeUnknown("TimeoutError")])
    done = run(store, CLASSIFY_FILE, batch, gateway_executor(store, tmp_path / "home", transport))
    assert [i["outcome"] for i in done["items"]] == ["accepted", "held", "not_run"]
    assert done["stopped"]["reason"] == "unknown_outcome"
    again_transport = Scripted([ok()])
    again = run(store, CLASSIFY_FILE, batch,
                gateway_executor(store, tmp_path / "home", again_transport))  # fmt: skip
    assert [i["outcome"] for i in again["items"]] == ["accepted", "held", "accepted"]
    assert again_transport.sent == 1  # the held subject was not sent again, the cached one neither


def test_spend_never_passes_the_run_cap_in_a_batch(store: ProjectStore, tmp_path: Path) -> None:
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("1"))
    transport = Scripted([ok(cost=None) for _ in range(6)])
    cap = Decimal("0.0007")
    done = run(
        store, CLASSIFY_FILE, subjects(6), gateway_executor(store, home, transport, str(cap))
    )
    assert done["stopped"]["reason"] == "budget_exceeded" and done["stopped"]["cap"] == "run"
    assert Decimal(spend.summary(home)["used"]) <= cap
    assert 0 < done["counts"]["accepted"] == transport.sent < 6


def test_a_reply_from_outside_the_list_is_a_hint_only(store: ProjectStore, tmp_path: Path) -> None:
    spend.set_monthly_cap(tmp_path / "home", Decimal("1"))
    transport = Scripted([ok(model="mistralai/mistral-large-3")])
    reply = run(store, CLASSIFY_FILE, subjects(1),
                gateway_executor(store, tmp_path / "home", transport))  # fmt: skip
    [row] = results.listing(store)
    assert row["hint_only"] is True and reply["items"][0]["hint_only"] is True


def test_0103_a_quote_too_short_to_verify_counts_as_none() -> None:
    bundle = classify_bundle()
    reply = json.dumps({"result": {"role": "data_raw", "quote": "id,value"}, "abstain": None})
    verdict = check(CLASSIFY_FILE, bundle, reply, current_digest=bundle.subject_sha256)
    assert verdict.outcome == "accepted" and verdict.output is not None
    assert verdict.output["result"] == {"role": "data_raw", "quote": None}


def test_0103_two_samples_quoting_different_spans_of_one_line_agree() -> None:
    from prometheos.adoption import assist

    one = {"statements": [{"quote": "- 2026-03-10: results/v5.mat is the current result.",
                           "line": 7, "referent": "results/v5.mat", "status": "current",
                           "date": "2026-03-10"}]}  # fmt: skip
    two = {
        "statements": [{**one["statements"][0], "quote": "results/v5.mat is the current result."}]
    }
    assert assist.same([one, two])
    other = {"statements": [{**one["statements"][0], "status": "final_claimed"}]}
    assert not assist.same([one, other])  # a different claim still disagrees
