"""CP10.3 round 2 regressions (review cp10-3-a; decision 0017 "Round 2").

F1 agent views never name restricted inputs; F2 provider runs resume after their task stage;
F3 an approved external effect happens at most once per grant; F4 a failure after a sent call
is an unknown outcome; F5 an acknowledged run is not resumable; F6 background runs are bounded;
F7 run text carries no local paths; F8 all eight states read the same on every transport; F10
compile, install and provider resumes, lease takeover (R1), the execution-time payload recheck
(R7) and a pending cancellation at resume (R10); F11 CLI run IDs and exit codes; F13 a waiting
run whose approval was used elsewhere closes truthfully.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import pytest
from audio_support import AudioEnv
from cp1_support import AGENT, OWNER, OWNER_ACT, create_entry, request
from hub_support import running_hub
from mcp import Client
from provider_support import ProviderEnv, completion, config_doc
from test_cp1_cli import Cli

from prometheos.app import run_handlers, runs, texcompile
from prometheos.audio import engines, models, transcribe
from prometheos.domain.ids import derived_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server
from prometheos.providers import adapter as ad
from prometheos.providers import ledger
from prometheos.providers import transport as tp
from prometheos.store import operational as op

REPO = Path(__file__).resolve().parents[1]
PAST = "2000-01-01T00:00:00.000000Z"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AudioEnv:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    return AudioEnv(tmp_path)


def refused(fn: Any) -> PrometheosError:
    with pytest.raises(PrometheosError) as err:
        fn()
    return err.value


def stored(store_dir: Path, run_id: str) -> op.OperationRun:
    with closing(sqlite3.connect(store_dir / "record.sqlite")) as conn:
        conn.row_factory = sqlite3.Row
        found = op.get_operation_run(conn, run_id)
    assert found is not None
    return found


def sql(store_dir: Path, statement: str, params: tuple[Any, ...] = ()) -> None:
    with closing(sqlite3.connect(store_dir / "record.sqlite")) as conn:
        conn.execute(statement, params)
        conn.commit()


def expire(store_dir: Path, run_id: str) -> None:
    sql(store_dir, "UPDATE operation_runs SET lease_expires_at = ? WHERE id = ?", (PAST, run_id))


def plan_queued(env: Any, name: str = "baseline.md") -> str:
    view = env.svc.import_plan(
        env.project_id, AGENT, mode="none", root_alias="research", paths=[name]
    )
    return str(view["run_id"])


# ----- F1 / F9: agent views never name restricted inputs ----------------------------------------


def test_an_agent_compile_run_never_names_or_hashes_a_restricted_workspace_file(
    tmp_path: Path,
) -> None:
    from test_cp9_review_cp9a_r2 import _restrict_in_clone
    from test_cp9_sync import Linked

    linked = Linked(tmp_path)
    _restrict_in_clone(linked, "refs.bib")
    bib_sha = hashlib.sha256((linked.folder / "refs.bib").read_bytes()).hexdigest()
    env = runs.Env(linked.svc, linked.pid, linked.svc.open(linked.pid))
    params = {"paper_id": linked.paper_id, "fetch": False}
    agent_run, _ = runs.create(env, AGENT, "paper_compile", params)
    view = linked.svc.run_get(linked.pid, AGENT, agent_run.id)
    names = [d["name"] for d in view["input_digests"]]
    assert names and "refs.bib" not in names  # the agent's own listing hides it too
    assert "refs.bib" not in json.dumps(view) and bib_sha not in json.dumps(view)
    # The fingerprint is server-side state of the run's own rank: the stored row of the
    # agent's run holds nothing above that rank either.
    assert bib_sha not in stored(linked.env.store_dir, agent_run.id).model_dump_json()
    owner_run, _ = runs.create(env, OWNER, "paper_compile", params)
    assert "refs.bib" in [
        d["name"] for d in linked.svc.run_get(linked.pid, OWNER, owner_run.id)["input_digests"]
    ]
    assert refused(lambda: linked.svc.run_get(linked.pid, AGENT, owner_run.id)).code == (
        "not_found"
    )


# ----- F2: provider runs resume after their task stage -------------------------------------------


def test_a_provider_run_paused_at_the_cap_resumes_and_sends_each_call_once(tmp_path: Path) -> None:
    env = ProviderEnv(tmp_path)
    env.configure()
    estimate = Decimal(env.preview(AGENT)["cost"]["estimate_upper_bound_usd"])
    env.configure(config_doc(budget={"spend_cap_usd": str(estimate + Decimal("0.001"))}))
    env.script(completion("not json"), completion(env.items_reply(2, 3)))
    err = refused(env.run)
    assert err.code == "budget_exceeded"
    rid = err.details["run_id"]
    paused = env.svc.run_get(env.project_id, OWNER, rid)
    assert paused["state"] == "paused_budget" and paused["resumable"] is True
    assert [s["name"] for s in paused["stages"]] == ["task", "call:1", "submit:1"]
    tasks_before = env.query("SELECT COUNT(*) FROM records WHERE kind = 'task'")[0][0]
    env.configure(config_doc(budget={"spend_cap_usd": "5.00"}))  # the owner raises the cap
    done = env.svc.run_resume(env.project_id, OWNER, rid, mode="foreground")
    assert done["state"] == "completed" and done["result"]["attempts"] == 2
    assert len(env.transport.requests) == 2  # call 1 once, the repair once
    assert env.query("SELECT COUNT(*) FROM records WHERE kind = 'task'")[0][0] == tasks_before
    # At most once: the run's single grant was spent once, with the run; the run is final.
    assert [r for r in env.query("SELECT uses, max_uses FROM capabilities "
                                 "WHERE operation = 'provider_run'")] == [(1, 1)]  # fmt: skip
    again = refused(lambda: env.svc.run_resume(env.project_id, OWNER, rid, mode="foreground"))
    assert again.code == "validation_error" and len(env.transport.requests) == 2


def test_a_provider_run_interrupted_after_its_task_resumes_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = ProviderEnv(tmp_path)
    env.configure()
    env.script(completion(env.items_reply(2, 3)))
    real = ledger.reserve

    def stop(*args: Any, **kwargs: Any) -> Any:
        raise transcribe.Interrupted  # the worker stops after "task", before anything is sent

    monkeypatch.setattr(ledger, "reserve", stop)
    created = env.run(caller=AGENT, mode="none")
    rid = created["run_id"]
    with pytest.raises(transcribe.Interrupted):
        runs.execute(runs.Env(env.svc, env.project_id, env.svc.open(env.project_id)), rid)
    monkeypatch.setattr(ledger, "reserve", real)
    expire(env.store_dir, rid)
    view = env.svc.run_get(env.project_id, AGENT, rid)
    assert view["state"] == "queued" and view["stages"][0]["name"] == "task"
    env.apply([create_entry(title="Moves the project on")])  # context renders differently now
    done = env.svc.run_resume(env.project_id, AGENT, rid, mode="foreground")
    assert done["state"] == "completed" and len(env.transport.requests) == 1


# ----- F3: one approved external effect per grant -------------------------------------------------


def _grant(env: Any, approval_id: str) -> None:
    shown = env.svc.approval_owner_view(env.project_id, approval_id)
    env.svc.approval_decide(
        env.project_id, approval_id, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )


def test_a_failed_granted_install_is_never_repeated_under_the_same_grant(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe P6: one grant, exactly one install attempt."""
    attempts: list[str] = []

    def fake_install(home: Path, key: str, plan_sha256: str, progress: Any = None) -> Any:
        attempts.append(key)
        raise PrometheosError("source_unavailable", "download interrupted", {})

    monkeypatch.setattr(models, "install", fake_install)
    plan = env.svc.audio_models_plan("faster-whisper-base")
    params = {"model": "faster-whisper-base", "plan_sha256": plan["plan_sha256"]}
    err = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "audio_models_install", params)
    )
    approval_id = err.details["approval_request_id"]
    _grant(env, approval_id)
    assert refused(lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id)).code == (
        "source_unavailable"
    )
    (run,) = env.svc.run_list(env.project_id, AGENT)["runs"]
    assert run["state"] == "failed" and run["resumable"] is False
    assert run["external_effect"] == "confirmed"  # the failure is known: nothing installed
    for caller in (AGENT, OWNER):
        again = refused(
            lambda c=caller: env.svc.run_resume(env.project_id, c, run["run_id"], mode="foreground")
        )
        assert again.code == "validation_error" and "new approval" in again.message
    assert len(attempts) == 1


