"""Decision 0014 items 1-4: a caller never commits, replays or names records above its scope.

Review 0010 follow-ups. ``Service.import_plan``/``import_commit``/``capture_meeting`` take the
caller (agent scope by default) and commit at its rank; a plan remembers the rank it was made
at; saved source checks commit at the caller's rank like ``apply``; ``prior_response`` checks
digest and rank. Each test below states the behaviour it pins; ``development/decisions/0014``
records which mutation of the implementation each one catches.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env, counts, create_entry, request
from cp7_support import PaperProject
from mcp import Client
from test_cp1_cli import Cli
from test_execution_receipts import receipt

from prometheos.app.apply import _parse_with_value, apply_request
from prometheos.domain.canonical import canonical_sha256, dumps_canonical
from prometheos.domain.ids import derived_request_id, new_id, new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def plan_as(env: Env, caller: Any, paths: list[str], sensitivity: str = "normal") -> dict[str, Any]:
    plan: dict[str, Any] = env.svc.import_plan(
        env.project_id,
        caller,
        root_alias="research",
        paths=paths,
        retention="copy",
        sensitivity=sensitivity,
    )
    return plan


def ids_in(plan: dict[str, Any]) -> list[str]:
    return [i for item in plan["items"] for i in (item["source_id"], item["version_id"])]


def mcp_commit(env: Env, plan_id: str) -> Any:
    async def main() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await client.call_tool("source_import_commit", {"plan_id": plan_id})

    return anyio.run(main)


def rewrite_request_key(env: Env, request_id: str, key: str) -> None:
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        conn.execute(
            "UPDATE requests SET request_sha256 = ? WHERE request_id = ?", (key, request_id)
        )
        conn.commit()
    finally:
        conn.close()


# ----- item 1: import plans -------------------------------------------------------------


def test_agent_cannot_commit_an_owner_plan(env: Env) -> None:
    owner_plan = plan_as(env, OWNER, ["baseline.md"], sensitivity="restricted")
    secrets = ids_in(owner_plan)
    before = counts(env.store_dir)
    # The service default (what MCP source_import_commit and the CLI use) is agent scope.
    with pytest.raises(PrometheosError) as err:
        env.svc.import_commit(env.project_id, owner_plan["plan_id"])
    assert err.value.code == "not_found"
    assert not any(s in repr(err.value.details) + str(err.value) for s in secrets)
    result = mcp_commit(env, owner_plan["plan_id"])
    assert result.is_error and "not_found" in result.content[0].text
    assert not any(s in result.content[0].text for s in secrets)
    assert counts(env.store_dir) == before  # nothing was written
    # The plan is still open for its owner.
    committed = env.svc.import_commit(env.project_id, owner_plan["plan_id"], OWNER)
    assert committed["replayed"] is False
    assert {r["id"] for r in committed["results"]} >= set(secrets)


def test_agent_cannot_replay_an_owner_plan_commit(env: Env) -> None:
    owner_plan = plan_as(env, OWNER, ["baseline.md"], sensitivity="restricted")
    secrets = ids_in(owner_plan)
    env.svc.import_commit(env.project_id, owner_plan["plan_id"], OWNER)
    before = counts(env.store_dir)
    with pytest.raises(PrometheosError) as err:
        env.svc.import_commit(env.project_id, owner_plan["plan_id"])
    assert err.value.code == "not_found"
    assert not any(s in repr(err.value.details) + str(err.value) for s in secrets)
    result = mcp_commit(env, owner_plan["plan_id"])
    assert result.is_error and not any(s in result.content[0].text for s in secrets)
    assert counts(env.store_dir) == before
    again = env.svc.import_commit(env.project_id, owner_plan["plan_id"], OWNER)
    assert again["replayed"] is True


def test_owner_commit_of_an_agent_plan_is_not_replayable_by_the_agent(env: Env) -> None:
    """Even for a plan the agent may see, the commit's response is bound to the committing
    rank: the owner's commit (rank 2) does not replay to the agent (rank 0)."""
    agent_plan = plan_as(env, AGENT, ["baseline.md"])
    first = env.svc.import_commit(env.project_id, agent_plan["plan_id"], OWNER)
    assert first["replayed"] is False
    before = counts(env.store_dir)
    with pytest.raises(PrometheosError) as err:
        env.svc.import_commit(env.project_id, agent_plan["plan_id"], AGENT)
    assert err.value.code == "request_key_conflict"
    assert counts(env.store_dir) == before
    assert env.svc.import_commit(env.project_id, agent_plan["plan_id"], OWNER)["replayed"] is True


