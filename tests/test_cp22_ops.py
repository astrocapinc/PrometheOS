"""CP22.4 (A07; decision 0094): the layer's operations on a real project.

Nothing leaves this computer: runs use recorded cassettes, or are dry runs.
- root sensitivity: restricted until the owner sets it; raising is open; lowering needs an owner
  act (an agent's request becomes an approval request); a root re-pointed at another folder is
  restricted again; restricted input is denied before any executor;
- the standing policy: preview needs the OpenRouter configuration; a grant needs an owner act
  (an agent's grant is an approval request; an agent's revocation answers ``policy_denied``);
  a grant sets the home's monthly cap; a changed provider configuration voids it;
- runs: without a policy a run says what is missing (``capability_unavailable``) and the
  cassette path still works; a dry run estimates and sends nothing; an agent runs only where the
  policy lets agents; a run on cassettes over an adoption scan stores tagged samples and changes
  no family, class or status.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cp1_support import Env

from prometheos import agentops
from prometheos.agentops import Ctx
from prometheos.app.authority import OwnerAct
from prometheos.app.reads import Caller
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError
from prometheos.intelligence import spend
from prometheos.providers import config as provider_config
from prometheos.store.project import ProjectStore

agentops.load()
OWNER = OwnerAct(channel="cli-interactive", actor="test-owner")
AGENT = Caller("test-agent")
PAIR = {"model": "mistralai/mistral-small-4", "provider": "mistral", "company": "mistralai",
        "supported_parameters": ["temperature"], "lowest_effort": None,
        "price_input_per_mtok_usd": "0.15", "price_output_per_mtok_usd": "0.60",
        "metadata_as_of": "2026-10-30"}  # fmt: skip


def policy(**change: Any) -> dict[str, Any]:
    base = {"tasks": [{"task_kind": "adoption.classify_file", "models": [PAIR],
                       "single_company_reason": "one pair until the spike measures more",
                       "task_ceiling_usd": "0.01"}],
            "sensitivity_ceiling": "normal", "data_policy": "zdr_deny_collection",
            "run_cap_usd": "0.10", "monthly_cap_usd": "1", "agents_may_run": False}  # fmt: skip
    return base | change


class NoNetwork:
    """Tests never reach a provider: every send or read is refused before leaving."""

    def post(self, *args: Any) -> Any:
        raise AssertionError("a test tried to send a request")

    def get(self, *args: Any) -> Any:
        raise AssertionError("a test tried to read from the provider")


def openrouter(env: Env) -> int:
    # Never the OS credential store (it may hold the owner's real key) and never the network.
    from prometheos.providers.secrets import MemorySecretStore

    env.svc.secret_store = MemorySecretStore()
    env.svc.provider_transport = NoNetwork()  # type: ignore[assignment]
    store = ProjectStore(env.store_dir)
    conn = store.connect()
    try:
        _, revision = provider_config.load(conn)
    finally:
        conn.close()
    config = provider_config.ProviderConfig.model_validate({
        "policy": "remote-allowed", "endpoint": "https://openrouter.ai/api/v1",
        "model": PAIR["model"], "credential_ref": "openrouter",
        "price_input_per_mtok_usd": "0.15", "price_output_per_mtok_usd": "0.60",
        "budget": {"max_input_chars": 60000, "max_output_tokens": 2048, "max_calls_per_run": 2,
                   "spend_cap_usd": None, "timeout_seconds": 60}})  # fmt: skip
    return provider_config.save(store, config, revision)


def agent_call(env: Env, op: str, **params: Any) -> Any:
    operation = agentops.OPS[op]
    ctx = Ctx(service=env.svc, project_id=env.project_id, caller=AGENT)
    return operation.run(ctx, operation.params.model_validate(params))


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def code_of(call: Any) -> str:
    with pytest.raises(PrometheosError) as refused:
        call()
    return refused.value.code


# ----- root sensitivity -----------------------------------------------------------------------


def test_a_root_is_restricted_until_the_owner_lowers_it(env: Env) -> None:
    def show() -> dict[str, Any]:
        roots = env.svc.ai_root_sensitivity_show(env.project_id)["roots"]
        return {r["root_alias"]: r for r in roots}

    assert (
        show()["research"]["sensitivity"] == "restricted" and not show()["research"]["set_by_owner"]
    )
    assert (
        env.svc.ai_root_sensitivity_set(env.project_id, "research", "private")["lowered"] is False
    )
    assert code_of(
        lambda: env.svc.ai_root_sensitivity_set(env.project_id, "research", "normal")
    ) == ("policy_denied")
    assert code_of(lambda: agent_call(env, "root_sensitivity_set", root_alias="research",
                                      sensitivity="normal")) == "approval_required"  # fmt: skip
    done = env.svc.ai_root_sensitivity_set(env.project_id, "research", "normal", authority=OWNER)
    assert done["lowered"] is True and show()["research"]["sensitivity"] == "normal"
    # The alias pointed at another folder: restricted again, until the owner sets it there.
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    conn.execute("UPDATE root_sensitivity SET folder = 'C:/elsewhere'")
    conn.commit()
    conn.close()
    assert show()["research"]["sensitivity"] == "restricted"
    assert "another folder" in show()["research"]["note"]


# ----- the standing policy ---------------------------------------------------------------------


def test_the_policy_is_granted_by_the_owner_only_and_voided_by_a_provider_change(env: Env) -> None:
    assert (
        code_of(lambda: env.svc.ai_policy_preview(env.project_id, policy())) == "validation_error"
    )
    openrouter(env)
    shown = env.svc.ai_policy_preview(env.project_id, policy())
    binding = shown["binding_sha256"]
    assert code_of(lambda: env.svc.ai_policy_grant(env.project_id, policy(), binding)) == (
        "policy_denied"
    )
    assert code_of(lambda: agent_call(env, "ai_policy_grant", policy=policy(),
                                      binding_sha256=binding)) == "approval_required"  # fmt: skip
    granted = env.svc.ai_policy_grant(env.project_id, policy(), binding, authority=OWNER)
    assert granted["in_force"] is True
    state = env.svc.ai_policy_show(env.project_id)
    assert state["in_force"] and state["policy"]["policy_sha256"] == shown["policy_sha256"]
    assert spend.monthly_cap(env.home) == Decimal("1")
    assert code_of(lambda: agent_call(env, "ai_policy_revoke")) == "policy_denied"
    changed = policy(agents_may_run=True)
    assert (
        code_of(lambda: env.svc.ai_policy_grant(env.project_id, changed, binding, authority=OWNER))
        == "revision_conflict"
    )
    openrouter(env)  # a new provider revision
    void = env.svc.ai_policy_show(env.project_id)
    assert void["in_force"] is False and "changed" in void["void_reason"]
    assert env.svc.ai_policy_revoke(env.project_id, authority=OWNER)["revoked"] is True
    assert env.svc.ai_policy_show(env.project_id)["policy"] is None


# ----- runs ------------------------------------------------------------------------------------


def scanned(env: Env) -> int:
    return int(env.svc.adoption_scan(env.project_id, AGENT, root_alias="research",
                                     mode="wait")["scan_gen"])  # fmt: skip


def write_cassette(folder: Path, env: Env, gen: int, path: str, role: str) -> None:
    from prometheos.adoption import assist

    store = ProjectStore(env.store_dir)
    conn = store.connect()
    try:
        bundle = assist._bundle(conn, env.research, gen, "classify_file", path)
    finally:
        conn.close()
    content = json.dumps({"result": {"role": role, "quote": None}, "abstain": None})
    folder.mkdir(exist_ok=True)
    (folder / f"{canonical_sha256(path)[:12]}.json").write_text(json.dumps({
        "task_kind": "adoption.classify_file", "input_sha256": bundle.input_sha256, "sample": 0,
        "body": {"id": "g", "model": PAIR["model"], "provider": "Mistral",
                 "usage": {"prompt_tokens": 50, "completion_tokens": 9},
                 "choices": [{"message": {"content": content}, "finish_reason": "stop"}]},
    }), encoding="utf-8")  # fmt: skip


def families(env: Env, gen: int) -> Any:
    return env.svc.adoption_families(env.project_id, AGENT, gen)


def test_runs_degrade_without_a_policy_and_cassettes_change_no_decision(
    env: Env, tmp_path: Path
) -> None:
    gen = scanned(env)
    run = lambda **kw: env.svc.ai_run(env.project_id, AGENT, task_kind="adoption.classify_file",  # noqa: E731
                                      scan_gen=gen, paths=["meeting.txt"], **kw)  # fmt: skip
    with pytest.raises(PrometheosError) as missing:
        run()
    # No provider configured: the per-call route cannot even preview (decision 0097).
    assert missing.value.code == "capability_unavailable"
    assert missing.value.details["blocker"] == "not_configured"
    tapes = tmp_path / "tapes"
    write_cassette(tapes, env, gen, "meeting.txt", "notes")
    before = families(env, gen)
    denied = run(cassettes=tapes)
    assert [i["outcome"] for i in denied["items"]] == ["denied"]  # the root is restricted
    env.svc.ai_root_sensitivity_set(env.project_id, "research", "normal", authority=OWNER)
    done = run(cassettes=tapes)
    [item] = done["items"]
    assert item["outcome"] == "accepted" and item["proposal"]["role"] == "notes"
    assert item["hint_only"] is True and done["executor"] == "cassette"
    assert env.svc.ai_results(env.project_id)["results"][0]["executor"] == "cassette"
    assert families(env, gen) == before  # a proposal changes no family, class or status


def test_a_dry_run_estimates_and_agents_need_the_policys_leave(env: Env) -> None:
    gen = scanned(env)
    openrouter(env)
    env.svc.ai_root_sensitivity_set(env.project_id, "research", "normal", authority=OWNER)
    binding = env.svc.ai_policy_preview(env.project_id, policy())["binding_sha256"]
    env.svc.ai_policy_grant(env.project_id, policy(), binding, authority=OWNER)
    owner = Caller("owner", scope="owner")
    estimate = env.svc.ai_run(
        env.project_id, owner, task_kind="adoption.classify_file", scan_gen=gen,
        paths=["meeting.txt", "sub/notes.txt"], dry_run=True,
    )  # fmt: skip
    assert estimate["dry_run"] is True and estimate["calls_at_most"] == 2
    assert Decimal(estimate["estimate_upper_usd"]) > 0
    # An agent the policy does not let run: the per-call preview, never a call (decision 0097).
    outside = env.svc.ai_run(env.project_id, AGENT, task_kind="adoption.classify_file",
                             scan_gen=gen, paths=["meeting.txt"], dry_run=True)  # fmt: skip
    assert outside["outside_policy"] is True and outside["calls_at_most"] == 1
    with pytest.raises(PrometheosError) as asked:
        env.svc.ai_run(env.project_id, AGENT, task_kind="adoption.classify_file", scan_gen=gen,
                       paths=["meeting.txt"])  # fmt: skip
    assert asked.value.code == "approval_required"


def test_accepting_a_proposal_is_one_owner_action_and_decides_nothing(
    env: Env, tmp_path: Path
) -> None:
    gen = scanned(env)
    tapes = tmp_path / "tapes"
    write_cassette(tapes, env, gen, "meeting.txt", "notes")
    env.svc.ai_root_sensitivity_set(env.project_id, "research", "normal", authority=OWNER)
    env.svc.ai_run(env.project_id, AGENT, task_kind="adoption.classify_file", scan_gen=gen,
                   paths=["meeting.txt"], cassettes=tapes)  # fmt: skip
    asked = lambda: agent_call(env, "ai_accept", task_kind="adoption.classify_file",  # noqa: E731
                               scan_gen=gen, subject="meeting.txt")  # fmt: skip
    assert code_of(asked) == "approval_required"
    assert code_of(lambda: env.svc.ai_accept(env.project_id, "adoption.classify_file", gen,
                                             "meeting.txt")) == "policy_denied"  # fmt: skip
    before = families(env, gen)
    done = env.svc.ai_accept(env.project_id, "adoption.classify_file", gen, "meeting.txt",
                             authority=OWNER)  # fmt: skip
    assert done["accepted_samples"] == 1 and done["assist_outcome"] == "accepted"
    tasks = env.svc.adoption_assist_tasks(env.project_id, AGENT, gen)["tasks"]
    assert not any(t["subject"] == "meeting.txt" and not t["done"] for t in tasks)
    after = families(env, gen)
    assert [f.get("current") for f in after["families"]] == [
        f.get("current") for f in before["families"]
    ]  # no family's current file was decided by the proposal
    nothing = lambda: env.svc.ai_accept(env.project_id, "adoption.classify_file", gen,  # noqa: E731
                                        "sub/notes.txt", authority=OWNER)  # fmt: skip
    assert code_of(nothing) == "validation_error"
