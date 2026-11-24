"""C06-AI: one provider adapter behind a capability interface and a per-project routing policy.
Egress preview, owner authority, secrets, budgets reserved before a call, strict structured
output with at most one repair, unknown outcomes never repeated, deterministic fallback (S15).
Scripted transport only: no network and no real credential."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, create_entry
from provider_support import (
    ENDPOINT,
    LOCAL,
    REF,
    SECRET,
    ProviderEnv,
    completion,
    config_doc,
)

from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.providers import adapter
from prometheos.providers import config as cfg
from prometheos.providers import transport as tp


@pytest.fixture
def env(tmp_path: Path) -> ProviderEnv:
    return ProviderEnv(tmp_path)


def all_store_bytes(env: ProviderEnv) -> bytes:
    return b"".join(p.read_bytes() for p in env.store_dir.rglob("*") if p.is_file())


# ----- capabilities and the default policy ------------


def test_default_policy_is_no_ai_and_nothing_is_ever_sent(env: ProviderEnv) -> None:
    status = env.svc.provider_status(env.project_id, AGENT)
    assert status["policy"] == "no-ai" and status["route"] == "none" and status["revision"] == 0
    assert status["silent_remote_fallback"] is False
    table = {row["capability"]: row for row in status["capabilities"]}
    assert table["generation"]["available"] is False
    assert table["transcription"] == {
        "capability": "transcription", "provider": "local-whisper", "route": "local",
        "available": True, "note": table["transcription"]["note"],
    }  # fmt: skip
    assert (
        table["embedding"]["available"] is False and table["classification"]["available"] is False
    )
    shown = env.preview()
    assert shown["would_send"] is False and "no-ai" in shown["blockers"][0]
    with pytest.raises(PrometheosError) as err:
        env.run(shown=shown)
    assert err.value.code == "policy_denied"
    assert env.transport.requests == []
    assert env.svc.provider_calls(env.project_id, AGENT)["calls"] == []


def test_the_adapter_declares_what_it_supports_and_refuses_the_rest() -> None:
    config = cfg.parse(config_doc())
    provider = adapter.ChatCompletionsProvider(config, cfg_secrets(), tp.UrllibTransport())
    assert provider.capabilities == {"generation"} and provider.supports("generation")
    for call, capability in (
        (provider.embed, "embedding"),
        (provider.classify, "classification"),
        (provider.transcribe, "transcription"),
    ):
        with pytest.raises(PrometheosError) as err:
            call()
        assert err.value.code == "unsupported" and err.value.details["capability"] == capability


def cfg_secrets() -> Any:
    from prometheos.providers.secrets import MemorySecretStore

    return MemorySecretStore()


# ----- configuration is an owner decision ------------


def test_configuration_needs_the_owners_exact_capability(env: ProviderEnv) -> None:
    doc = config_doc()
    shown = env.svc.provider_configure_preview(env.project_id, doc, 0)
    assert shown["changes"] and shown["route"] == "remote" and shown["stale"] is False
    with pytest.raises(PrometheosError) as none:
        env.svc.provider_configure(env.project_id, doc, 0, None)
    assert none.value.code == "policy_denied"
    other = env.svc.grant(
        env.project_id, operation="provider_configure", digest="0" * 64, label="SYNTHETIC-TEST"
    )
    with pytest.raises(PrometheosError):
        env.svc.provider_configure(env.project_id, doc, 0, other.secret)
    assert env.revision() == 0  # neither attempt changed anything
    result = env.configure(doc, store_secret=False)
    assert result["revision"] == 1 and result["route"] == "remote"
    # A grant for the first configuration cannot authorise a different one.
    changed = config_doc(model="another-model")
    grant = env.svc.grant(
        env.project_id, operation="provider_configure", digest=shown["digest"], label="S"
    )
    with pytest.raises(PrometheosError):
        env.svc.provider_configure(env.project_id, changed, 1, grant.secret)
    assert env.svc.provider_configure_preview(env.project_id, doc, 0)["stale"] is True
    old_grant = env.svc.grant(
        env.project_id, operation="provider_configure", digest=shown["digest"], label="S"
    )
    with pytest.raises(PrometheosError) as stale:  # a stale base revision is refused
        env.svc.provider_configure(env.project_id, doc, 0, old_grant.secret)
    assert stale.value.code == "revision_conflict"


def test_an_agent_alone_cannot_configure_or_store_a_secret(env: ProviderEnv) -> None:
    with pytest.raises(PrometheosError) as err:
        env.svc.provider_secret_set(env.project_id, REF, SECRET, None)
    assert err.value.code == "policy_denied"
    assert env.secrets.get(REF) is None
    with pytest.raises(PrometheosError):
        env.svc.provider_secret_delete(env.project_id, REF, None)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"policy": "local-only"}, "loopback"),  # local-only with a remote endpoint
        ({"endpoint": "http://api.example.test/v1"}, "https"),
        ({"endpoint": "https://user:pw@api.example.test/v1"}, "credentials"),
        ({"endpoint": "https://api.example.test/v1?key=abc"}, "query"),
        ({"endpoint": "ftp://api.example.test"}, "http"),
        ({"model": None}, "endpoint and model"),
        ({"credential_ref": None}, "credential reference"),
        ({"price_input_per_mtok_usd": None}, "prices"),
        ({"credential_ref": "Has Spaces"}, "credential_ref"),
        ({"budget": {"max_calls_per_run": 3}}, "max_calls_per_run"),
    ],
)
def test_invalid_configurations_are_refused(overrides: dict, message: str) -> None:
    with pytest.raises(PrometheosError) as err:
        cfg.parse(config_doc(**overrides))
    assert message in json.dumps(err.value.details) or message in err.value.message


def test_routes_are_decided_by_the_endpoint_only() -> None:
    for endpoint, route in (
        (LOCAL, "local"), ("http://localhost:8080/v1", "local"), ("http://[::1]:9/v1", "local"),
        (ENDPOINT, "remote"), ("https://localhost.evil.example/v1", "remote"),
        ("https://127.0.0.1.evil.example/v1", "remote"), (None, "none"),
    ):  # fmt: skip
        assert cfg.route_of(endpoint) == route, endpoint


# ----- secrets never leave the credential store ------------


def test_the_credential_value_appears_nowhere_but_the_authorization_header(
    env: ProviderEnv, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env.configure()
    env.script(completion(env.items_reply(2)))
    outputs: list[Any] = []
    outputs.append(env.svc.provider_status(env.project_id, AGENT))
    shown = env.preview()
    outputs.append(shown)
    outputs.append(env.run(shown=shown))
    outputs.append(env.svc.provider_calls(env.project_id, AGENT))
    secret_hint = env.svc.provider_secret_set  # noqa: F841 - never returns the value
    for output in outputs:
        assert SECRET not in json.dumps(output, default=str)
    status = outputs[0]
    assert status["credential"] == {
        "reference": REF, "present": True, "store": "memory",
    }  # fmt: skip
    assert status["config"]["credential_ref"] == REF
    url, headers, body = env.transport.requests[0]
    assert headers["Authorization"] == f"Bearer {SECRET}"  # only the adapter holds it
    assert SECRET not in json.dumps(body)
    assert SECRET.encode() not in all_store_bytes(env)
    assert SECRET not in caplog.text
    assert url == f"{ENDPOINT}/chat/completions"


def test_secret_set_returns_health_not_the_value_and_delete_works(env: ProviderEnv) -> None:
    digest = env.svc.provider_secret_digest("set", REF)
    grant = env.svc.grant(env.project_id, operation="provider_configure", digest=digest, label="S")
    reply = env.svc.provider_secret_set(env.project_id, REF, SECRET, grant.secret)
    assert reply == {"credential": {"reference": REF, "present": True, "store": "memory"}}
    assert SECRET not in json.dumps(reply)
    with pytest.raises(PrometheosError):  # single use
        env.svc.provider_secret_set(env.project_id, REF, SECRET, grant.secret)
    delete = env.svc.grant(
        env.project_id, operation="provider_configure",
        digest=env.svc.provider_secret_digest("delete", REF), label="S",
    )  # fmt: skip
    assert env.svc.provider_secret_delete(env.project_id, REF, delete.secret)["removed"] is True
    assert env.secrets.get(REF) is None
    with pytest.raises(PrometheosError):
        env.svc.provider_secret_set(env.project_id, "BAD REF", "x", None)
    with pytest.raises(PrometheosError):
        env.svc.provider_secret_set(env.project_id, REF, "two\nlines", None)


def test_a_missing_credential_blocks_before_any_request(env: ProviderEnv) -> None:
    env.configure(store_secret=False)
    shown = env.preview()
    assert shown["credential"]["present"] is False
    assert any("no credential" in b for b in shown["blockers"])
    with pytest.raises(PrometheosError):
        env.run(shown=shown)
    assert env.transport.requests == []


# ----- egress preview ------------


def test_preview_is_pure_and_shows_what_would_leave(env: ProviderEnv) -> None:
    env.configure()
    seq = env.seq()
    shown = env.preview()
    assert env.seq() == seq and env.transport.requests == []
    assert env.svc.provider_calls(env.project_id, AGENT)["calls"] == []
    assert shown["would_send"] is True and shown["blockers"] == []
    assert shown["route"] == "remote" and shown["endpoint_host"] == "api.example.test"
    assert shown["model"] == "test-model" and shown["policy"] == "remote-allowed"
    assert shown["adapter"] == "chat-completions"
    assert "Perhaps compare the controllers" in shown["transmits"]["excerpt"]
    assert (
        shown["transmits"]["characters"] > 100 and len(shown["transmits"]["payload_sha256"]) == 64
    )
    assert shown["cost"]["estimate_upper_bound_usd"] is not None
    assert "estimate" in shown["cost"]["label"]
    assert shown["calls"]["max_per_run"] == 2
    assert shown["inputs"]["restricted_included"] is False
    assert env.preview()["digest"] == shown["digest"]  # same inputs, same digest


def test_preview_blocks_an_oversized_payload_and_a_changed_configuration_changes_the_digest(
    env: ProviderEnv,
) -> None:
    env.configure(config_doc(budget={"max_input_chars": 1000}))
    shown = env.preview()
    assert any("exceeds the input budget" in b for b in shown["blockers"])
    before = shown["digest"]
    env.configure(config_doc(budget={"max_input_chars": 60000}))
    assert env.preview()["digest"] != before


def test_restricted_preview_needs_owner_scope(env: ProviderEnv) -> None:
    env.configure()
    with pytest.raises(PrometheosError) as err:
        env.preview(include_restricted=True)
    assert err.value.code == "policy_denied"


# ----- a successful run ------------


def test_run_creates_proposals_through_the_task_gate_and_records_the_call(env: ProviderEnv) -> None:
    env.configure()
    env.script(completion(env.items_reply(2, 3)))
    result = env.run()
    assert result["ok"] and result["status"] == "completed" and result["attempts"] == 1
    assert result["repaired"] is False and result["route"] == "remote"
    assert result["usage"] == {"prompt_tokens": 1000, "completion_tokens": 200}
    assert result["cost"]["usd"] == "0.006000" and "estimate" in result["cost"]["label"]
    assert result["receipt"]["adapter"] == "chat-completions"
    assert result["receipt"]["endpoint_host"] == "api.example.test"
    assert len(result["receipt"]["request_sha256"]) == 64
    reconciled = env.svc.reconcile(env.project_id, OWNER, env.meeting["meeting_id"])
    assert len(reconciled["items"]) == 2
    assert all(row["status"] == "proposed" for row in reconciled["items"])  # never adopted
    task = env.svc.task_get(env.project_id, result["task_id"])
    assert task["task"]["body"]["status"] == "submitted"
    ledger = env.svc.provider_calls(env.project_id, OWNER)
    row = ledger["calls"][0]
    assert row["state"] == "succeeded" and row["route"] == "remote" and row["attempt"] == 1
    assert row["actual_cost_usd"] == "0.006000" and row["task_id"] == result["task_id"]
    assert row["output_digest"] and row["input_digest"]
    assert ledger["usage"]["calls"] == 1 and ledger["usage"]["spent_estimate_usd"] == "0.006000"
    # The ledger holds digests and numbers, never the prompt or the reply.
    assert "Perhaps compare" not in json.dumps(
        ledger
    ) and "Compare the controllers" not in json.dumps(ledger)
    request = env.transport.requests[0][2]
    assert request["model"] == "test-model" and request["temperature"] == 0
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["max_tokens"] == 1024
    assert (
        request["messages"][0]["role"] == "system"
        and "research data" in request["messages"][1]["content"]
    )


def test_items_cite_real_lines_and_are_attributed_to_the_provider(env: ProviderEnv) -> None:
    env.configure()
    env.script(completion(env.items_reply(2)))
    result = env.run()
    item_id = result["commit"]["results"][0]["id"]
    actors = {r[0] for r in env.query("SELECT actor FROM audit WHERE record_id = ?", (item_id,))}
    assert actors == {"provider:test-model"}


# ----- authority ------------


def test_run_needs_the_owners_capability_for_the_exact_preview(env: ProviderEnv) -> None:
    env.configure()
    shown = env.preview()
    kwargs: dict[str, Any] = {
        "purpose": "meeting_extract", "meeting_id": env.meeting["meeting_id"],
        "context_budget": 12000, "include_restricted": False,
        "approved_digest": shown["digest"],
    }  # fmt: skip
    with pytest.raises(PrometheosError) as none:
        env.svc.provider_run(env.project_id, capability=None, **kwargs)
    assert none.value.code == "policy_denied"
    wrong = env.svc.grant(
        env.project_id, operation="provider_run", digest="0" * 64, label="SYNTHETIC-TEST"
    )
    with pytest.raises(PrometheosError):
        env.svc.provider_run(env.project_id, capability=wrong.secret, **kwargs)
    config_grant = env.svc.grant(
        env.project_id, operation="provider_configure", digest=shown["digest"], label="S"
    )
    with pytest.raises(PrometheosError):  # a configuration grant is not a run grant
        env.svc.provider_run(env.project_id, capability=config_grant.secret, **kwargs)
    assert env.transport.requests == []
    assert env.svc.provider_calls(env.project_id, AGENT)["calls"] == []  # nothing reserved
    good = env.svc.grant(
        env.project_id, operation="provider_run", digest=shown["digest"], label="SYNTHETIC-TEST"
    )
    env.script(completion(env.items_reply(2)))
    assert env.svc.provider_run(env.project_id, capability=good.secret, **kwargs)["ok"]
    env.script(completion(env.items_reply(3)))
    with pytest.raises(PrometheosError):  # single use
        env.svc.provider_run(env.project_id, capability=good.secret, **kwargs)


def test_a_preview_that_went_stale_is_refused(env: ProviderEnv) -> None:
    env.configure()
    shown = env.preview()
    env.apply([create_entry(title="New fact", text="Something recorded after the preview.")])
    grant = env.svc.grant(
        env.project_id, operation="provider_run", digest=shown["digest"], label="SYNTHETIC-TEST"
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.provider_run(
            env.project_id, purpose="meeting_extract", meeting_id=env.meeting["meeting_id"],
            context_budget=12000, include_restricted=False, approved_digest=shown["digest"],
            capability=grant.secret,
        )  # fmt: skip
    assert err.value.code == "revision_conflict" and env.transport.requests == []


# ----- strict structured output and one repair ------------


def test_malformed_then_valid_uses_exactly_one_repair(env: ProviderEnv) -> None:
    env.configure()
    env.script(completion("this is not JSON {"), completion(env.items_reply(2)))
    result = env.run()
    assert result["ok"] and result["attempts"] == 2 and result["repaired"] is True
    first, second = env.transport.requests[0][2], env.transport.requests[1][2]
    assert len(first["messages"]) == 2 and len(second["messages"]) == 4
    assert second["messages"][2] == {"role": "assistant", "content": "this is not JSON {"}
    assert "rejected" in second["messages"][3]["content"]
    calls = env.svc.provider_calls(env.project_id, AGENT)["calls"]
    assert [c["attempt"] for c in reversed(calls)] == [1, 2]
    assert len(env.svc.reconcile(env.project_id, OWNER, env.meeting["meeting_id"])["items"]) == 1


def test_invalid_twice_stops_with_nothing_recorded_and_the_task_still_usable(
    env: ProviderEnv,
) -> None:
    env.configure()
    bad = env.items_reply(2)
    bad["items"][0]["evidence"][0]["locator"]["end_line"] = 99  # a line that does not exist
    bad["items"][0]["evidence"][0]["locator"]["start_line"] = 99
    env.script(completion(bad), completion(bad))
    with pytest.raises(PrometheosError) as err:
        env.run()
    assert err.value.code == "validation_error"
    assert err.value.details["status"] == "invalid_output" and err.value.details["attempts"] == 2
    assert len(env.transport.requests) == 2  # one repair, never a third call
    assert env.svc.reconcile(env.project_id, OWNER, env.meeting["meeting_id"])["items"] == []
    task_id = err.value.details["task_id"]
    task = env.svc.task_get(env.project_id, task_id)
    assert task["task"]["body"]["status"] == "prepared"  # an external agent can take it over
    good = env.items_reply(3)
    done = env.svc.task_submit(env.project_id, task_id, json.dumps(good).encode(), new_request_id())
    assert done["ok"]


def test_a_reply_cut_off_at_the_output_limit_is_not_accepted(env: ProviderEnv) -> None:
    env.configure()
    env.script(completion(env.items_reply(2), finish="length"), completion(env.items_reply(3)))
    result = env.run()
    assert result["repaired"] is True  # the truncated reply was rejected, one repair fixed it
    assert len(env.svc.reconcile(env.project_id, OWNER, env.meeting["meeting_id"])["items"]) == 1


def plant_quantity(env: ProviderEnv, decimal: str) -> dict[str, Any]:
    def plant_versions() -> list[dict[str, Any]]:
        listed = env.svc.list_records(env.project_id, OWNER, kind="version", limit=50)["items"]
        return [v for v in listed if v["body"]["mime"].startswith("text/markdown")]

    if not plant_versions():
        env.import_files(["plant-result.md"])
    ver = plant_versions()[0]
    lines = (env.research / "plant-result.md").read_text(encoding="utf-8").split("\n")
    number = next(i for i, text in enumerate(lines, 1) if "candidate: 1.80 kJ" in text)
    start = lines[number - 1].index("1.80 kJ")
    ref = {
        "source_id": ver["body"]["source_id"], "version_id": ver["id"],
        "locator": {"type": "text", "start_line": number, "end_line": number},
        "quote_sha256": None,
    }  # fmt: skip
    return {
        "purpose": "notebook_draft",
        "entry": {
            "title": "Candidate recovered energy", "text": "The candidate recovered energy.",
            "category": "finding", "evidence": [ref], "claim_refs": [], "author_ref": None,
            "origin": "agent", "review": "unreviewed",
            "quantities": [{
                "decimal": decimal, "unit": "kJ", "source_ref": ref,
                "source_pointer": f"text:{number}:{start}:{start + 7}",
            }],
        },
    }  # fmt: skip


def test_an_altered_quantity_is_rejected_and_repair_may_fix_it(env: ProviderEnv) -> None:
    env.configure()
    altered, honest = plant_quantity(env, "1.81"), plant_quantity(env, "1.80")
    env.script(completion(altered), completion(honest))
    result = env.run(purpose="notebook_draft", meeting_id=None)
    assert result["ok"] and result["repaired"] is True
    notebook = env.svc.notebook(env.project_id, OWNER)
    assert any(
        e.get("quantities") and e["quantities"][0]["decimal"] == "1.80"
        for e in json.loads(json.dumps(notebook))["entries"]
    )
    repair = env.transport.requests[1][2]["messages"][-1]["content"]
    assert (
        "rejected" in repair and "1.81" not in repair
    )  # the rejection names the rule, not the text


def test_an_altered_quantity_twice_records_nothing(env: ProviderEnv) -> None:
    env.configure()
    altered = plant_quantity(env, "1.81")
    entries_before = len(json.loads(json.dumps(env.svc.notebook(env.project_id, OWNER)))["entries"])
    env.script(completion(altered), completion(altered))
    with pytest.raises(PrometheosError) as err:
        env.run(purpose="notebook_draft", meeting_id=None)
    assert err.value.details["status"] == "invalid_output" and len(env.transport.requests) == 2
    after = len(json.loads(json.dumps(env.svc.notebook(env.project_id, OWNER)))["entries"])
    assert after == entries_before  # no invented validated result


# ----- budgets, unknown outcomes, outages ------------


def test_the_spend_cap_refuses_before_anything_is_sent(env: ProviderEnv) -> None:
    env.configure(config_doc(budget={"spend_cap_usd": "0.000001"}))
    shown = env.preview()
    assert any("past the cap" in b for b in shown["blockers"])
    with pytest.raises(PrometheosError) as err:
        env.run(shown=shown)
    assert err.value.code in ("validation_error", "budget_exceeded")
    assert env.transport.requests == []


def test_the_cap_counts_what_earlier_calls_cost(env: ProviderEnv) -> None:
    # Each call reserves about 0.016 USD (max output 1024 tokens at $15/M plus input).
    env.configure(config_doc(budget={"spend_cap_usd": "0.025"}))
    env.script(completion(env.items_reply(2), usage=(1000, 200)))
    env.run()
    usage = env.svc.provider_calls(env.project_id, AGENT)["usage"]
    assert usage["spent_estimate_usd"] == "0.006000" and usage["remaining_usd"] == "0.019000"
    env.script(completion(env.items_reply(3)))
    shown = env.preview()
    assert shown["would_send"] is True  # 0.006 + ~0.016 still fits under 0.025
    env.run(shown=shown)
    third = env.preview()
    assert third["would_send"] is False and any("past the cap" in b for b in third["blockers"])
    with pytest.raises(PrometheosError):
        env.run(shown=third)
    assert env.svc.provider_calls(env.project_id, OWNER)["usage"]["calls"] == 2  # never sent


def test_a_timeout_after_sending_is_unknown_never_repeated_and_stays_reserved(
    env: ProviderEnv,
) -> None:
    env.configure()
    env.script(tp.OutcomeUnknown("TimeoutError"))
    with pytest.raises(PrometheosError) as err:
        env.run()
    assert err.value.code == "source_unavailable"
    assert err.value.details["status"] == "unknown_outcome" and len(err.value.details["calls"]) == 1
    assert len(env.transport.requests) == 1  # no automatic retry, no repair
    ledger = env.svc.provider_calls(env.project_id, OWNER)
    row = ledger["calls"][0]
    assert row["state"] == "unknown" and row["reserved_cost_usd"] and row["actual_cost_usd"] is None
    assert ledger["usage"]["unknown_outcomes"] == 1
    assert float(ledger["usage"]["spent_estimate_usd"]) > 0  # still counted against the cap
    # The same request is now blocked until the owner says they know about it.
    shown = env.preview()
    assert shown["would_send"] is False and row["call_id"] in shown["unacknowledged_unknown_calls"]
    with pytest.raises(PrometheosError):
        env.run(shown=shown)
    assert len(env.transport.requests) == 1
    # Acknowledging the unknown call makes the retry an explicit, recorded choice.
    env.script(completion(env.items_reply(2)))
    retried = env.run(shown=shown, acknowledge_unknown=row["call_id"])
    assert retried["ok"]
    calls = env.svc.provider_calls(env.project_id, AGENT)["calls"]
    assert any(c["acknowledges"] == row["call_id"] for c in calls)
    with pytest.raises(PrometheosError):  # one acknowledgement, once
        env.run(shown=shown, acknowledge_unknown=row["call_id"])


def test_nothing_sent_releases_the_reservation_and_may_be_retried(env: ProviderEnv) -> None:
    env.configure()
    env.script(tp.NotSent("ConnectionRefusedError"))
    with pytest.raises(PrometheosError) as err:
        env.run()
    assert err.value.details["status"] == "provider_unavailable"
    ledger = env.svc.provider_calls(env.project_id, AGENT)
    assert (
        ledger["calls"][0]["state"] == "failed"
        and ledger["calls"][0]["actual_cost_usd"] == "0.000000"
    )
    assert ledger["usage"]["spent_estimate_usd"] == "0.000000"
    env.script(completion(env.items_reply(2)))
    assert env.run()["ok"]


def test_http_rejections_are_free_and_server_errors_stay_reserved(env: ProviderEnv) -> None:
    env.configure()
    env.script(tp.Reply(401, b'{"error":"bad key"}'))
    with pytest.raises(PrometheosError) as err:
        env.run()
    assert err.value.details["status"] == "provider_rejected"
    assert (
        env.svc.provider_calls(env.project_id, AGENT)["calls"][0]["actual_cost_usd"] == "0.000000"
    )
    env.script(tp.Reply(503, b"overloaded"))
    with pytest.raises(PrometheosError) as busy:
        env.run()
    assert busy.value.details["status"] == "provider_error"
    newest = env.svc.provider_calls(env.project_id, AGENT)["calls"][0]
    assert newest["state"] == "failed" and newest["actual_cost_usd"] is None
    assert newest["reserved_cost_usd"]
    env.script(tp.Reply(200, b"<html>not a completion</html>"))
    with pytest.raises(PrometheosError) as junk:
        env.run()
    assert junk.value.details["status"] == "provider_error"


def test_an_outage_never_blocks_the_model_free_path(env: ProviderEnv) -> None:
    env.configure()
    env.script(tp.NotSent("gaierror"))
    with pytest.raises(PrometheosError) as err:
        env.run()
    fallback = err.value.details["fallback"]
    assert fallback["model_free"] is True and fallback["task_id"] == err.value.details["task_id"]
    # Everything else keeps working: capture, notebook, search, context, tasks.
    env.apply([create_entry(title="After the outage", text="Typed while the provider was down.")])
    assert env.svc.search(env.project_id, AGENT, "outage")["hits"]
    assert "After the outage" in env.svc.context(env.project_id, AGENT, purpose="orient")["text"]
    assert env.svc.next_steps(env.project_id, AGENT)["candidates"] is not None
    other = env.svc.task_prepare(
        env.project_id, purpose="meeting_extract", meeting_id=env.meeting["meeting_id"],
        request_id=new_request_id(),
    )  # fmt: skip
    done = env.svc.task_submit(
        env.project_id,
        other["task_id"],
        json.dumps(env.items_reply(3, text="Agent proposal")).encode(),
        new_request_id(),
    )
    assert done["ok"]  # the external-agent path is unaffected


def test_no_provider_code_is_involved_in_model_free_operations(env: ProviderEnv) -> None:
    """With the policy at no-ai and an exploding transport, the whole non-AI product works."""

    class Exploding:
        def post(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("a model-free operation reached the provider")

    env.svc.provider_transport = Exploding()
    env.apply([create_entry(title="Typed", text="Plain note.")])
    env.svc.search(env.project_id, AGENT, "Plain")
    env.svc.context(env.project_id, AGENT, purpose="orient")
    env.svc.notebook(env.project_id, AGENT)
    env.svc.reconcile(env.project_id, AGENT, env.meeting["meeting_id"])
    env.svc.task_prepare(env.project_id, purpose="notebook_draft", request_id=new_request_id())


# ----- routing policy: no silent remote fallback ------------


def test_local_only_uses_loopback_and_never_falls_back_to_remote(env: ProviderEnv) -> None:
    env.configure(
        config_doc(policy="local-only", endpoint=LOCAL, credential_ref=None,
                   price_input_per_mtok_usd=None, price_output_per_mtok_usd=None,
                   budget={"spend_cap_usd": None}), store_secret=False,
    )  # fmt: skip
    status = env.svc.provider_status(env.project_id, AGENT)
    assert status["route"] == "local" and status["endpoint_host"] == "127.0.0.1"
    env.script(tp.NotSent("ConnectionRefusedError"), completion(env.items_reply(2)))
    with pytest.raises(PrometheosError) as err:
        env.run()
    assert (
        err.value.details["route"] == "local"
        and err.value.details["status"] == "provider_unavailable"
    )
    urls = {url for url, _h, _b in env.transport.requests}
    assert urls == {f"{LOCAL}/chat/completions"}  # exactly one try, only on the loopback route
    assert len(env.transport.requests) == 1
    assert "Authorization" not in env.transport.requests[0][1]  # no credential for a local server


def test_changing_the_route_is_a_visible_owner_decision(env: ProviderEnv) -> None:
    env.configure(config_doc(policy="local-only", endpoint=LOCAL, credential_ref=None,
                             price_input_per_mtok_usd=None, price_output_per_mtok_usd=None,
                             budget={"spend_cap_usd": None}), store_secret=False)  # fmt: skip
    assert env.svc.provider_status(env.project_id, AGENT)["route"] == "local"
    moved = env.svc.provider_configure_preview(env.project_id, config_doc(), env.revision())
    assert (
        moved["route"] == "remote"
        and "endpoint" in moved["changes"]
        and "policy" in moved["changes"]
    )
    with pytest.raises(PrometheosError):
        env.svc.provider_configure(env.project_id, config_doc(), env.revision(), None)
    assert env.svc.provider_status(env.project_id, AGENT)["route"] == "local"


# ----- restricted inputs need a separate egress grant ------------


def test_restricted_content_is_excluded_by_default_and_needs_a_separate_grant(
    env: ProviderEnv,
) -> None:
    canary = "RESTRICTED-CANARY-5520"
    env.configure()
    env.apply(
        [create_entry(title="Sensitive note", text=f"Internal: {canary}", sensitivity="restricted")]
    )
    env.apply([create_entry(title="Public note", text="Ordinary content.")])
    reply = plant_quantity(env, "1.80")  # imports the file first: the preview is of what follows
    shown = env.preview(purpose="notebook_draft", meeting_id=None)
    assert canary not in json.dumps(shown) and shown["inputs"]["restricted_included"] is False
    # An agent must not learn that restricted records exist; the owner may see the count.
    assert shown["inputs"]["excluded_above_scope"] is None
    assert shown["needs_egress_grant"] is False
    assert (
        env.preview(OWNER, purpose="notebook_draft", meeting_id=None)["inputs"][
            "excluded_above_scope"
        ]
        >= 1
    )
    env.script(completion(reply))
    env.run(shown=shown, purpose="notebook_draft", meeting_id=None)
    assert canary not in json.dumps(env.transport.requests)  # never in what was sent

    # With the owner's explicit choice and BOTH grants, it is sent, and the results inherit it.
    owner_shown = env.preview(
        OWNER, purpose="notebook_draft", meeting_id=None, include_restricted=True
    )
    assert (
        owner_shown["needs_egress_grant"] is True and canary in owner_shown["transmits"]["excerpt"]
    )
    run_grant = env.svc.grant(
        env.project_id, operation="provider_run", digest=owner_shown["digest"], label="S"
    )
    kwargs: dict[str, Any] = {
        "purpose": "notebook_draft", "meeting_id": None, "context_budget": 12000,
        "include_restricted": True, "approved_digest": owner_shown["digest"],
        "capability": run_grant.secret,
    }  # fmt: skip
    env.script(completion(plant_quantity(env, "1.80")))
    with pytest.raises(PrometheosError) as no_egress:
        env.svc.provider_run(env.project_id, egress_capability=None, **kwargs)
    assert no_egress.value.code == "policy_denied" and env.transport.requests == []
    run_grant = env.svc.grant(
        env.project_id, operation="provider_run", digest=owner_shown["digest"], label="S"
    )
    kwargs["capability"] = run_grant.secret
    egress = env.svc.grant(
        env.project_id, operation="provider_egress",
        digest=owner_shown["transmits"]["payload_sha256"], label="S",
    )  # fmt: skip
    done = env.svc.provider_run(env.project_id, egress_capability=egress.secret, **kwargs)
    assert done["ok"] and canary in json.dumps(env.transport.requests)
    task = env.svc.task_get(env.project_id, done["task_id"], OWNER)
    assert task["task"]["sensitivity"] == "restricted"
    new_id = done["commit"]["results"][0]["id"]
    created = env.svc.get(env.project_id, OWNER, new_id, None)
    assert created["sensitivity"] == "restricted"  # derived output inherits its inputs' level
    visible = env.svc.list_records(env.project_id, AGENT, kind="entry", limit=100)["items"]
    assert new_id not in {e["id"] for e in visible}
    with pytest.raises(PrometheosError):
        env.svc.get(env.project_id, AGENT, new_id, None)


# ----- the real transport against a local HTTP server ------------


def test_urllib_transport_classifies_outcomes(tmp_path: Path) -> None:
    import http.server
    import socket
    import threading
    import time

    seen: list[dict[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            seen.append({k.lower(): v for k, v in self.headers.items()})
            if self.path == "/ok":
                body = b'{"hello": "world"}'
                self.send_response(200)
            elif self.path == "/redirect":
                body = b""
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            elif self.path == "/slow":
                time.sleep(2)
                body = b"late"
                self.send_response(200)
            else:
                body = b"nope"
                self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            try:
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # the client gave up (the timeout case)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = tp.UrllibTransport()
        base = f"http://127.0.0.1:{port}"
        ok = transport.post(f"{base}/ok", {"Authorization": "Bearer abc"}, b"{}", 5)
        assert ok.status == 200 and json.loads(ok.body) == {"hello": "world"}
        assert seen[-1]["authorization"] == "Bearer abc"
        assert transport.post(f"{base}/boom", {}, b"{}", 5).status == 500
        redirected = transport.post(f"{base}/redirect", {"Authorization": "Bearer abc"}, b"{}", 5)
        assert redirected.status == 302  # not followed, so the key cannot travel on
        with pytest.raises(tp.OutcomeUnknown):
            transport.post(f"{base}/slow", {}, b"{}", 0.3)
    finally:
        server.shutdown()
        server.server_close()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()
    with pytest.raises(tp.NotSent):
        tp.UrllibTransport().post(f"http://127.0.0.1:{dead_port}/x", {}, b"{}", 10)


def test_end_to_end_against_a_local_server_uses_the_real_adapter(tmp_path: Path) -> None:
    """The adapter, transport, ledger and task gate together, with a real loopback HTTP server
    standing in for a local model server (route: local, no credential)."""
    import http.server
    import threading

    env = ProviderEnv(tmp_path)
    body = json.dumps(completion(env.items_reply(2)).body.decode())  # noqa: F841
    reply = completion(env.items_reply(2)).body

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env.svc.provider_transport = None  # the real transport
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1"
        env.configure(
            config_doc(policy="local-only", endpoint=endpoint, credential_ref=None,
                       price_input_per_mtok_usd=None, price_output_per_mtok_usd=None,
                       budget={"spend_cap_usd": None}), store_secret=False,
        )  # fmt: skip
        result = env.run()
        assert result["ok"] and result["route"] == "local"
        assert (
            len(env.svc.reconcile(env.project_id, OWNER, env.meeting["meeting_id"])["items"]) == 1
        )
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_windows_credential_store_round_trip_never_exposes_the_value() -> None:
    """A real write, read and delete of one throw-away entry in the user's Credential Manager
    (a random test value under a unique reference; always removed)."""
    import uuid

    from prometheos.providers.secrets import LayeredSecretStore, WindowsCredentialStore, health

    reference = f"prometheos-test-{uuid.uuid4().hex[:12]}"
    value = f"test-value-{uuid.uuid4().hex}"
    store = WindowsCredentialStore()
    layered = LayeredSecretStore(store, cfg_secrets())
    try:
        assert store.get(reference) is None
        layered.set(reference, value)
        assert store.get(reference) == value
        report = health(layered, reference)
        assert report == {"reference": reference, "present": True, "store": store.name}
        assert value not in json.dumps(report)
        layered.set(reference, value + "-2")  # overwrite
        assert store.get(reference) == value + "-2"
    finally:
        store.delete(reference)
    assert store.get(reference) is None
    assert store.delete(reference) is False


def test_call_budget_is_an_exact_integer_one_or_two() -> None:
    for bad in (True, 1.0, 3, 0):
        with pytest.raises(PrometheosError):
            cfg.parse(config_doc(budget={"max_calls_per_run": bad}))
    assert cfg.parse(config_doc(budget={"max_calls_per_run": 1})).budget.max_calls_per_run == 1


def test_the_reservation_itself_refuses_past_the_cap_even_if_a_preview_was_stale(
    env: ProviderEnv,
) -> None:
    """The cap is enforced where the reservation is made (the preview is only advice): two
    previews that each fit may not both be sent."""
    from decimal import Decimal

    from prometheos.providers import ledger

    env.configure(config_doc(budget={"spend_cap_usd": "0.020"}))
    config = cfg.parse(config_doc(budget={"spend_cap_usd": "0.020"}))
    store = env.svc.open(env.project_id)

    def reserve(run: str, cost: str) -> str:
        return ledger.reserve(
            store, config, run_key=run, purpose="meeting_extract", route="remote",
            host="api.example.test", attempt=1, input_digest="d" * 64, request_identity="i",
            input_refs=[], task_id=None, max_rank=0, reserved_cost=Decimal(cost),
        )  # fmt: skip

    reserve("run-a", "0.012")
    with pytest.raises(PrometheosError) as err:
        reserve("run-b", "0.012")  # 0.024 would pass the 0.020 cap
    assert err.value.code == "budget_exceeded" and err.value.details["cap_usd"] == "0.020"
    rows = env.svc.provider_calls(env.project_id, AGENT)["calls"]
    assert [r["state"] for r in rows] == ["reserved"]  # the second never became a call
    with pytest.raises(PrometheosError):  # and the per-run call count is enforced here too
        ledger.reserve(
            store, config, run_key="run-a", purpose="meeting_extract", route="remote",
            host="h", attempt=1, input_digest="d" * 64, request_identity="i", input_refs=[],
            task_id=None, max_rank=0, reserved_cost=Decimal("0.001"),
        )  # fmt: skip