@pytest.mark.parametrize("caller", [AGENT, OWNER], ids=["agent", "owner"])
def test_same_scope_import_flow_is_unchanged(env: Env, caller: Any) -> None:
    # Agents may still plan and commit imports, including restricted ones they cannot read back.
    plan = plan_as(env, caller, ["baseline.md", "plant-result.md"], sensitivity="restricted")
    first = env.svc.import_commit(env.project_id, plan["plan_id"], caller)
    again = env.svc.import_commit(env.project_id, plan["plan_id"], caller)
    assert first["replayed"] is False and again["replayed"] is True
    # Each call is its own operation run (decision 0017); the commit itself is replayed.
    assert first.pop("run_id") != again.pop("run_id")
    assert {**again, "replayed": False} == first
    assert env.svc.get(env.project_id, OWNER, plan["items"][0]["source_id"], None)[
        "sensitivity"
    ] == ("restricted")


def assert_owner_scope_required(err: PrometheosError, hidden_id: str) -> None:
    """The refusal for a path bound to a source above the caller's scope: a distinct,
    actionable message that names no record (not "create a new plan", which cannot help)."""
    assert err.code == "policy_denied" and err.details == {}
    text = str(err) + repr(err.details)
    assert hidden_id not in text
    assert "owner scope" in err.message and "--scope owner --capability-file" in err.message
    assert "new plan" not in err.message


def test_plan_over_a_hidden_source_needs_owner_scope_and_names_nothing(env: Env) -> None:
    hidden = env.import_files(["baseline.md"], sensitivity="restricted")["plan"]["items"][0]
    (env.research / "baseline.md").write_text("changed bytes\n", encoding="utf-8")
    for caller in (AGENT, None):  # None: the service default is agent scope
        with pytest.raises(PrometheosError) as err:
            plan_as(env, caller, ["baseline.md"])
        assert_owner_scope_required(err.value, hidden["source_id"])
    # Retention and sensitivity of the hidden source are not probed either.
    with pytest.raises(PrometheosError) as err:
        env.svc.import_plan(
            env.project_id, AGENT, root_alias="research", paths=["baseline.md"],
            retention="reference", sensitivity="normal",
        )  # fmt: skip
    assert_owner_scope_required(err.value, hidden["source_id"])
    # The owner, who can see it, gets the real new-version plan.
    owner_item = plan_as(env, OWNER, ["baseline.md"], sensitivity="restricted")["items"][0]
    assert owner_item["action"] == "new_version" and owner_item["source_id"] == hidden["source_id"]


def test_commit_over_a_source_hidden_after_planning_needs_owner_scope(env: Env) -> None:
    """The race: the agent planned before the owner registered the path as restricted."""
    agent_plan = plan_as(env, AGENT, ["baseline.md"])
    hidden = env.svc.import_commit(
        env.project_id, plan_as(env, OWNER, ["baseline.md"], "restricted")["plan_id"], OWNER
    )["results"][0]["id"]
    with pytest.raises(PrometheosError) as err:
        env.svc.import_commit(env.project_id, agent_plan["plan_id"], AGENT)
    assert_owner_scope_required(err.value, hidden)
    # A visible binding still gets the ordinary race message, with its ID.
    visible = env.import_files(["plant-result.md"])["plan"]["items"][0]
    stale = plan_as(env, AGENT, ["manuscript.md"])
    env.import_files(["manuscript.md"])
    with pytest.raises(PrometheosError) as err2:
        env.svc.import_commit(env.project_id, stale["plan_id"], AGENT)
    assert err2.value.code == "revision_conflict" and "new plan" in err2.value.message
    assert visible["source_id"] not in repr(err2.value.details)