def test_an_install_stopped_mid_effect_is_unknown_external_and_not_repeated(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_models = env.home / "empty-synthetic-models"
    empty_models.mkdir()
    monkeypatch.setenv(models.MODELS_DIR_ENV, str(empty_models))
    attempts: list[str] = []

    def stops(home: Path, key: str, plan_sha256: str, progress: Any = None) -> Any:
        attempts.append(key)
        raise transcribe.Interrupted  # the worker dies during the download

    monkeypatch.setattr(models, "install", stops)
    plan = env.svc.audio_models_plan("faster-whisper-base")
    assert plan["installed"] is False
    created = env.svc.audio_models_install(
        "faster-whisper-base", plan["plan_sha256"], authority=OWNER_ACT,
        project_id=env.project_id, caller=OWNER, mode="none",
    )  # fmt: skip
    rid = created["run_id"]
    with pytest.raises(transcribe.Interrupted):
        runs.execute(runs.Env(env.svc, env.project_id, env.svc.open(env.project_id)), rid)
    assert attempts == ["faster-whisper-base"]
    expire(env.store_dir, rid)
    view = env.svc.run_get(env.project_id, OWNER, rid)
    assert view["state"] == "unknown_external" and view["external_effect"] == "uncertain"
    assert refused(lambda: env.svc.run_resume(env.project_id, OWNER, rid)).code == (
        "validation_error"
    )
    assert len(attempts) == 1


def test_an_install_interrupted_before_its_effect_resumes_and_installs_once(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def fake_install(home: Path, key: str, plan_sha256: str, progress: Any = None) -> Any:
        attempts.append(key)
        return {"model": key, "status": "installed", "location": str(home / "models" / key)}

    monkeypatch.setattr(models, "install", fake_install)
    plan = env.svc.audio_models_plan("faster-whisper-base")
    created = env.svc.audio_models_install(
        "faster-whisper-base", plan["plan_sha256"], authority=OWNER_ACT,
        project_id=env.project_id, caller=OWNER, mode="none",
    )  # fmt: skip
    rid = created["run_id"]
    done = env.svc.run_resume(env.project_id, OWNER, rid, mode="foreground")
    assert done["state"] == "completed" and attempts == ["faster-whisper-base"]
    assert "<PrometheOS" in done["result"]["location"] or not os.path.isabs(
        done["result"]["location"]
    )


# ----- F4: a failure after the call was sent is an unknown outcome --------------------------------


def test_a_billed_reply_then_a_busy_store_is_unknown_external_never_sent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe P3."""
    env = ProviderEnv(tmp_path)
    env.configure()
    env.script(completion(env.items_reply(2, 3)), completion(env.items_reply(2, 3)))
    real = ledger.mark

    def flaky(store: Any, call_id: str, state: str, **kw: Any) -> None:
        if state == "succeeded":
            raise PrometheosError("busy", "the store is busy", {})
        real(store, call_id, state, **kw)

    monkeypatch.setattr(ledger, "mark", flaky)
    err = refused(env.run)
    monkeypatch.setattr(ledger, "mark", real)
    rid = err.details["run_id"]
    view = env.svc.run_get(env.project_id, OWNER, rid)
    assert view["state"] == "unknown_external" and view["external_effect"] == "uncertain"
    assert view["resumable"] is False
    assert [c["state"] for c in env.svc.provider_calls(env.project_id, OWNER)["calls"]] == [
        "unknown"
    ]
    assert refused(lambda: env.svc.run_resume(env.project_id, OWNER, rid)).code == (
        "validation_error"
    )
    assert len(env.transport.requests) == 1


def test_an_unexpected_error_after_the_send_is_unknown_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe P4."""
    env = ProviderEnv(tmp_path)
    env.configure()
    env.script(completion(env.items_reply(2, 3)))
    real = ad.ChatCompletionsProvider.generate

    def boom(self: Any, request_: Any) -> Any:
        real(self, request_)  # sent and answered
        raise RuntimeError("unexpected reply shape at C:\\Users\\someone\\x.json")

    monkeypatch.setattr(ad.ChatCompletionsProvider, "generate", boom)
    err = refused(env.run)
    view = env.svc.run_get(env.project_id, OWNER, err.details["run_id"])
    assert view["state"] == "unknown_external" and view["external_effect"] == "uncertain"
    assert [c["state"] for c in env.svc.provider_calls(env.project_id, OWNER)["calls"]] == [
        "unknown"
    ]
    assert "someone" not in json.dumps(view)  # F7: only the exception type is kept


# ----- F5: an acknowledged run is not resumable --------------------------------------------------


def test_an_acknowledged_unknown_external_run_is_refused_explicitly(tmp_path: Path) -> None:
    """Reviewer probe P2."""
    env = ProviderEnv(tmp_path)
    env.configure()
    env.script(tp.OutcomeUnknown("TimeoutError"))
    rid = refused(env.run).details["run_id"]
    closed = env.svc.run_acknowledge(env.project_id, OWNER, rid, "checked")
    assert closed["state"] == "failed" and closed["resumable"] is False
    err = refused(lambda: env.svc.run_resume(env.project_id, OWNER, rid, mode="foreground"))
    assert err.code == "validation_error" and "unknown outcome" in err.message
    assert err.details["external_effect"] == "uncertain"
    assert len(env.transport.requests) == 1


# ----- F6: bounded background execution -----------------------------------------------------------


def test_background_runs_are_bounded_and_the_rest_wait_queued(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(runs.FAMILIES, "source_import_plan", ("import", 1))
    handler = runs.HANDLERS["source_import_plan"]
    real = handler.execute
    lock = threading.Lock()
    state = {"now": 0, "max": 0}
    # The first run is held until all four have been created and observed: the observation no
    # longer races the first run's end (a garbage-collection pause in a long test session could
    # let it finish first; CP10 fixtures round 7). Each run still lasts 0.3 s, so two running at
    # once would still be seen.
    observed = threading.Event()

    def slow(ctx: runs.RunContext) -> dict[str, Any]:
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        observed.wait(30)
        time.sleep(0.3)
        try:
            return real(ctx)
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(handler, "execute", slow)
    names = ["baseline.md", "plant-result.md", "meeting.txt", "manuscript.md"]
    try:
        views = [
            env.svc.import_plan(
                env.project_id, AGENT, mode="background", root_alias="research", paths=[name]
            )
            for name in names
        ]
        early = [stored(env.store_dir, v["run_id"]).state for v in views]
    finally:
        observed.set()
    assert early.count("queued") >= 3  # one slot for imports in this test: the rest wait
    for view in views:
        runs.wait(runs.Env(env.svc, env.project_id, env.svc.open(env.project_id)),
                  view["run_id"], 20)  # fmt: skip
    deadline = time.monotonic() + 30
    while any(stored(env.store_dir, v["run_id"]).state != "completed" for v in views):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert state["max"] == 1


# ----- F7: run text carries no local paths --------------------------------------------------------


def test_run_logs_and_errors_carry_no_local_path(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe P5."""
    from prometheos.audio import importing

    secret = str(env.home / "models" / "private-folder")

    def boom(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", secret)

    monkeypatch.setattr(importing, "import_file", boom)
    env.audio_file("a.wav")
    err = refused(
        lambda: env.svc.audio_import(
            env.project_id, AGENT, root_alias="research", path="a.wav", sensitivity="normal"
        )
    )
    for caller in (AGENT, OWNER):
        text = json.dumps(env.svc.run_get(env.project_id, caller, err.details["run_id"]))
        assert "private-folder" not in text and str(env.home) not in text
    # Decision 0019 (review R2-L1): the hub's free-text rule; an unquoted path runs to the end
    # of the line (tests/test_cp10_shared_redaction.py has the rule's cases).
    assert runs.scrub("at C:\\Users\\x\\y.txt and /home/u/z/w") == "at <local path>"
    assert runs.scrub("see https://example.org/a/b") == "see https://example.org/a/b"
    # Round 2 (INT-CP10-L1): POSIX-style paths on their own, MSYS and WSL spellings included;
    # route text is kept.
    assert runs.scrub("at /home/u/z/w") == "at <local path>"
    assert runs.scrub("at /c/Users/x/y.txt") == "at <local path>"
    assert runs.scrub("at /mnt/c/Users/x/y.txt") == "at <local path>"
    assert runs.scrub("GET /api/v1/runs/x") == "GET /api/v1/runs/x"


def test_a_spaced_path_in_a_run_error_is_consumed_whole_on_every_transport(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 0019 (CP10.3 review R2-L1): a run's stored error, read through the service and
    through MCP (which has no hub middleware), names no part of a path with spaces."""
    from prometheos.audio import importing

    secret = "C:\\Users\\John Smith\\Secret Project\\notes.txt"

    def boom(*a: Any, **k: Any) -> Any:
        raise PrometheosError("io_error", f"cannot read '{secret}' (locked)\nretry: {secret}", {})

    monkeypatch.setattr(importing, "import_file", boom)
    env.audio_file("a.wav")
    err = refused(
        lambda: env.svc.audio_import(
            env.project_id, AGENT, root_alias="research", path="a.wav", sensitivity="normal"
        )
    )
    run_id = err.details["run_id"]

    async def over_mcp() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return (await client.call_tool("run_get", {"run_id": run_id})).structured_content

    seen = [env.svc.run_get(env.project_id, c, run_id) for c in (AGENT, OWNER)]
    seen.append(anyio.run(over_mcp))
    for view in seen:
        text = json.dumps(view)
        for part in ("John", "Smith", "Secret", "Project", "notes.txt"):
            assert part not in text, (part, text[:600])
        assert "'<local path>' (locked)" in view["error"]["message"]


# ----- F8: all eight states, every transport ------------------------------------------------------


def _eight_states(env: AudioEnv) -> dict[str, str]:
    from test_cp10_runs import _states

    out = _states(env)
    host = (socket.gethostname() or "host").replace(":", "-")[:60]
    future = runs.stamp(datetime.now(UTC) + timedelta(seconds=300))
    running = plan_queued(env, "sub/notes.txt")
    sql(
        env.store_dir,
        "UPDATE operation_runs SET state = 'running', lease_owner = ?, "
        "lease_expires_at = ? WHERE id = ?",
        (f"{host}:{os.getpid()}:test:live", future, running),
    )
    out["running"] = running
    project = env.svc.get(env.project_id, OWNER, env.project_id, None)
    settings = dict(project["body"]) | {"current_objective": "A protected change."}
    raw = request(env.project_id, [{"op": "project_update", "base_rev": project["rev"],
                                    "settings": settings}], env.seq())  # fmt: skip
    approval = refused(lambda: env.svc.apply(env.project_id, raw, AGENT)).details
    waiting, _ = runs.create(
        runs.Env(env.svc, env.project_id, env.svc.open(env.project_id)), AGENT,
        "source_import_plan",
        {"root_alias": "research", "paths": ["baseline.md"], "retention": None,
         "sensitivity": "normal", "titles": None, "description": ""},
        waiting_on={"kind": "approval", "approval_request_id": approval["approval_request_id"]},
    )  # fmt: skip
    out["waiting_for_input"] = waiting.id
    uncertain = plan_queued(env, "artifact.bin")
    call = json.dumps({"provider": "test", "external_ref": None, "request_sha256": None,
                       "started_at": PAST})  # fmt: skip
    sql(env.store_dir, "UPDATE operation_runs SET state = 'running', lease_owner = 'gone', "
        "lease_expires_at = ?, external_effect = 'started', external_call = ? WHERE id = ?",
        (PAST, call, uncertain))  # fmt: skip
    env.svc.run_get(env.project_id, AGENT, uncertain)  # recovery makes it truthful
    out["unknown_external"] = uncertain
    return out


def test_all_eight_states_read_the_same_through_service_cli_mcp_and_http(
    env: AudioEnv, tmp_path: Path
) -> None:
    ids = _eight_states(env)
    assert {k: stored(env.store_dir, v).state for k, v in ids.items()} == {k: k for k in ids}
    assert len(ids) == 8
    service = {rid: env.svc.run_get(env.project_id, AGENT, rid) for rid in ids.values()}
    cli = Cli(tmp_path)
    for rid in ids.values():
        code, document, _ = cli("run", "get", rid, "--project", env.project_id)
        assert code == 0 and document == service[rid], rid

    async def run_mcp() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return {
                rid: (await client.call_tool("run_get", {"run_id": rid})).structured_content
                for rid in ids.values()
            }

    assert anyio.run(run_mcp) == service
    with running_hub(env.svc, env.project_id) as hub:
        agent = {"Authorization": f"Bearer {hub.gate.new_agent_token()}"}
        for rid in ids.values():
            reply = hub.request(
                "GET", f"/api/v1/runs/{rid}", cookie=None, csrf=None, origin=None, headers=agent
            )
            assert reply.status == 200 and reply.json() == service[rid], rid


# ----- F10: compile resume, lease takeover (R1), payload recheck (R7), pending cancel (R10) -------


def test_a_compile_resumed_after_its_registration_reports_it_and_never_registers_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_cp9_sync import Linked

    linked = Linked(tmp_path)
    calls: list[str] = []

    def registers_then_dies(store: Any, home: Any, caller: Any, paper_id: str, **kw: Any) -> Any:
        calls.append(paper_id)
        texcompile._register_outputs(
            store, paper_id=paper_id, paper_title="Linked paper",
            outputs={"log": (b"PrometheOS compile log: synthetic\n", None)},
            transformation={"type": "conversion", "engine": "tectonic", "engine_version": "x",
                            "model": None, "language": None, "parameters_digest": "0" * 64,
                            "input_version_refs": [], "run_id": None},
            sensitivity="normal", actor="test", request_id=kw["request_id"],
            max_rank=caller.rank,
        )  # fmt: skip
        raise transcribe.Interrupted  # the worker dies right after the registration

    monkeypatch.setattr(texcompile, "compile_paper", registers_then_dies)
    env = runs.Env(linked.svc, linked.pid, linked.svc.open(linked.pid))
    run, _ = runs.create(env, OWNER, "paper_compile", {"paper_id": linked.paper_id, "fetch": False})
    with pytest.raises(transcribe.Interrupted):
        runs.execute(env, run.id)
    expire(linked.env.store_dir, run.id)
    seq = linked.env.seq()
    done = linked.svc.run_resume(linked.pid, OWNER, run.id, mode="foreground")
    assert done["state"] == "completed" and done["result"]["resumed_after_registration"] is True
    assert calls == [linked.paper_id] and linked.env.seq() == seq  # nothing compiled or committed
    assert any("registered before the worker stopped" in line for line in done["limitations"])
    key = derived_request_id("compile-register", linked.pid, run.id)
    with closing(sqlite3.connect(linked.env.store_dir / "record.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE request_id = ?",
                            (key,)).fetchone()[0] == 1  # fmt: skip


def test_a_live_lease_of_another_worker_is_never_taken_over(env: AudioEnv) -> None:
    """R1: busy while another worker's lease is live; taken over only once it expired."""
    rid = plan_queued(env)
    future = runs.stamp(datetime.now(UTC) + timedelta(seconds=300))
    sql(env.store_dir, "UPDATE operation_runs SET state = 'running', lease_owner = 'elsewhere', "
        "lease_expires_at = ? WHERE id = ?", (future, rid))  # fmt: skip
    run_env = runs.Env(env.svc, env.project_id, env.svc.open(env.project_id))
    assert refused(lambda: runs.execute(run_env, rid, raise_errors=True)).code == "busy"
    assert stored(env.store_dir, rid).lease_owner == "elsewhere"
    expire(env.store_dir, rid)
    assert runs.execute(run_env, rid).state == "completed"


def test_the_approved_payload_is_rechecked_at_execution(tmp_path: Path) -> None:
    """R7: a run whose context changed after approval sends nothing."""
    env = ProviderEnv(tmp_path)
    env.configure()
    env.script(completion(env.items_reply(2, 3)))
    rid = env.run(caller=AGENT, mode="none")["run_id"]
    env.svc.capture_meeting(
        env.project_id, title="A second meeting", text="Supervisor: something new.\n",
        held_at=None,
    )  # fmt: skip
    run_env = runs.Env(env.svc, env.project_id, env.svc.open(env.project_id))
    final = runs.execute(run_env, rid)
    assert final.state == "failed" and final.error is not None
    assert final.error.code == "revision_conflict" and env.transport.requests == []


def test_resume_refuses_a_run_with_a_pending_cancellation(env: AudioEnv) -> None:
    """R10: a cancellation requested while the worker ran, then the run paused."""
    audio = env.import_audio(sensitivity="normal")
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config(), budget_seconds=5
    )
    env.svc.transcribe_run(env.project_id, AGENT, run["run_id"])
    sql(env.store_dir, "UPDATE operation_runs SET cancel_requested_at = ? WHERE id = ?",
        (PAST, run["run_id"]))  # fmt: skip
    assert env.svc.run_get(env.project_id, AGENT, run["run_id"])["resumable"] is False
    err = refused(
        lambda: env.svc.run_resume(env.project_id, AGENT, run["run_id"], budget_amount="60")
    )
    assert err.code == "validation_error" and "cancellation" in err.message
    assert stored(env.store_dir, run["run_id"]).state == "paused_budget"


# ----- F11: CLI run IDs and exit codes ------------------------------------------------------------


def test_cli_names_a_waiting_transcription_and_resume_exits_non_zero_when_it_ends_badly(
    env: AudioEnv, tmp_path: Path
) -> None:
    cli = Cli(tmp_path)
    cli.env[engines.SYNTHETIC_ENV] = "1"
    audio = env.import_audio(sensitivity="normal")
    script = tmp_path / "script.json"
    script.write_text(json.dumps(list(env.synthetic_config().script)), encoding="utf-8")
    code, done, err = cli(
        "audio", "transcribe", audio["version_id"], "--engine", "synthetic", "--chunk-seconds",
        "4", "--overlap-seconds", "2", "--script", str(script), "--project", env.project_id,
        "--budget-seconds", "5",
    )  # fmt: skip
    assert f"audio_transcribe: operation run {done['run_id']}" in err
    assert done["state"] == "paused_budget"
    code, view, _ = cli("run", "resume", done["run_id"], "--project", env.project_id)
    assert code == 2 and view["state"] == "paused_budget"  # still at its budget
    code, view, _ = cli(
        "run", "resume", done["run_id"], "--project", env.project_id, "--budget", "60"
    )
    assert code == 0 and view["state"] == "completed"
    failing = refused(
        lambda: env.svc.import_plan(
            env.project_id, AGENT, root_alias="research", paths=["no-such-file.md"]
        )
    )
    code, view, _ = cli("run", "resume", failing.details["run_id"], "--project", env.project_id)
    assert view["state"] == "failed" and code == 5  # source_unavailable


# ----- F13: a waiting run whose approval was used without it closes truthfully --------------------


def test_a_waiting_run_whose_approval_was_used_elsewhere_is_closed(tmp_path: Path) -> None:
    env = ProviderEnv(tmp_path)
    env.configure()
    shown = env.preview(AGENT)
    params = {"purpose": "meeting_extract", "meeting_id": env.meeting["meeting_id"],
              "context_budget": 12000, "digest": shown["digest"]}  # fmt: skip
    approval_id = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "provider_run", params)
    ).details["approval_request_id"]
    # A second run waiting on the same request with other parameters (a stale take-over).
    stray, _ = runs.create(
        runs.Env(env.svc, env.project_id, env.svc.open(env.project_id)), AGENT, "provider_run",
        {"purpose": "meeting_extract", "meeting_id": env.meeting["meeting_id"],
         "context_budget": 9000, "include_restricted": False,
         "approved_digest": shown["digest"], "acknowledge_unknown": None},
        waiting_on={"kind": "approval", "approval_request_id": approval_id},
        fingerprint=runs.Fingerprint(inputs=(("payload", "0" * 64),), config={}),
    )  # fmt: skip
    _grant(env, approval_id)
    env.script(completion(env.items_reply(2, 3)))
    env.svc.approval_resume(env.project_id, AGENT, approval_id)
    view = env.svc.run_get(env.project_id, AGENT, stray.id)
    assert view["state"] == "cancelled" and view["waiting_on"] is None
    assert "used without this run starting" in view["limitations"][-1]
    assert len(env.transport.requests) == 1
    assert run_handlers  # the handlers module registers every long operation
