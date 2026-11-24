"""CP22.1 (A07; decision 0092): the gateway, the home's spend ledger and the standing policy.

A scripted transport stands in for OpenRouter; nothing leaves this computer. Covered (C22-GATEWAY,
S68): the request defaults (zero retention, data collection denied, parameters required, strict
schema, no fallback outside the pair) and the per-model profile; fallback only inside the
evaluated pairs, one call per pair; 402/403 and 429 stop the batch with the reservation returned;
a timeout holds the reservation and is never repeated; a served model outside the list is marked;
a reply without a cost settles at the labelled estimate; the run and monthly caps hold across
projects and processes; the policy gate's answers.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from prometheos.domain.models import PrometheosError
from prometheos.intelligence import spend
from prometheos.intelligence.gateway import (
    BatchStop,
    Gateway,
    UnknownOutcome,
    estimate_cost,
    request_body,
)
from prometheos.intelligence.policy import (
    EvaluatedModel,
    OutsidePolicy,
    StandingPolicy,
    TaskGrant,
    check_run,
    policy_sha256,
)
from prometheos.providers import transport as tp
from prometheos.providers.config import Budget, ProviderConfig
from prometheos.providers.secrets import MemorySecretStore

WIRE = {"type": "object", "properties": {"role": {"type": "string"}}, "required": ["role"],
        "additionalProperties": False}  # fmt: skip


def pair(model: str, provider: str, company: str, params: list[str] | None = None,
         effort: str | None = None) -> EvaluatedModel:  # fmt: skip
    return EvaluatedModel(
        model=model, provider=provider, company=company,
        supported_parameters=params if params is not None else ["temperature", "seed",
                                                                 "response_format"],
        lowest_effort=effort,  # type: ignore[arg-type]
        price_input_per_mtok_usd="0.15", price_output_per_mtok_usd="0.60",
        metadata_as_of="2026-10-30",
    )  # fmt: skip


A = pair("mistralai/mistral-small-4", "mistral", "mistralai")
B = pair("google/gemini-3.1-flash-lite", "google-vertex", "google", ["response_format"])
GRANT = TaskGrant(task_kind="adoption.classify_file", models=[A, B], single_company_reason=None,
                  task_ceiling_usd="0.05")  # fmt: skip


def config() -> ProviderConfig:
    return ProviderConfig(
        policy="remote-allowed", endpoint="https://openrouter.ai/api/v1",
        model="mistralai/mistral-small-4", credential_ref="openrouter",
        price_input_per_mtok_usd="0.15", price_output_per_mtok_usd="0.60",
        budget=Budget(max_input_chars=60_000, max_output_tokens=2048, max_calls_per_run=2,
                      spend_cap_usd=None, timeout_seconds=60),
    )  # fmt: skip


def completion(model: str, provider: str | None, cost: float | None = 0.0001,
               finish: str = "stop", content: str = '{"role": "notes"}') -> tp.Reply:  # fmt: skip
    usage: dict[str, Any] = {"prompt_tokens": 120, "completion_tokens": 9}
    if cost is not None:
        usage["cost"] = cost
    body = {"id": "gen-1", "model": model, "provider": provider, "usage": usage,
            "choices": [{"message": {"content": content}, "finish_reason": finish}]}  # fmt: skip
    return tp.Reply(200, json.dumps(body).encode())


class Scripted:
    """Answers each POST from a list: a Reply, or an exception to raise."""

    def __init__(self, answers: list[Any], key: dict[str, Any] | None = None) -> None:
        self.answers, self.sent, self.key = list(answers), [], key

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> tp.Reply:
        self.sent.append({"url": url, "headers": headers, "body": json.loads(body)})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]

    def get(self, url: str, headers: dict[str, str], timeout: float) -> tp.Reply:
        if self.key is None:
            return tp.Reply(404, b"{}")
        return tp.Reply(200, json.dumps({"data": self.key}).encode())


def gateway(tmp_path: Path, transport: Scripted, project: str = "project_a") -> Gateway:
    secrets = MemorySecretStore()
    secrets.set("openrouter", "sk-or-test-not-a-real-key")
    return Gateway(config(), secrets, transport, tmp_path / "home", project)


def call(gw: Gateway, run: str = "run_1", cap: str = "0.10", grant: TaskGrant = GRANT) -> Any:
    return gw.call(run_id=run, task_kind=grant.task_kind, grant=grant, run_cap=Decimal(cap),
                   system="classify", user="x" * 400, wire=WIRE, max_tokens=64)  # fmt: skip


@pytest.fixture
def home(tmp_path: Path) -> Path:
    spend.set_monthly_cap(tmp_path / "home", Decimal("1.00"))
    return tmp_path / "home"


# ----- the request ---------------------------------------------------------------------------


def test_every_request_asks_for_zero_retention_and_the_strict_schema(home: Path) -> None:
    transport = Scripted([completion(A.model, "Mistral")])
    call(gateway(home.parent, transport))
    [sent] = transport.sent
    body = sent["body"]
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert body["provider"] == {"only": ["mistral"], "allow_fallbacks": False,
                                "data_collection": "deny", "zdr": True,
                                "require_parameters": True}  # fmt: skip
    assert body["response_format"]["json_schema"] == {"name": "result", "strict": True,
                                                       "schema": WIRE}  # fmt: skip
    assert body["stream"] is False and body["usage"] == {"include": True}
    assert body["temperature"] == 0 and body["seed"] == 7
    assert sent["headers"]["Authorization"] == "Bearer sk-or-test-not-a-real-key"


def test_the_profile_sends_only_what_the_model_lists() -> None:
    body = request_body(B, "s", "u", WIRE, 64)
    assert "temperature" not in body and "seed" not in body and "reasoning" not in body
    thinking = pair("openai/gpt-6-luna", "openai", "openai", ["reasoning", "response_format"],
                    "none")  # fmt: skip
    assert request_body(thinking, "s", "u", WIRE, 64)["reasoning"] == {"effort": "none"}


def test_the_estimate_is_an_upper_bound_from_the_stored_prices() -> None:
    # every character a token (no tokenizer yields more): 400 at 0.15/M, 64 output at 0.60/M
    assert estimate_cost(A, 400, 64) == Decimal("0.000099")
    reasoning = A.model_copy(update={"lowest_effort": "low"})
    assert estimate_cost(reasoning, 400, 64) == Decimal("0.000137")  # the output budget twice


# ----- outcomes ---------------------------------------------------------------------------------


def test_a_reply_settles_its_billed_cost(home: Path) -> None:
    reply = call(gateway(home.parent, Scripted([completion(A.model, "Mistral", 0.000042)])))
    assert reply.in_list and reply.billed == Decimal("0.000042")
    assert reply.receipt["cost_basis"] == "billed" and reply.receipt["data_policy"]["zdr"] is True
    assert json.loads(reply.receipt["reply_body"])["choices"][0]["message"]["content"] == (
        '{"role": "notes"}'
    )
    assert spend.summary(home)["billed"] == "0.000042"


def test_a_reply_without_a_cost_keeps_the_labelled_estimate(home: Path) -> None:
    reply = call(gateway(home.parent, Scripted([completion(A.model, "Mistral", cost=None)])))
    assert reply.billed is None and reply.receipt["cost_basis"] == "estimate"
    assert spend.summary(home)["estimated"] == str(reply.estimate)


def test_a_served_model_outside_the_list_is_marked(home: Path) -> None:
    other = call(
        gateway(home.parent, Scripted([completion("mistralai/mistral-large-3", "Mistral")]))
    )
    assert other.in_list is False
    wrong_provider = call(gateway(home.parent, Scripted([completion(A.model, "SomeHost")])))
    assert wrong_provider.in_list is False


def test_fallback_stays_inside_the_evaluated_pairs(home: Path) -> None:
    transport = Scripted([tp.Reply(503, b"{}"), completion(B.model, "Google Vertex")])
    reply = call(gateway(home.parent, transport))
    assert [s["body"]["model"] for s in transport.sent] == [A.model, B.model]
    assert [s["body"]["provider"]["only"] for s in transport.sent] == [
        ["mistral"],
        ["google-vertex"],
    ]
    assert reply.pair == B and reply.in_list
    assert reply.receipt["tried_before"] == [f"{A.model}@mistral: HTTP 503"]
    nothing = Scripted([tp.NotSent("refused"), tp.Reply(404, b"{}")])
    with pytest.raises(BatchStop) as stop:
        call(gateway(home.parent, nothing), run="run_2")
    assert stop.value.reason == "no_route" and len(nothing.sent) == 2
    # The 503 and the 404 may have been processed upstream: each counts its estimate; the call
    # that was never sent counts nothing.
    one = estimate_cost(A, len("classify") + 400, 64)
    assert Decimal(spend.summary(home)["estimated"]) == 2 * one


@pytest.mark.parametrize(("status", "reason"), [(402, "key_limit"), (403, "key_limit"),
                                                (429, "rate_limited")])  # fmt: skip
def test_the_key_limit_and_rate_limiting_stop_the_batch(home: Path, status: int,
                                                        reason: str) -> None:  # fmt: skip
    transport = Scripted([tp.Reply(status, b"{}")])
    with pytest.raises(BatchStop) as stop:
        call(gateway(home.parent, transport))
    assert stop.value.reason == reason and len(transport.sent) == 1  # no fallback, no retry
    assert spend.summary(home)["used"] == "0"


def test_an_unknown_outcome_is_held_and_never_repeated(home: Path) -> None:
    transport = Scripted([tp.OutcomeUnknown("TimeoutError")])
    with pytest.raises(UnknownOutcome) as held:
        call(gateway(home.parent, transport))
    assert len(transport.sent) == 1 and held.value.pair == A
    summary = spend.summary(home)
    assert summary["held_calls"] == 1 and Decimal(summary["used"]) > 0


def test_a_truncated_reply_is_marked_and_still_paid(home: Path) -> None:
    reply = call(gateway(home.parent, Scripted([completion(A.model, "Mistral", finish="length")])))
    assert reply.truncated and Decimal(spend.summary(home)["used"]) > 0


def test_an_invalid_reply_stops_and_counts_the_estimate(home: Path) -> None:
    with pytest.raises(BatchStop) as stop:
        call(gateway(home.parent, Scripted([tp.Reply(200, b'{"no": "choices"}')])))
    assert stop.value.reason == "invalid_reply" and Decimal(spend.summary(home)["used"]) > 0


def test_no_key_stops_before_anything_is_sent(home: Path) -> None:
    transport = Scripted([])
    gw = Gateway(config(), MemorySecretStore(), transport, home, "project_a")
    with pytest.raises(BatchStop) as stop:
        call(gw)
    assert stop.value.reason == "no_credential" and transport.sent == []


def test_the_keys_remaining_limit_is_read(home: Path) -> None:
    gw = gateway(home.parent, Scripted([], key={"limit": 5, "limit_remaining": 4.2}))
    assert gw.key_limit() == {"limit": 5, "limit_remaining": 4.2}
    assert gateway(home.parent, Scripted([])).key_limit() is None


# ----- caps ---------------------------------------------------------------------------------------


def test_spend_never_passes_the_run_cap(home: Path) -> None:
    transport = Scripted([completion(A.model, "Mistral", cost=None) for _ in range(10)])
    gw = gateway(home.parent, transport)
    per_call = estimate_cost(A, len("classify") + 400, 64)
    cap = per_call * 3
    done = 0
    with pytest.raises(PrometheosError) as stop:
        for _ in range(10):
            call(gw, cap=str(cap))
            done += 1
    assert stop.value.code == "budget_exceeded" and stop.value.details["cap"] == "run"
    assert done == 3 and len(transport.sent) == 3
    assert Decimal(spend.summary(home)["used"]) <= cap


def test_the_monthly_cap_is_the_homes_across_projects(home: Path) -> None:
    spend.set_monthly_cap(home, estimate_cost(A, 408, 64) * 2)
    one = gateway(home.parent, Scripted([completion(A.model, "Mistral", cost=None)]), "project_a")
    two = gateway(home.parent, Scripted([completion(A.model, "Mistral", cost=None)] * 2),
                  "project_b")  # fmt: skip
    call(one, run="r1")
    call(two, run="r2")
    with pytest.raises(PrometheosError) as stop:
        call(two, run="r3")
    assert stop.value.details["cap"] == "monthly"


def test_the_task_ceiling_and_an_unset_monthly_cap_refuse(tmp_path: Path) -> None:
    gw = gateway(tmp_path, Scripted([]))
    with pytest.raises(PrometheosError) as unset:
        call(gw)
    assert unset.value.details["cap"] == "monthly"
    spend.set_monthly_cap(tmp_path / "home", Decimal("1"))
    tiny = GRANT.model_copy(update={"task_ceiling_usd": "0.000001"})
    with pytest.raises(PrometheosError) as ceiling:
        call(gw, grant=tiny)
    assert ceiling.value.details["cap"] == "task"


# ----- the policy ---------------------------------------------------------------------------------


def policy(**change: Any) -> StandingPolicy:
    base: dict[str, Any] = {"tasks": [GRANT.model_dump()], "sensitivity_ceiling": "normal",
                            "data_policy": "zdr_deny_collection", "run_cap_usd": "0.50",
                            "monthly_cap_usd": "3", "agents_may_run": False}  # fmt: skip
    return StandingPolicy.model_validate(base | change)


def test_the_policy_refuses_routers_one_company_without_reason_and_missing_caps() -> None:
    for model in ("openrouter/auto", "~anthropic/claude-haiku-latest",
                  "meta/llama-5:free", "mistralai/mistral-small-latest"):  # fmt: skip
        with pytest.raises(ValueError):
            pair(model, "x", "y")
    with pytest.raises(ValueError, match="one company"):
        TaskGrant(task_kind="k.k", models=[A], single_company_reason=None, task_ceiling_usd="1")
    for caps in ({"run_cap_usd": "0"}, {"monthly_cap_usd": "0"},
                 {"run_cap_usd": "5", "monthly_cap_usd": "3"}):  # fmt: skip
        with pytest.raises(ValueError):
            policy(**caps)


def test_the_gate_answers_by_sensitivity_policy_task_and_caller() -> None:
    granted = {"policy": policy().model_dump(mode="json"), "provider_revision": 4}
    ok = check_run(granted, config(), 4, task_kind=GRANT.task_kind, sensitivity="normal",
                   agent=False)  # fmt: skip
    assert ok.task_kind == GRANT.task_kind
    cases = [
        (granted, 4, GRANT.task_kind, "restricted", False, "policy_denied"),
        (granted, 4, GRANT.task_kind, "private", False, "policy_denied"),
        (None, 4, GRANT.task_kind, "normal", False, "approval_required"),
        (granted, 5, GRANT.task_kind, "normal", False, "approval_required"),
        (granted, 4, "knowledge.link_claim_question", "normal", False, "approval_required"),
        (granted, 4, GRANT.task_kind, "normal", True, "approval_required"),
    ]
    for row, revision, kind, sensitivity, agent, code in cases:
        with pytest.raises((PrometheosError, OutsidePolicy)) as refused:
            check_run(row, config(), revision, task_kind=kind, sensitivity=sensitivity,
                      agent=agent)  # fmt: skip
        answer = (
            refused.value.code
            if isinstance(refused.value, PrometheosError)
            else ("approval_required")
        )
        assert answer == code, (revision, kind, sensitivity, agent)
    agents = {"policy": policy(agents_may_run=True).model_dump(mode="json"),
              "provider_revision": 4}  # fmt: skip
    assert check_run(agents, config(), 4, task_kind=GRANT.task_kind, sensitivity="normal",
                     agent=True)  # fmt: skip
    local = config().model_copy(update={"endpoint": "https://api.example.org/v1"})
    with pytest.raises(OutsidePolicy):
        check_run(granted, local, 4, task_kind=GRANT.task_kind, sensitivity="normal",
                  agent=False)  # fmt: skip


def test_the_policy_digest_changes_with_any_field() -> None:
    assert policy_sha256(policy()) != policy_sha256(policy(agents_may_run=True))
    assert policy_sha256(policy()) == policy_sha256(policy())


# ----- decision 0103: found by the first live evaluation -----------------------------------------


def test_0103_a_reasoning_model_gets_the_doubled_budget_the_estimate_counts() -> None:
    thinking = pair("anthropic/claude-haiku-5.5", "google-vertex", "anthropic",
                    ["reasoning", "response_format"], "low")  # fmt: skip
    assert request_body(thinking, "s", "u", WIRE, 64)["max_tokens"] == 128
    assert request_body(A, "s", "u", WIRE, 64)["max_tokens"] == 64
    # the estimate already counted the doubled output, so it stays an upper bound
    assert estimate_cost(thinking, 10, 64) >= estimate_cost(A, 10, 128)


def test_0103_a_reply_cut_off_before_any_answer_is_truncated_not_a_stop(home: Path) -> None:
    body = {"id": "gen-1", "model": A.model, "provider": "Mistral",
            "usage": {"prompt_tokens": 9, "completion_tokens": 64, "cost": 0.00002},
            "choices": [{"message": {"content": None}, "finish_reason": "length"}]}  # fmt: skip
    gw = gateway(home.parent, Scripted([tp.Reply(200, json.dumps(body).encode())]))
    reply = call(gw)
    assert reply.truncated and reply.text == ""


def test_0103_google_in_a_reply_is_the_google_vertex_provider(home: Path) -> None:
    gw = gateway(home.parent, Scripted([completion(B.model, "Google")]))
    grant = TaskGrant(task_kind="adoption.classify_file", models=[B],
                      single_company_reason="one pair for the provider-name test",
                      task_ceiling_usd="0.05")  # fmt: skip
    assert call(gw, grant=grant).in_list is True
    other = gateway(home.parent, Scripted([completion(B.model, "Google AI Studio")]))
    assert call(other, run="run_2", grant=grant).in_list is False  # a different provider
