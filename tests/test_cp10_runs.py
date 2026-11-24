"""CP10.3 durable operation runs (C10-RUNS; spec/04 "Operation runs"; decision 0017).

Long operations return a durable ``oprun_`` ID at once; queued, running, waiting_for_input,
paused_budget, completed, failed, cancelled and unknown_external persist and read the same
through the service, CLI, MCP and HTTP; resume accepts only identical inputs, parameters,
configuration, model and workflow version; a budget pause resumes without recharging finished
stages; an uncertain external call is never retried automatically; a cancellation request is
not reported as a confirmed stop; heartbeats never advance the project sequence. The
subprocess kill probes and the S25-shaped scenario are in ``test_cp10_runs_kill.py``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from audio_support import AudioEnv
from cp1_support import AGENT, OWNER, OWNER_ACT
from hub_support import running_hub
from mcp import Client
from provider_support import ProviderEnv, completion
from test_cp1_cli import Cli

from prometheos.app import runs
from prometheos.audio import engines, transcribe
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server
from prometheos.providers import transport as tp
from prometheos.store import admission
from prometheos.store import operational as op

RUN_ID = re.compile(r"^oprun_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AudioEnv:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    return AudioEnv(tmp_path)


def refused(fn: Any) -> PrometheosError:
    with pytest.raises(PrometheosError) as err:
        fn()
    return err.value


def row(env: AudioEnv, run_id: str) -> op.OperationRun:
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        conn.row_factory = sqlite3.Row
        found = op.get_operation_run(conn, run_id)
    assert found is not None
    return found


def sql(env: AudioEnv, statement: str, params: tuple[Any, ...] = ()) -> None:
    env.execute(statement, params)


def plan_queued(env: AudioEnv, name: str = "baseline.md") -> dict[str, Any]:
    return env.svc.import_plan(
        env.project_id, AGENT, mode="none", root_alias="research", paths=[name]
    )


# ----- a run ID at once ----------------------------------------------------------------------


def test_a_long_operation_answers_its_run_id_at_once(env: AudioEnv) -> None:
    view = plan_queued(env)
    assert RUN_ID.fullmatch(view["run_id"]) and view["state"] == "queued"
    assert view["result"] is None and view["operation"] == "source_import_plan"
    assert env.query("SELECT COUNT(*) FROM import_plans")[0][0] == 0  # nothing ran yet
    done = env.svc.run_resume(env.project_id, AGENT, view["run_id"], mode="foreground")
    assert done["run_id"] == view["run_id"] and done["state"] == "completed"
    plan = done["result"]
    assert plan["plan_id"] == view["run_id"].removeprefix("oprun_")  # derived from the run
    # A server answers at once and executes in a thread; a bounded wait (at most 20 s) lets a
    # quick operation answer with its result in the same call.
    commit = env.svc.import_commit(
        env.project_id, plan["plan_id"], AGENT, mode="background", wait_seconds=20
    )
    assert commit["state"] == "completed" and commit["result"]["ok"] is True
    assert {a["role"] for a in commit["artifacts"]} == {"imported"}
    assert commit["result_seq"] == commit["result"]["project_seq"]
    # Callers that wait (the CLI default, in-process callers) get the result with its run_id.
    waited = env.svc.import_plan(
        env.project_id, AGENT, root_alias="research", paths=["plant-result.md"]
    )
    assert RUN_ID.fullmatch(waited["run_id"]) and waited["plan_id"] in waited["run_id"]
    refused_wait = refused(
        lambda: env.svc.import_plan(
            env.project_id, AGENT, mode="background", wait_seconds=21, root_alias="research",
            paths=["sub/notes.txt"],
        )
    )  # fmt: skip
    assert refused_wait.code == "validation_error"


def test_an_identical_start_request_returns_the_same_run(env: AudioEnv) -> None:
    rid = "00000000-0000-4000-8000-0000000000a1"

    def start(paths: list[str]) -> dict[str, Any]:
        return env.svc.import_plan(
            env.project_id, AGENT, mode="none", request_id=rid, root_alias="research",
            paths=paths,
        )  # fmt: skip

    first = start(["baseline.md"])
    assert start(["baseline.md"])["run_id"] == first["run_id"]
    assert refused(lambda: start(["plant-result.md"])).code == "request_key_conflict"
    assert env.query("SELECT COUNT(*) FROM operation_runs")[0][0] == 1


# ----- one read surface ----------------------------------------------------------------------


def _states(env: AudioEnv) -> dict[str, str]:
    """Runs in several persisted states (each reached through the service)."""
    out: dict[str, str] = {}
    out["queued"] = plan_queued(env)["run_id"]
    done = env.svc.import_plan(
        env.project_id, AGENT, root_alias="research", paths=["sub/notes.txt"]
    )
    out["completed"] = done["run_id"]
    cancelled = plan_queued(env, "plant-result.md")
    env.svc.run_cancel(env.project_id, AGENT, cancelled["run_id"])
    out["cancelled"] = cancelled["run_id"]
    planned = env.svc.import_plan(
        env.project_id, AGENT, root_alias="research", paths=["manuscript.md"]
    )
    (env.research / "manuscript.md").write_text("changed after the plan\n", "utf-8")
    failed = refused(lambda: env.svc.import_commit(env.project_id, planned["plan_id"], AGENT))
    out["failed"] = failed.details["run_id"]
    audio = env.import_audio(sensitivity="normal")
    paused = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config(), budget_seconds=5
    )
    env.svc.transcribe_run(env.project_id, AGENT, paused["run_id"])
    out["paused_budget"] = paused["run_id"]
    return out


def test_run_states_persist_and_read_the_same_through_service_cli_mcp_and_http(
    env: AudioEnv, tmp_path: Path
) -> None:
    ids = _states(env)
    assert {k: row(env, v).state for k, v in ids.items()} == {k: k for k in ids}
    service = {rid: env.svc.run_get(env.project_id, AGENT, rid) for rid in ids.values()}
    listed = env.svc.run_list(env.project_id, AGENT, limit=50)
    assert {r["run_id"] for r in listed["runs"]} >= set(ids.values())

    cli = Cli(tmp_path)
    for rid in ids.values():
        code, document, _ = cli("run", "get", rid, "--project", env.project_id)
        assert code == 0 and document == service[rid], rid
    code, cli_list, _ = cli("run", "list", "--project", env.project_id, "--limit", "50")
    assert code == 0 and cli_list == env.svc.run_list(env.project_id, AGENT, limit=50)

    async def steps(client: Client) -> Any:
        got = {}
        for rid in ids.values():
            got[rid] = (await client.call_tool("run_get", {"run_id": rid})).structured_content
        listing = (await client.call_tool("run_list", {"limit": 50})).structured_content
        return got, listing

    async def run_mcp() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await steps(client)

    mcp_views, mcp_list = anyio.run(run_mcp)
    assert mcp_views == service
    assert mcp_list == env.svc.run_list(env.project_id, AGENT, limit=50)

    with running_hub(env.svc, env.project_id) as hub:
        token = hub.gate.new_agent_token()
        agent = {"Authorization": f"Bearer {token}"}
        for rid in ids.values():
            reply = hub.request(
                "GET", f"/api/v1/runs/{rid}", cookie=None, csrf=None, origin=None, headers=agent
            )
            assert reply.status == 200 and reply.json() == service[rid], rid
        reply = hub.request(
            "GET", "/api/v1/runs?limit=50", cookie=None, csrf=None, origin=None, headers=agent
        )
        assert reply.json() == env.svc.run_list(env.project_id, AGENT, limit=50)
        # The same refusal on every transport: an unknown run.
        missing = "oprun_00000000-0000-4000-8000-00000000dead"
        reply = hub.request(
            "GET", f"/api/v1/runs/{missing}", cookie=None, csrf=None, origin=None, headers=agent
        )
        assert reply.status == 404 and reply.json()["error"]["code"] == "not_found"
    code, document, _ = cli("run", "get", missing, "--project", env.project_id)
    assert code != 0 and document["error"]["code"] == "not_found"


def test_an_owners_run_reads_as_absent_to_an_agent(env: AudioEnv) -> None:
    owned = env.svc.import_plan(
        env.project_id, OWNER, mode="none", root_alias="research", paths=["baseline.md"]
    )
    assert env.svc.run_get(env.project_id, OWNER, owned["run_id"])["caller_rank"] == 2
    assert refused(lambda: env.svc.run_get(env.project_id, AGENT, owned["run_id"])).code == (
        "not_found"
    )
    assert owned["run_id"] not in {
        r["run_id"] for r in env.svc.run_list(env.project_id, AGENT)["runs"]
    }
    for control in (
        lambda: env.svc.run_cancel(env.project_id, AGENT, owned["run_id"]),
        lambda: env.svc.run_resume(env.project_id, AGENT, owned["run_id"]),
    ):
        assert refused(control).code == "not_found"


# ----- resume compatibility --------------------------------------------------------------------


def test_resume_refuses_a_changed_input_and_changes_nothing(env: AudioEnv) -> None:
    plan = env.svc.import_plan(env.project_id, AGENT, root_alias="research", paths=["baseline.md"])
    queued = env.svc.import_commit(env.project_id, plan["plan_id"], AGENT, mode="none")
    seq = env.seq()
    (env.research / "baseline.md").write_text("an edited baseline\n", "utf-8")
    err = refused(
        lambda: env.svc.run_resume(env.project_id, AGENT, queued["run_id"], mode="foreground")
    )
    assert err.code == "stale_task" and err.details["changed"] == ["input_digest"]
    assert "start a new run" in err.message
    after = env.svc.run_get(env.project_id, AGENT, queued["run_id"])
    assert after["state"] == "queued" and after["attempts"] == 0 and env.seq() == seq


def test_resume_refuses_changed_configuration_model_or_workflow_version(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = env.import_audio(sensitivity="normal")
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config(), budget_seconds=5
    )
    rid = run["run_id"]
    env.svc.transcribe_run(env.project_id, AGENT, rid)  # pauses at its budget
    assert row(env, rid).state == "paused_budget"

    def resume() -> PrometheosError:
        return refused(lambda: env.svc.run_resume(env.project_id, AGENT, rid, mode="foreground"))

    real = engines.engine_version_without_loading
    monkeypatch.setattr(
        engines, "engine_version_without_loading", lambda name: f"{real(name)}-upgraded"
    )
    assert resume().details["changed"] == ["config_digest"]
    monkeypatch.setattr(engines, "engine_version_without_loading", real)
    sql(env, "UPDATE operation_runs SET workflow_version = 'transcribe-v0' WHERE id = ?", (rid,))
    assert resume().details["changed"] == ["workflow_version"]
    sql(env, "UPDATE operation_runs SET workflow_version = 'transcribe-v1', model = 'other' "
        "WHERE id = ?", (rid,))  # fmt: skip
    assert resume().details["changed"] == ["model"]
    sql(env, "UPDATE operation_runs SET model = 'synthetic' WHERE id = ?", (rid,))
    assert row(env, rid).state == "paused_budget"  # every refusal changed nothing
    resumed = env.svc.run_resume(env.project_id, AGENT, rid, budget_amount="60", mode="foreground")
    assert resumed["state"] == "completed"


# ----- budget pause ---------------------------------------------------------------------------


def test_a_budget_pause_resumes_without_recharging_finished_stages(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = engines.SyntheticEngine.transcribe

    def counted(self: Any, data: bytes, start_ms: int, language: str | None) -> Any:
        calls.append(start_ms)
        return real(self, data, start_ms, language)

    monkeypatch.setattr(engines.SyntheticEngine, "transcribe", counted)
    audio = env.import_audio(sensitivity="normal")
    # 8 s of audio in 4 s chunks with 2 s overlap: three chunks of 4 s; 9 s pays for two.
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config(), budget_seconds=9
    )
    rid = run["run_id"]
    paused = env.svc.transcribe_run(env.project_id, AGENT, rid)
    assert paused["state"] == "paused_budget" and paused["chunks_done"] == 2 and calls == [0, 2000]
    view = env.svc.run_get(env.project_id, AGENT, rid)
    assert view["budget"] == {
        "reservation": {"unit": "seconds", "amount": "9"},
        "charged": "8.000000",
    }
    assert view["resumable"] and "budget is used up" in view["logs"][-1]["message"]
    first_two = {
        s["name"]: s["completed_at"] for s in view["stages"] if s["name"].startswith("chunk")
    }
    # Executing it again without a larger budget stays refused; a lower one is refused too.
    assert refused(lambda: env.svc.transcribe_run(env.project_id, AGENT, rid)).code == (
        "budget_exceeded"
    )
    assert (
        refused(lambda: env.svc.run_resume(env.project_id, AGENT, rid, budget_amount="5")).code
        == "validation_error"
    )
    done = env.svc.run_resume(env.project_id, AGENT, rid, budget_amount="12", mode="foreground")
    assert done["state"] == "completed" and calls == [0, 2000, 4000]  # only the missing chunk
    assert done["budget"]["charged"] == "12.000000"
    stages = {s["name"]: s for s in done["stages"]}
    assert {k: stages[k]["completed_at"] for k in first_two} == first_two  # never recharged
    assert [s["charged"] for n, s in stages.items() if n.startswith("chunk")] == ["4.000000"] * 3
    transcripts = [a for a in done["artifacts"] if a["role"] == "transcript"]
    assert len(transcripts) == 1 and done["result"]["segments"] >= 1


# ----- unknown external outcomes ---------------------------------------------------------------


def test_an_uncertain_external_call_is_unknown_external_and_never_repeated(
    tmp_path: Path,
) -> None:
    env = ProviderEnv(tmp_path)
    env.configure()
    env.script(tp.OutcomeUnknown("TimeoutError"))
    err = refused(env.run)  # the waited form keeps the operation's own error envelope
    assert err.code == "source_unavailable" and err.details["status"] == "unknown_outcome"
    rid = err.details["run_id"]
    view = env.svc.run_get(env.project_id, OWNER, rid)
    assert view["state"] == "unknown_external" and view["external_effect"] == "uncertain"
    assert view["external_call"]["provider"] == "provider api.example.test"
    assert view["external_call"]["request_sha256"] == view["input_digests"][0]["sha256"]
    assert any("not repeated automatically" in line for line in view["limitations"])
    assert len(env.transport.requests) == 1
    store = env.svc.open(env.project_id)
    for again in (
        lambda: env.svc.run_resume(env.project_id, OWNER, rid, mode="foreground"),
        lambda: runs.execute(runs.Env(env.svc, env.project_id, store), rid),
        lambda: env.svc.run_cancel(env.project_id, OWNER, rid),
    ):
        assert refused(again).code == "validation_error"
    assert len(env.transport.requests) == 1  # nothing was sent again
    closed = env.svc.run_acknowledge(env.project_id, OWNER, rid, "checked the provider console")
    assert closed["state"] == "failed" and closed["external_effect"] == "uncertain"
    assert closed["error"]["code"] == "source_unavailable"
    assert "acknowledged" in closed["error"]["message"] and closed["finished_at"]
    # Refused explicitly because the outcome is unknown (round 2, F5), not by accident.
    after = refused(lambda: env.svc.run_resume(env.project_id, OWNER, rid))
    assert after.code == "validation_error" and "unknown outcome" in after.message
    assert closed["resumable"] is False
    assert len(env.transport.requests) == 1
    ledger = env.svc.provider_calls(env.project_id, OWNER)["calls"]
    assert [c["state"] for c in ledger] == ["unknown"]


def test_a_definite_provider_failure_is_failed_with_a_known_outcome(tmp_path: Path) -> None:
    env = ProviderEnv(tmp_path)
    env.configure()
    env.script(tp.NotSent("ConnectionRefusedError"))
    err = refused(env.run)
    view = env.svc.run_get(env.project_id, OWNER, err.details["run_id"])
    assert view["state"] == "failed" and view["external_effect"] == "confirmed"
    assert view["error"]["details"]["status"] == "provider_unavailable"


# ----- cancellation ---------------------------------------------------------------------------


def test_a_cancel_request_is_not_reported_as_a_confirmed_stop(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    rid = run["run_id"]
    seen: dict[str, Any] = {}

    def during(index: int) -> None:
        if index == 0:
            seen["reply"] = env.svc.run_cancel(env.project_id, AGENT, rid)
            seen["view"] = env.svc.run_get(env.project_id, AGENT, rid)

    store = env.svc.open(env.project_id)
    result = transcribe.execute(store, env.svc.home, rid, owner="w", after_chunk=during)
    assert seen["reply"]["outcome"] == "cancel_requested"
    assert seen["view"]["state"] == "running" and seen["view"]["cancel_confirmed"] is False
    assert seen["view"]["cancel_requested_at"] and seen["view"]["finished_at"] is None
    assert result["state"] == "cancelled"
    final = env.svc.run_get(env.project_id, AGENT, rid)
    assert final["cancel_confirmed"] is True and final["finished_at"]
    assert any(line["message"] == "cancellation confirmed by the worker" for line in final["logs"])
    assert final["result"] is None and not final["artifacts"]
    # Nothing executes a queued run: it is cancelled at once, and only once.
    queued = plan_queued(env)
    reply = env.svc.run_cancel(env.project_id, AGENT, queued["run_id"])
    assert reply["outcome"] == "cancelled" and reply["run"]["cancel_confirmed"] is True
    assert refused(lambda: env.svc.run_cancel(env.project_id, AGENT, queued["run_id"])).code == (
        "validation_error"
    )
    assert (
        refused(lambda: env.svc.run_resume(env.project_id, AGENT, queued["run_id"])).code
        == "validation_error"
    )


def test_a_cancel_requested_while_the_worker_died_is_confirmed_by_recovery(
    env: AudioEnv,
) -> None:
    queued = plan_queued(env)
    rid = queued["run_id"]
    past = "2000-01-01T00:00:00.000000Z"
    sql(
        env,
        "UPDATE operation_runs SET state = 'running', lease_owner = 'gone', "
        "lease_expires_at = ?, cancel_requested_at = ? WHERE id = ?",
        (past, past, rid),
    )
    view = env.svc.run_get(env.project_id, AGENT, rid)
    assert view["state"] == "cancelled" and view["cancel_confirmed"] is True
    assert view["logs"][-1]["message"] == "cancellation confirmed: the worker had stopped"


# ----- heartbeats and the project sequence ------------------------------------------------------


def test_heartbeats_and_status_writes_never_advance_the_project_sequence(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    seq = env.seq()
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    rid = run["run_id"]
    assert env.seq() == seq  # creating a run is operational state
    store = env.svc.open(env.project_id)
    sql(env, "UPDATE operation_runs SET state = 'running', lease_owner = 'beat', "
        "lease_expires_at = ? WHERE id = ?",
        (runs.stamp(datetime.now(UTC) + timedelta(seconds=60)), rid))  # fmt: skip
    for _ in range(3):
        assert runs.heartbeat(store, rid, "beat") is True
    assert runs.heartbeat(store, rid, "someone-else") is False
    assert env.seq() == seq
    sql(env, "UPDATE operation_runs SET state = 'queued', lease_owner = NULL, "
        "lease_expires_at = NULL WHERE id = ?", (rid,))  # fmt: skip
    done = env.svc.transcribe_run(env.project_id, AGENT, rid)
    logs = env.query("SELECT COUNT(*) FROM operation_run_logs WHERE run_id = ?", (rid,))[0][0]
    assert done["state"] == "completed" and logs >= 3
    assert env.seq() == seq + 1  # only the transcript commit, through the transaction service
    assert env.svc.run_get(env.project_id, AGENT, rid)["result_seq"] == seq + 1


# ----- restart recovery -----------------------------------------------------------------------


def _finished_pid() -> int:
    probe = subprocess.Popen([sys.executable, "-c", "pass"])
    probe.wait()
    return int(probe.pid)


def test_recovery_makes_interrupted_runs_truthful(env: AudioEnv) -> None:
    past = "2000-01-01T00:00:00.000000Z"
    future = runs.stamp(datetime.now(UTC) + timedelta(seconds=120))
    plain, external, alive, dead = (plan_queued(env)["run_id"] for _ in range(4))
    call = json.dumps(
        {"provider": "test", "external_ref": None, "request_sha256": None, "started_at": past}
    )
    sql(env, "UPDATE operation_runs SET state = 'running', lease_owner = 'w', "
        "lease_expires_at = ?, stage = 'hash' WHERE id = ?", (past, plain))  # fmt: skip
    sql(env, "UPDATE operation_runs SET state = 'running', lease_owner = 'w', "
        "lease_expires_at = ?, external_effect = 'started', external_call = ? WHERE id = ?",
        (past, call, external))  # fmt: skip
    host = socket.gethostname().replace(":", "-")[:60]
    live_owner = f"{host}:{os.getpid()}:test:aaaa1111"
    sql(env, "UPDATE operation_runs SET state = 'running', lease_owner = ?, "
        "lease_expires_at = ? WHERE id = ?", (live_owner, future, alive))  # fmt: skip
    gone_owner = f"{host}:{_finished_pid()}:test:bbbb2222"
    sql(env, "UPDATE operation_runs SET state = 'running', lease_owner = ?, "
        "lease_expires_at = ? WHERE id = ?", (gone_owner, future, dead))  # fmt: skip
    seq = env.seq()
    views = {
        rid: env.svc.run_get(env.project_id, AGENT, rid) for rid in (plain, external, alive, dead)
    }
    assert views[plain]["state"] == "queued" and views[plain]["resumable"]
    assert "interrupted during hash" in views[plain]["logs"][-1]["message"]
    assert views[external]["state"] == "unknown_external"
    assert views[external]["external_effect"] == "uncertain" and not views[external]["resumable"]
    assert views[alive]["state"] == "running"  # a live lease is never taken over
    # The worker's process on this computer has ended: no need to wait for its lease.
    assert views[dead]["state"] == "queued"
    assert env.seq() == seq
    resumed = env.svc.run_resume(env.project_id, AGENT, plain, mode="foreground")
    assert resumed["state"] == "completed" and resumed["attempts"] == 1


# ----- approvals: one run from the request to the result ---------------------------------------


def _grant(env: ProviderEnv, approval_id: str) -> None:
    shown = env.svc.approval_owner_view(env.project_id, approval_id)
    env.svc.approval_decide(
        env.project_id, approval_id, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )


def test_a_protected_long_operation_waits_on_its_approval_under_one_run_id(
    tmp_path: Path,
) -> None:
    env = ProviderEnv(tmp_path)
    env.configure()
    shown = env.preview(AGENT)
    params = {
        "purpose": "meeting_extract",
        "meeting_id": env.meeting["meeting_id"],
        "context_budget": 12000,
        "digest": shown["digest"],
    }
    rid_request = "00000000-0000-4000-8000-0000000000b2"

    def ask() -> PrometheosError:
        return refused(
            lambda: env.svc.approval_request(
                env.project_id, AGENT, "provider_run", params, rid_request
            )
        )

    err = ask()
    assert err.code == "approval_required"
    approval_id = err.details["approval_request_id"]
    waiting = env.svc.run_list(env.project_id, AGENT, state="waiting_for_input")["runs"]
    assert len(waiting) == 1 and waiting[0]["operation"] == "provider_run"
    assert waiting[0]["waiting_on"] == {"kind": "approval", "approval_request_id": approval_id}
    rid = waiting[0]["run_id"]
    # A retry of the same request does not add a second run; resuming before the owner decides
    # answers approval_required again; nothing was sent.
    assert ask().details["approval_request_id"] == approval_id
    assert len(env.svc.run_list(env.project_id, AGENT)["runs"]) == 1
    assert refused(lambda: env.svc.run_resume(env.project_id, AGENT, rid)).code == (
        "approval_required"
    )
    assert env.transport.requests == []
    _grant(env, approval_id)
    env.script(completion(env.items_reply(2, 3)))
    done = env.svc.run_resume(env.project_id, AGENT, rid)
    assert done["run_id"] == rid and done["state"] == "completed"
    assert done["result"]["ok"] is True and len(env.transport.requests) == 1
    approval = env.svc.approval_get(env.project_id, AGENT, approval_id)
    assert approval["state"] == "used" and approval["result"]["run_id"] == rid
    assert len(env.svc.run_list(env.project_id, AGENT)["runs"]) == 1  # one run throughout
    # A denied request: its waiting run never starts and reads as cancelled.
    smaller = env.preview(AGENT, context_budget=8000)
    second = params | {"context_budget": 8000, "digest": smaller["digest"]}
    err = refused(lambda: env.svc.approval_request(env.project_id, AGENT, "provider_run", second))
    assert err.code == "approval_required"
    denied_id = err.details["approval_request_id"]
    decided = env.svc.approval_owner_view(env.project_id, denied_id)
    env.svc.approval_decide(
        env.project_id, denied_id, grant=False, plan_sha256=decided["plan_sha256"], owner=OWNER_ACT
    )
    ended = [r for r in env.svc.run_list(env.project_id, AGENT)["runs"] if r["run_id"] != rid]
    assert len(ended) == 1 and ended[0]["state"] == "cancelled"
    assert "approval request ended (denied)" in ended[0]["limitations"][-1]
    assert len(env.transport.requests) == 1


# ----- jobs ------------------------------------------------------------------------------------


def test_a_run_can_name_the_job_slot_that_started_it(env: AudioEnv) -> None:
    from prometheos.domain.ids import new_id

    job_id = new_id("job")
    view = env.svc.run_start(
        env.project_id, AGENT, "source_import_plan",
        {"root_alias": "research", "paths": ["baseline.md"], "retention": None,
         "sensitivity": "normal", "titles": None, "description": ""},
        job=(job_id, "2026-10-07T09:00:00Z"), mode="none",
    )  # fmt: skip
    assert view["job_id"] == job_id and view["job_due_utc"] == "2026-10-07T09:00:00Z"


# ----- CP6 run IDs ------------------------------------------------------------------------------


def test_transcription_run_ids_from_cp6_still_resolve(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal")
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    bare = run["run_id"].removeprefix("oprun_")
    assert env.svc.transcription_status(env.project_id, AGENT, bare)["run_id"] == run["run_id"]
    assert env.svc.run_get(env.project_id, AGENT, bare)["run_id"] == run["run_id"]


def test_every_view_matches_the_run_view_contract(env: AudioEnv) -> None:
    """Every view the service produces validates against the exported run view contract, and
    each stored row against the run row contract (both pinned by check_contracts)."""
    ids = _states(env)
    for rid in ids.values():
        op.RunView.model_validate(env.svc.run_get(env.project_id, AGENT, rid), strict=True)
        stored = row(env, rid)
        assert stored.params_digest == canonical_sha256(stored.params)


def test_mcp_and_http_answer_a_long_operation_with_its_run_at_once(env: AudioEnv) -> None:
    async def steps(client: Client) -> Any:
        reply = await client.call_tool(
            "source_import_plan", {"root_alias": "research", "paths": ["baseline.md"]}
        )
        return reply.structured_content

    async def run_mcp() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await steps(client)

    mcp_view = anyio.run(run_mcp)
    assert RUN_ID.fullmatch(mcp_view["run_id"]) and mcp_view["operation"] == "source_import_plan"
    assert mcp_view["state"] in ("queued", "running", "completed")
    with running_hub(env.svc, env.project_id) as hub:
        token = hub.gate.new_agent_token()
        agent = {"Authorization": f"Bearer {token}"}
        reply = hub.request(
            "POST", "/api/v1/sources/plans",
            {"root_alias": "research", "paths": ["plant-result.md"]},
            cookie=None, csrf=None, origin=None, headers=agent,
        )  # fmt: skip
        assert reply.status == 200 and RUN_ID.fullmatch(reply.json()["run_id"])
        http_id = reply.json()["run_id"]
    for rid in (mcp_view["run_id"], http_id):
        runs.wait(runs.Env(env.svc, env.project_id, env.svc.open(env.project_id)), rid, 20)
        assert row(env, rid).state == "completed" and row(env, rid).actor in ("mcp", "http-agent")


def test_without_wal_a_long_reader_delays_a_heartbeat_or_cancel_but_never_falsifies_it(
    env: AudioEnv,
) -> None:
    """Decision 0017 keeps journal_mode=DELETE: while another connection holds a read
    snapshot, a status write waits (busy timeout) and then reports busy. A heartbeat is
    skipped (the lease is long enough to survive it), and a cancellation is refused as busy
    (retryable), never recorded as a stop that did not happen."""
    queued = plan_queued(env)
    rid = queued["run_id"]
    future = runs.stamp(datetime.now(UTC) + timedelta(seconds=120))
    sql(env, "UPDATE operation_runs SET state = 'running', lease_owner = 'beat', "
        "lease_expires_at = ? WHERE id = ?", (future, rid))  # fmt: skip
    store = env.svc.open(env.project_id)
    reader = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM records").fetchone()
        assert runs.heartbeat(store, rid, "beat") is False
        err = refused(lambda: env.svc.run_cancel(env.project_id, AGENT, rid))
        assert err.code == "busy"
    finally:
        reader.execute("ROLLBACK")
        reader.close()
    after = row(env, rid)
    assert after.state == "running" and after.cancel_requested_at is None
    assert after.lease_expires_at == future  # untouched by the skipped heartbeat
    assert runs.heartbeat(store, rid, "beat") is True


# ----- decision 0076: pushed run progress for the hub ------------------------------------------


def test_the_owners_run_list_waits_for_a_run_write_on_memory_and_agents_get_no_count(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[float] = []
    original = admission.Gate.__init__

    def counting(self: admission.Gate, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        taken.append(time.monotonic())

    header = "x-prometheos-run-generation"  # the test client lowercases header names
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        first = hub.request("GET", "/api/v1/runs?limit=1")
        start = int(first.headers[header])
        monkeypatch.setattr(admission.Gate, "__init__", counting)
        one_read = hub.request("GET", "/api/v1/runs?limit=1")
        per_read = len(taken)
        assert per_read >= 1
        # Nothing written: the owner's watch waits its full time on memory alone; the store is
        # read once, for the list it answers with.
        taken.clear()
        began = time.monotonic()
        idle = hub.request("GET", f"/api/v1/runs?limit=1&watch_after={start}&wait=1")
        assert idle.status == 200 and int(idle.headers[header]) == start
        assert idle.json() == one_read.json()
        assert time.monotonic() - began >= 0.9
        assert len(taken) == per_read
        # A watch waiting while a run is created and worked on answers at once, with the run.
        answer: dict[str, Any] = {}

        def watch() -> None:
            reply = hub.request("GET", f"/api/v1/runs?limit=5&watch_after={start}&wait=20")
            answer.update(generation=int(reply.headers[header]), runs=reply.json()["runs"])

        waiter = threading.Thread(target=watch)
        waiter.start()
        time.sleep(0.3)
        token = hub.gate.new_agent_token()
        agent = {"Authorization": f"Bearer {token}"}
        began = time.monotonic()
        reply = hub.request(
            "POST", "/api/v1/sources/plans", {"root_alias": "research", "paths": ["baseline.md"]},
            cookie=None, csrf=None, origin=None, headers=agent,
        )  # fmt: skip
        assert reply.status == 200 and RUN_ID.fullmatch(reply.json()["run_id"])
        waiter.join(10)
        assert answer["generation"] > start
        assert reply.json()["run_id"] in {r["run_id"] for r in answer["runs"]}
        assert time.monotonic() - began < 5
        # An agent gets its list at once and no count, even when it asks to watch.
        began = time.monotonic()
        seen = hub.request(
            "GET", f"/api/v1/runs?watch_after={answer['generation']}&wait=20",
            cookie=None, csrf=None, origin=None, headers=agent,
        )  # fmt: skip
        assert seen.status == 200 and header not in seen.headers
        assert time.monotonic() - began < 5