def test_plans_stored_without_a_creating_rank_need_owner_scope(env: Env) -> None:
    plan = plan_as(env, AGENT, ["baseline.md"])
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        row = conn.execute("SELECT plan FROM import_plans WHERE plan_id = ?", (plan["plan_id"],))
        stored = json.loads(row.fetchone()[0])
        assert stored.pop("created_rank") == 0
        conn.execute(
            "UPDATE import_plans SET plan = ? WHERE plan_id = ?",
            (dumps_canonical(stored).decode(), plan["plan_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(PrometheosError) as err:
        env.svc.import_commit(env.project_id, plan["plan_id"], AGENT)
    assert err.value.code == "not_found"
    assert env.svc.import_commit(env.project_id, plan["plan_id"], OWNER)["replayed"] is False


def test_the_returned_plan_and_its_digest_do_not_carry_the_rank(env: Env) -> None:
    plan = plan_as(env, OWNER, ["baseline.md"])
    plan.pop("run_id")  # the operation run that made it (decision 0017), not part of the plan
    digest = plan.pop("plan_sha256")
    assert "created_rank" not in plan and canonical_sha256(plan) == digest


def test_cli_import_and_meeting_need_a_capability_for_owner_scope(env: Env, tmp_path: Path) -> None:
    cli = Cli(tmp_path)
    code, doc, _ = cli(
        "source", "import", "baseline.md", "--plan", "--root", "research", "--project",
        env.project_id, "--scope", "owner",
    )  # fmt: skip
    assert code == 4 and doc["error"]["code"] == "policy_denied"
    code, doc, _ = cli(
        "meeting", "capture", "--title", "t", "--notes", "-", "--project", env.project_id,
        "--scope", "owner", stdin=b"notes\n",
    )  # fmt: skip
    assert code == 4 and doc["error"]["code"] == "policy_denied"
    # Agent scope (the default) still works end to end.
    code, plan, _ = cli(
        "source", "import", "baseline.md", "--plan", "--root", "research", "--project",
        env.project_id,
    )  # fmt: skip
    assert code == 0
    code, committed, _ = cli(
        "source", "import", "--commit", plan["plan_id"], "--project", env.project_id
    )
    assert code == 0 and committed["replayed"] is False


# ----- item 1: meeting capture ------------------------------------------------------------


def test_meeting_capture_replay_is_bound_to_the_caller_rank(env: Env) -> None:
    rid = new_request_id()
    args: dict[str, Any] = {
        "title": "Embargoed supervision",
        "text": "Private notes about the draft.\n",
        "held_at": "2026-10-01T09:00:00Z",
        "sensitivity": "restricted",
        "request_id": rid,
    }
    first = env.svc.capture_meeting(env.project_id, OWNER, **args)
    secrets = [first["source_id"], first["version_id"], first["meeting_id"]]
    before = counts(env.store_dir)
    for caller in (AGENT, None):  # None is the service default: agent scope
        with pytest.raises(PrometheosError) as err:
            env.svc.capture_meeting(env.project_id, caller, **args)
        assert err.value.code == "request_key_conflict"
        assert not any(s in repr(err.value.details) + str(err.value) for s in secrets)
    assert counts(env.store_dir) == before
    replay = env.svc.capture_meeting(env.project_id, OWNER, **args)
    assert replay["commit"]["replayed"] is True and replay["meeting_id"] == first["meeting_id"]


def test_agent_meeting_capture_retries_at_the_same_scope(env: Env) -> None:
    args: dict[str, Any] = {
        "title": "Open supervision",
        "text": "Notes.\n",
        "held_at": None,
        "request_id": new_request_id(),
    }
    first = env.svc.capture_meeting(env.project_id, **args)
    again = env.svc.capture_meeting(env.project_id, AGENT, **args)
    assert again["commit"]["replayed"] is True and again["meeting_id"] == first["meeting_id"]
    with pytest.raises(PrometheosError) as err:
        env.svc.capture_meeting(env.project_id, OWNER, **args)
    assert err.value.code == "request_key_conflict"


# ----- item 3: saved source checks --------------------------------------------------------


def test_saved_source_check_commits_at_the_callers_rank(env: Env) -> None:
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    rid = new_request_id()
    first = env.svc.source_check(env.project_id, [item["version_id"]], True, OWNER, rid)
    assert first["saved"] is True and first["commit"]["replayed"] is False
    before = counts(env.store_dir)
    with pytest.raises(PrometheosError) as err:  # same visible targets, lower rank
        env.svc.source_check(env.project_id, [item["version_id"]], True, AGENT, rid)
    assert err.value.code == "request_key_conflict"
    assert counts(env.store_dir) == before
    again = env.svc.source_check(env.project_id, [item["version_id"]], True, OWNER, rid)
    assert again["commit"]["replayed"] is True
    other = new_request_id()
    agent_first = env.svc.source_check(env.project_id, [item["version_id"]], True, AGENT, other)
    agent_again = env.svc.source_check(env.project_id, [item["version_id"]], True, AGENT, other)
    assert agent_first["commit"]["replayed"] is False and agent_again["commit"]["replayed"] is True
    with pytest.raises(PrometheosError) as err:
        env.svc.source_check(env.project_id, [item["version_id"]], True, OWNER, other)
    assert err.value.code == "request_key_conflict"


# ----- item 4: other non-apply commit paths across ranks -------------------------------------


def test_receipt_record_replay_is_bound_to_the_caller_rank(env: Env) -> None:
    (env.research / "trace.csv").write_text("time_s,speed\n0,0\n", "utf-8")
    item = env.import_files(["trace.csv"], sensitivity="restricted")["plan"]["items"][0]
    output = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "sha256": item["sha256"],
        "role": "data",
    }
    raw = json.dumps(receipt([output])).encode("utf-8")
    rid = new_request_id()

    def record(caller: Any) -> Any:
        return env.svc.receipt_record(
            env.project_id, raw=raw, title="run", description="", request_id=rid, caller=caller
        )

    first = record(OWNER)
    assert first["replayed"] is False
    before = counts(env.store_dir)
    with pytest.raises(PrometheosError) as err:
        record(AGENT)
    assert err.value.code == "request_key_conflict"
    assert counts(env.store_dir) == before
    assert record(OWNER)["replayed"] is True


def test_job_add_replay_is_bound_to_the_caller_rank(env: Env) -> None:
    body = {
        "handler": "digest",
        "parameters": {},
        "schedule": {"type": "interval", "minutes": 60, "start_utc": "2030-01-01T00:00:00Z"},
        "enabled": True,
        "budget": {
            "max_inputs": 20,
            "max_output_chars": 2000,
            "max_provider_calls": 0,
            "max_concurrent_runs": 1,
            "spend_cap_estimate_usd": None,
        },
        "last_slot": None,
    }
    rid = new_request_id()
    first = env.svc.job_add(env.project_id, OWNER, body, rid)
    with pytest.raises(PrometheosError) as err:
        env.svc.job_add(env.project_id, AGENT, body, rid)
    assert err.value.code == "request_key_conflict"
    assert env.svc.job_add(env.project_id, OWNER, body, rid)["commit"]["replayed"] is True
    assert first["job_id"]


def test_legacy_row_resent_at_owner_rank_with_a_different_body_conflicts(env: Env) -> None:
    rid = new_request_id()
    raw = request(env.project_id, [create_entry(title="Legacy original")], 1, rid)
    env.svc.apply(env.project_id, raw, OWNER)
    rewrite_request_key(env, rid, _parse_with_value(raw)[1])  # the pre-binding bare digest
    different = request(env.project_id, [create_entry(title="Legacy different")], 1, rid)
    before = counts(env.store_dir)
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, different, OWNER)
    assert err.value.code == "request_key_conflict"
    assert counts(env.store_dir) == before
    # The identical body still replays at owner rank and is refused below it.
    assert env.svc.apply(env.project_id, raw, OWNER)["replayed"] is True
    with pytest.raises(PrometheosError):
        env.svc.apply(env.project_id, raw, AGENT)


# ----- item 2: prior_response -------------------------------------------------------------


def commit_under_identity(env: Env, rid: str, identity: str, rank: int) -> dict[str, Any]:
    raw = request(env.project_id, [create_entry(title="Identity-keyed")], env.seq(), rid)
    return apply_request(
        env.svc.open(env.project_id),
        raw,
        "test",
        max_rank=rank,
        request_key_sha256=identity,
    )


def test_prior_response_checks_the_digest_and_the_rank(env: Env) -> None:
    store = env.svc.open(env.project_id)
    rid, identity = new_request_id(), "a" * 64
    assert store.prior_response(rid, request_sha256=identity, max_rank=0) is None
    first = commit_under_identity(env, rid, identity, 0)
    replay = store.prior_response(rid, request_sha256=identity, max_rank=0)
    assert replay is not None and replay["replayed"] is True
    assert {**replay, "replayed": False} == first
    for digest, rank in (("b" * 64, 0), (identity, 2)):
        with pytest.raises(PrometheosError) as err:
            store.prior_response(rid, request_sha256=digest, max_rank=rank)
        assert err.value.code == "request_key_conflict"


def test_prior_response_never_returns_a_higher_rank_commit(env: Env) -> None:
    store = env.svc.open(env.project_id)
    rid, identity = new_request_id(), "c" * 64
    owner_commit = commit_under_identity(env, rid, identity, 2)
    secret = owner_commit["results"][0]["id"]
    with pytest.raises(PrometheosError) as err:
        store.prior_response(rid, request_sha256=identity, max_rank=0)
    assert err.value.code == "request_key_conflict" and secret not in repr(err.value.details)
    owner_view = store.prior_response(rid, request_sha256=identity, max_rank=2)
    assert owner_view is not None and owner_view["replayed"] is True


def test_paper_check_save_does_not_replay_an_owner_commit_under_its_key(env: Env) -> None:
    """The derived key is computable by anyone: an owner request committed under exactly that
    request ID must not be handed back (with its restricted record IDs) to the agent-scope save."""
    pp = PaperProject(env)
    paper = pp.paper()
    client_rid = new_request_id()
    key = derived_request_id("paper_check_save", pp.pid, paper, client_rid)
    secret_entry = new_id("entry")
    owner_raw = request(
        pp.pid,
        [create_entry(secret_entry, sensitivity="restricted", title="Embargoed")],
        env.seq(),
        key,
    )
    env.svc.apply(pp.pid, owner_raw, OWNER)
    before = counts(env.store_dir)
    with pytest.raises(PrometheosError) as err:
        env.svc.paper_check_save(pp.pid, paper, client_rid)
    assert err.value.code == "request_key_conflict"
    assert secret_entry not in repr(err.value.details) + str(err.value)
    assert counts(env.store_dir) == before


def test_paper_check_save_still_replays_its_own_retry(env: Env) -> None:
    pp = PaperProject(env)
    paper = pp.paper()
    rid = new_request_id()
    first = env.svc.paper_check_save(pp.pid, paper, rid)
    seq = env.seq()
    again = env.svc.paper_check_save(pp.pid, paper, rid)
    assert again["saved"]["replayed"] is True and env.seq() == seq
    assert again["saved"]["results"] == first["saved"]["results"]
