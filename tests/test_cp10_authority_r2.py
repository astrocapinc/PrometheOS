"""CP10.2 round 2 regressions (review cp10-2-a; decision 0016 "Round 2").

F1 recordings start only where the owner sees them, within a window and a maximum duration,
and the requester can stop what it caused; F2 the owner reviews the full effect; F3 a stale
apply says the project changed; F4 the agent token reaches every agent route; F5 grant and
revoke answer their contracts; F6 at-most-once, the requester's rank and the push kind; F7
no home paths; F8 escaped, explicitly truncated CLI plans; F9 the queue reaches every pending
request; F10 an ended request says so on retry; F11 agent-token commits are audited as the
agent; F14 owner handlers check the owner; F15 agent tokens expire.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env, create_entry, request
from hub_support import running_hub
from mcp import Client
from test_cp10_authority import ask, grant, refused, rows, settings_change

from prometheos.app import approvals
from prometheos.domain.models import CapabilityRevokeReceipt, PrometheosError
from prometheos.mcp_adapter import build_server


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _mcp(env: Env, steps: Any) -> Any:
    async def run() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await steps(client)

    return anyio.run(run)


# ----- F1: recordings ----------------------------------------------------------------------


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch) -> Any:
    from prometheos.audio import capture, engines

    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    monkeypatch.setenv(capture.BACKEND_ENV, "synthetic")
    monkeypatch.setenv("PROMETHEOS_SYNTHETIC_SPEED", "0")
    return capture.get_backend("synthetic")


def _recording_request(env: Env, **extra: Any) -> Any:
    params = {"title": "Lab meeting", "device_ids": ["synthetic-mic"], **extra}
    err = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "recording_start", params)
    )
    assert err.code == "approval_required"
    return err.details


def test_a_recording_plan_carries_its_window_and_maximum_duration(env: Env) -> None:
    details = _recording_request(env, max_seconds=90)
    plan = env.svc.approval_get(env.project_id, AGENT, details["approval_request_id"])["plan"]
    assert plan["max_seconds"] == 90 and plan["start_within_seconds"] == 600
    assert "90 s" in " ".join(
        env.svc.approval_get(env.project_id, AGENT, details["approval_request_id"])["summary"]
    )
    too_long = refused(
        lambda: env.svc.approval_request(
            env.project_id,
            AGENT,
            "recording_start",
            {"title": "x", "device_ids": ["synthetic-mic"], "max_seconds": 5 * 3600},
        )
    )
    assert too_long.code == "validation_error"


def test_an_agent_transport_never_starts_capture(env: Env, synthetic: Any) -> None:
    details = _recording_request(env, sensitivity="normal")
    approval_id = details["approval_request_id"]
    grant(env, approval_id)
    out = refused(
        lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id, backend=synthetic)
    )
    assert out.code == "policy_denied" and "where the owner sees it" in out.message

    async def steps(client: Client) -> Any:
        return await client.call_tool("approval_resume", {"approval_id": approval_id})

    over_mcp = _mcp(env, steps)
    assert over_mcp.is_error and "policy_denied" in over_mcp.content[0].text
    with running_hub(env.svc, env.project_id) as hub:
        token = hub.gate.new_agent_token()
        reply = hub.request(
            "POST",
            f"/api/v1/approvals/{approval_id}/resume",
            {},
            cookie=None,
            csrf=None,
            origin=None,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reply.status == 403
    assert env.svc.recording_list(env.project_id, OWNER, True)["sessions"] == []
    assert rows(env, "SELECT state FROM approval_requests")[0]["state"] == "granted"


def test_the_owner_starts_it_in_the_hub_bounded_by_the_plan(env: Env, synthetic: Any) -> None:
    details = _recording_request(env, sensitivity="normal", max_seconds=10)
    approval_id = details["approval_request_id"]
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        granted = hub.request(
            "POST",
            f"/api/v1/approvals/{approval_id}/grant",
            {"plan_sha256": details["plan_sha256"]},
        )
        assert granted.status == 200
        started = hub.request(
            "POST",
            f"/api/v1/approvals/{approval_id}/start",
            {"plan_sha256": details["plan_sha256"]},
        )
        assert started.status == 200, started.body
        session_id = started.json()["result"]["session_id"]
        assert started.json()["state"] == "used" and started.json()["result"]["max_seconds"] == 10
        deadline = time.monotonic() + 60
        state = ""
        while time.monotonic() < deadline:
            state = env.svc.recording_status(env.project_id, OWNER, session_id)["state"]
            if state == "imported":
                break
            time.sleep(0.2)
        # The hub process captured (visible banner) and stopped at the plan's maximum.
        assert state == "imported"
        status = env.svc.recording_status(env.project_id, OWNER, session_id)
        assert 10_000 <= status["elapsed_ms"] <= 12_000
        # Requester's rank (review F6/R5): the agent may read and control what it caused.
        assert env.svc.recording_status(env.project_id, AGENT, session_id)["state"] == "imported"


def test_the_start_window_closes_after_ten_minutes(
    env: Env, synthetic: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    details = _recording_request(env)
    approval_id = details["approval_request_id"]
    grant(env, approval_id)
    later = datetime.now(UTC) + timedelta(minutes=11)
    monkeypatch.setattr(approvals, "_now", lambda: later)
    out = refused(
        lambda: env.svc.approval_start_recording(
            env.project_id,
            approval_id,
            owner=OWNER_ACT,
            plan_sha256=details["plan_sha256"],
            backend=synthetic,
            start_capture=False,
        )
    )
    assert out.code == "policy_denied" and out.details["state"] == "start_window_passed"
    assert env.svc.recording_list(env.project_id, OWNER, True)["sessions"] == []


@pytest.mark.parametrize("sensitivity", ["normal", "restricted"])
def test_the_requester_stops_what_it_caused_and_the_owner_always_can(
    env: Env, synthetic: Any, sensitivity: str
) -> None:
    details = _recording_request(env, sensitivity=sensitivity, max_seconds=300)
    approval_id = details["approval_request_id"]
    grant(env, approval_id)
    started = env.svc.approval_start_recording(
        env.project_id,
        approval_id,
        owner=OWNER_ACT,
        plan_sha256=details["plan_sha256"],
        backend=synthetic,
        start_capture=False,
    )
    session = started["result"]
    assert session["consent_acknowledged"] is True
    import threading

    from prometheos.audio import capture

    realtime = capture.SyntheticBackend(speed=1.0)  # capture at real speed so a stop matters
    outcome: dict[str, Any] = {}

    def run() -> None:
        outcome["final"] = env.svc.recording_run(
            env.project_id, session["session_id"], realtime, max_seconds=300
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(1.5)
    stopped = env.svc.approval_stop(env.project_id, AGENT, approval_id)
    assert set(stopped) == {"approval_request_id", "session_id", "state"}
    worker.join(timeout=30)
    if worker.is_alive():  # the stop did not take: end the capture before failing
        env.svc.recording_control(env.project_id, OWNER, session["session_id"], "cancel")
        worker.join(timeout=30)
        pytest.fail("the requester's stop did not stop the recording")
    final = outcome["final"]
    assert final["state"] == "imported" and final["duration_ms"] < 60_000
    if sensitivity == "normal":  # the session is at the requester's rank (creator rule)
        assert env.svc.recording_status(env.project_id, AGENT, session["session_id"])
    # An unrelated request cannot be used to stop it.
    _, other = ask(env, settings_change(env))
    assert (
        refused(
            lambda: env.svc.approval_stop(
                env.project_id, AGENT, other.details["approval_request_id"]
            )
        ).code
        == "validation_error"
    )


# ----- F2: the owner reviews the full effect ----------------------------------------------------


def test_the_owner_reviews_the_full_push_including_restricted_files(tmp_path: Path) -> None:
    """Probe P1 of review cp10-2-a as a regression, end to end on a local bare remote."""
    from test_cp9_review_cp9a_r2 import _restrict_in_clone
    from test_cp9_sync import Linked, git

    linked = Linked(tmp_path)
    svc, pid, paper = linked.svc, linked.pid, linked.paper_id
    _restrict_in_clone(linked, "refs.bib")
    bib = linked.folder / "refs.bib"
    bib.write_text(bib.read_text("utf-8") + "% RESTRICTED-CANARY edit\n", "utf-8", newline="\n")
    svc.overleaf_commit(pid, OWNER, paper, "Owner commits the restricted bib")
    agent_preview = svc.overleaf_push_preview(pid, AGENT, paper)
    err = refused(
        lambda: svc.approval_request(
            pid, AGENT, "overleaf_push", {"paper_id": paper, "digest": agent_preview["digest"]}
        )
    )
    approval_id = err.details["approval_request_id"]
    agent_view = json.dumps(svc.approval_get(pid, AGENT, approval_id))
    assert "refs.bib" not in agent_view  # the agent's plan binds the owner view by digest only
    owner_view = svc.approval_owner_view(pid, approval_id)
    review = owner_view["preview"]
    assert review["matches_plan"] is True
    assert "refs.bib" in json.dumps(review["restricted_files"])
    grant(linked.env, approval_id)
    done = svc.approval_resume(pid, AGENT, approval_id)
    assert done["result"]["pushed"]
    assert "RESTRICTED-CANARY" in git(linked.remote.bare, "show", "master:refs.bib")
    # Run once: a second resume replays, the remote does not move again.
    head = git(linked.remote.bare, "rev-parse", "master")
    assert svc.approval_resume(pid, AGENT, approval_id)["replayed"] is True
    assert git(linked.remote.bare, "rev-parse", "master") == head


def test_a_push_that_changed_after_the_request_cannot_be_granted_or_run(tmp_path: Path) -> None:
    from test_cp9_sync import Linked

    linked = Linked(tmp_path)
    svc, pid, paper = linked.svc, linked.pid, linked.paper_id
    (linked.folder / "main.tex").write_text(
        (linked.folder / "main.tex").read_text("utf-8") + "% one\n", "utf-8", newline="\n"
    )
    svc.overleaf_commit(pid, OWNER, paper, "First change")
    preview = svc.overleaf_push_preview(pid, AGENT, paper)
    err = refused(
        lambda: svc.approval_request(
            pid, AGENT, "overleaf_push", {"paper_id": paper, "digest": preview["digest"]}
        )
    )
    approval_id = err.details["approval_request_id"]
    (linked.folder / "main.tex").write_text(
        (linked.folder / "main.tex").read_text("utf-8") + "% two\n", "utf-8", newline="\n"
    )
    svc.overleaf_commit(pid, OWNER, paper, "Second change after the request")
    assert svc.approval_owner_view(pid, approval_id)["preview"]["matches_plan"] is False
    shown = svc.approval_owner_view(pid, approval_id)
    # Round 3 (R2-4): the server refuses the grant itself, not only the hub button.
    late = refused(
        lambda: svc.approval_decide(
            pid, approval_id, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
        )
    )
    assert late.code == "revision_conflict"
    assert svc.approval_get(pid, AGENT, approval_id)["state"] == "pending"
    out = refused(lambda: svc.approval_resume(pid, AGENT, approval_id))
    assert out.code == "approval_required"


def test_the_owner_reviews_the_purge_closure(env: Env) -> None:
    hidden = create_entry(sensitivity="restricted", title="Hidden note")
    env.apply([hidden], caller=OWNER)
    plan = env.svc.purge_plan(env.project_id, [hidden["id"]], OWNER)
    err = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "retention_purge_commit", {"plan_id": plan["plan_id"]}
        )
    )
    approval_id = err.details["approval_request_id"]
    assert hidden["id"] not in json.dumps(env.svc.approval_get(env.project_id, AGENT, approval_id))
    review = env.svc.approval_owner_view(env.project_id, approval_id)["preview"]
    assert review["matches_plan"] is True and review["counts"]["closure"] >= 1
    assert hidden["id"] in review["owner_view"]["closure"]


def test_a_provider_configuration_shows_old_and_new_routing(tmp_path: Path) -> None:
    from provider_support import ProviderEnv, config_doc

    penv = ProviderEnv(tmp_path)
    penv.configure()
    moved = config_doc(endpoint="https://elsewhere.example.test/v1")
    err = refused(
        lambda: penv.svc.approval_request(
            penv.project_id,
            AGENT,
            "provider_configure",
            {"config": moved, "revision": penv.revision()},
        )
    )
    shown = penv.svc.approval_owner_view(penv.project_id, err.details["approval_request_id"])
    plan = shown["plan"]
    assert plan["before"]["endpoint_host"] == "api.example.test"
    assert plan["after"]["endpoint_host"] == "elsewhere.example.test"
    assert plan["credential_to_new_host"] is True
    assert any("WARNING" in line and "new host" in line for line in shown["summary"])
    assert "sk-CANARY" not in json.dumps(shown)


# ----- F3: a stale apply says the project changed ---------------------------------------------


def test_a_stale_apply_says_the_project_changed(env: Env) -> None:
    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    env.apply([create_entry()])
    out = refused(lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id))
    assert out.code == "revision_conflict" and "project changed" in out.message
    assert "new request" in out.message and out.details["approval_request_id"] == approval_id


# ----- F4: the agent token reaches every agent route; agent CLI preview ---------------------------


def test_the_inventory_catches_an_agent_route_the_token_cannot_reach() -> None:
    from prometheos import registry
    from prometheos.hub.app import AGENT_ROUTES
    from prometheos.registry import inventory

    found = inventory.surfaces()
    full = inventory.compare(registry.operations(), found, agent_routes=AGENT_ROUTES)
    assert full["forward_problems"] == []
    missing = inventory.compare(
        registry.operations(), found, agent_routes=AGENT_ROUTES - {"GET /api/v1/records"}
    )
    assert any(
        "agent route not reachable" in p and "/records" in p for p in missing["forward_problems"]
    )
    leaked = inventory.compare(
        registry.operations(),
        found,
        agent_routes=AGENT_ROUTES | {"POST /api/v1/approvals/{approval_id}/grant"},
    )
    assert any("owner-only route admits" in p for p in leaked["forward_problems"])


def test_an_agent_previews_a_transaction_from_the_cli(env: Env) -> None:
    from test_cp1_cli import Cli

    path = env.tmp / "req.json"
    path.write_bytes(request(env.project_id, settings_change(env), env.seq()))
    code, out, _ = Cli(env.tmp)(
        "decision", "preview", "--file", str(path), "--project", env.project_id
    )
    assert code == 0 and out["protected_effects"] == ["project settings change"]
    assert not rows(env, "SELECT id FROM approval_requests")


# ----- F5: grant and revoke answer their contracts ----------------------------------------------


def test_revoke_answers_its_receipt_on_http_and_cli(env: Env) -> None:
    from test_cp1_cli import Cli

    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        reply = hub.request(
            "POST", f"/api/v1/approvals/{approval_id}/revoke", {"approval_request_id": approval_id}
        )
        assert reply.status == 200
        receipt = CapabilityRevokeReceipt.model_validate(reply.json(), strict=True)
        assert receipt.revoked and receipt.state == "revoked"
    cap = env.svc.grant(env.project_id, operation="read_sensitive", digest=None, label="t")
    code, out, _ = Cli(env.tmp)(
        "decision", "revoke", cap.capability_id, "--project", env.project_id
    )
    assert code == 0 and CapabilityRevokeReceipt.model_validate(out, strict=True).revoked


# ----- F6: at-most-once, base rechecks and the requester's rank ---------------------------------


def _home_kind(env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str) -> Any:
    """Request and grant one home-level kind; return (approval_id, effects list, rebase)."""
    from prometheos.app import texengine
    from prometheos.audio import models

    effects: list[str] = []
    monkeypatch.setattr(
        texengine, "install", lambda home, d, archive=None: effects.append("tex") or {}
    )
    monkeypatch.setattr(
        models,
        "install",
        lambda home, k, d, progress=None: effects.append("install") or {"model": k},
    )
    monkeypatch.setattr(
        models, "remove", lambda home, k: effects.append("remove") or {"removed": True}
    )
    if kind == "tex_engine_install":
        empty_engine = env.home / "empty-synthetic-engine"
        empty_engine.mkdir()
        monkeypatch.setenv(texengine.ENGINE_DIR_ENV, str(empty_engine))
        params: dict[str, Any] = {"digest": env.svc.tex_engine_plan()["plan_digest"]}
    elif kind == "audio_models_install":
        params = {
            "model": "faster-whisper-base",
            "plan_sha256": env.svc.audio_models_plan()["plan_sha256"],
        }
    elif kind == "audio_models_remove":
        params = {"model": "faster-whisper-base"}
    else:
        from cp4_support import import_package, write_package

        import_package(env.svc, write_package(tmp_path / "pkg"))
        env.svc.skill_revoke("synthetic-evidence@1.0.0")
        params = {"ref": "synthetic-evidence@1.0.0"}
        real = env.svc.skill_trust

        def trust(ref: str, *, authority: Any) -> Any:
            effects.append("trust")
            return real(ref, authority=authority)

        monkeypatch.setattr(env.svc, "skill_trust", trust)
    err = refused(lambda: env.svc.approval_request(env.project_id, AGENT, kind, params))
    grant(env, err.details["approval_request_id"])
    return err.details["approval_request_id"], effects


HOME_KINDS = ["tex_engine_install", "audio_models_install", "audio_models_remove", "skill_trust"]


@pytest.mark.parametrize("kind", HOME_KINDS)
def test_home_level_effects_happen_at_most_once(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    approval_id, effects = _home_kind(env, monkeypatch, tmp_path, kind)
    env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert env.svc.approval_resume(env.project_id, AGENT, approval_id)["replayed"] is True
    assert len(effects) == 1


@pytest.mark.parametrize("kind", HOME_KINDS)
def test_no_home_level_effect_without_a_successful_spend(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    approval_id, effects = _home_kind(env, monkeypatch, tmp_path, kind)

    def refuse(*args: Any, **kwargs: Any) -> str:
        raise PrometheosError("policy_denied", "the grant could not be spent", {})

    monkeypatch.setattr(approvals, "spend", refuse)
    assert (
        refused(lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id)).code
        == "policy_denied"
    )
    assert effects == []


@pytest.mark.parametrize("kind", ["audio_models_install", "audio_models_remove", "skill_trust"])
def test_a_changed_home_level_base_refuses_the_resume(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    approval_id, effects = _home_kind(env, monkeypatch, tmp_path, kind)
    service = type(env.svc)
    if kind == "audio_models_install":
        changed = dict(env.svc.audio_models_plan(), plan_sha256="e" * 64)
        monkeypatch.setattr(
            service, "audio_models_plan", lambda self, key="", redact_paths=False: changed
        )
    elif kind == "audio_models_remove":
        status = {
            "models": [{"model": "faster-whisper-base", "installed": True, "revision": "other"}]
        }
        monkeypatch.setattr(
            service, "audio_models_status", lambda self, verify=False, redact_paths=False: status
        )
    else:
        env.svc.skill_trust("synthetic-evidence@1.0.0", authority=OWNER_ACT)  # status moved on
        effects.clear()
    out = refused(lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id))
    assert out.code == "revision_conflict" and effects == []
    assert rows(env, "SELECT state FROM approval_requests")[0]["state"] == "granted"


def test_job_resolve_rechecks_visibility_at_the_requesters_rank(env: Env) -> None:
    from test_jobs_followups import make_unknown

    job_id, due = make_unknown(env)
    params = {"job_id": job_id, "due_utc": due, "resolution": "failed", "reason": "seen"}
    err = refused(lambda: env.svc.approval_request(env.project_id, AGENT, "job_resolve", params))
    grant(env, err.details["approval_request_id"])
    # The job becomes restricted after the request (jobs change only through their own
    # commands, so the store is edited directly for this test).
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        conn.execute("UPDATE records SET sensitivity = 'restricted' WHERE id = ?", (job_id,))
        conn.commit()
    out = refused(
        lambda: env.svc.approval_resume(env.project_id, AGENT, err.details["approval_request_id"])
    )
    assert out.code == "not_found"


# ----- F7: no home paths over MCP ------------------------------------------------------------


def test_a_model_install_resumed_over_mcp_reveals_no_home_path(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.audio import models

    monkeypatch.delenv("PROMETHEOS_MODELS_DIR", raising=False)
    monkeypatch.setattr(models, "installed", lambda home, spec: True)
    plan = env.svc.audio_models_plan("faster-whisper-base")
    err = refused(
        lambda: env.svc.approval_request(
            env.project_id,
            AGENT,
            "audio_models_install",
            {"model": "faster-whisper-base", "plan_sha256": plan["plan_sha256"]},
        )
    )
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)

    async def steps(client: Client) -> Any:
        return await client.call_tool("approval_resume", {"approval_id": approval_id})

    reply = _mcp(env, steps)
    home = str(env.home)
    escaped = json.dumps(home)[1:-1]  # how the path would appear inside JSON text
    stored = env.svc.approval_get(env.project_id, AGENT, approval_id)
    for document in (reply.structured_content, stored):
        text = json.dumps(document)
        assert escaped not in text and home.replace("\\", "/") not in text
    assert "<PrometheOS models>" in json.dumps(reply.structured_content)


# ----- F8: the CLI shows the whole plan, escaped ---------------------------------------------


def test_the_cli_plan_block_escapes_format_characters_and_marks_truncation() -> None:
    from prometheos.cli import PLAN_SHOWN_CHARS, _escaped_block

    hostile = {"title": "Lab\u202egnihton\u200b\nEffects: none", "pad": "x"}
    block = _escaped_block("Plan", hostile)
    assert "\u202e" not in block and "\u200b" not in block and "\\u202e" in block
    assert block.isascii()
    big = _escaped_block("Plan", {"pad": "y" * (PLAN_SHOWN_CHARS + 50)})
    assert "TRUNCATED" in big and "more characters]" in big


# ----- F9, F10, F11, F14, F15 ---------------------------------------------------------------------


def test_the_owner_queue_keeps_every_pending_request(env: Env) -> None:
    first = _recording_request(env, title="Kept pending")
    for n in range(60):
        details = _recording_request(env, title=f"churn {n}")
        env.svc.approval_withdraw(env.project_id, AGENT, details["approval_request_id"])
    queue = env.svc.approval_queue(env.project_id)["approvals"]
    assert first["approval_request_id"] in {a["approval_request_id"] for a in queue}
    assert len([a for a in queue if a["state"] == "withdrawn"]) == approvals.RECENT_ENDED
    assert all("preview" not in a for a in queue)  # F12: no dry runs in a listing


def test_a_retry_of_an_ended_request_says_it_ended(env: Env) -> None:
    raw, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    shown = env.svc.approval_owner_view(env.project_id, approval_id)
    env.svc.approval_decide(
        env.project_id, approval_id, grant=False, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )
    again = refused(lambda: env.svc.apply(env.project_id, raw, AGENT))
    assert again.code == "policy_denied" and again.details["state"] == "denied"
    assert "denied" in again.message


def test_agent_token_commits_are_audited_as_the_agent_transport(env: Env) -> None:
    entry = create_entry(title="By the token")
    with running_hub(env.svc, env.project_id) as hub:
        token = hub.gate.new_agent_token()
        body = json.loads(request(env.project_id, [entry], env.seq()))
        reply = hub.request(
            "POST",
            "/api/v1/transactions",
            body,
            cookie=None,
            csrf=None,
            origin=None,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reply.status == 200, reply.body
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        actors = {
            r[0]
            for r in conn.execute("SELECT actor FROM audit WHERE record_id = ?", (entry["id"],))
        }
    assert actors == {"http-agent"}


def test_owner_handlers_refuse_an_agent_even_if_the_allow_list_slips(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.hub import app as hub_app

    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    slipped = hub_app.AGENT_ROUTES | {
        "POST /api/v1/approvals/{approval_id}/grant",
        "POST /api/v1/approvals/{approval_id}/deny",
        "POST /api/v1/approvals/{approval_id}/revoke",
        "POST /api/v1/approvals/{approval_id}/start",
    }
    monkeypatch.setattr(hub_app, "AGENT_ROUTES", slipped)
    with running_hub(env.svc, env.project_id) as hub:
        token = hub.gate.new_agent_token()
        for verb, body in (
            ("grant", {"plan_sha256": err.details["plan_sha256"]}),
            ("deny", {"plan_sha256": err.details["plan_sha256"]}),
            ("revoke", {"approval_request_id": approval_id}),
            ("start", {"plan_sha256": err.details["plan_sha256"]}),
        ):
            reply = hub.request(
                "POST",
                f"/api/v1/approvals/{approval_id}/{verb}",
                body,
                cookie=None,
                csrf=None,
                origin=None,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert reply.status == 403 and reply.json()["error"]["code"] == "policy_denied", verb
    assert rows(env, "SELECT state FROM approval_requests")[0]["state"] == "pending"


def test_agent_tokens_expire() -> None:
    from prometheos.hub.security import AGENT_TOKEN_TTL, Gate

    now = [1000.0]
    gate = Gate(port=1, clock=lambda: now[0])
    token = gate.new_agent_token()
    assert gate.agent_ok(f"Bearer {token}")
    now[0] += AGENT_TOKEN_TTL + 1
    assert not gate.agent_ok(f"Bearer {token}")
    now[0] -= AGENT_TOKEN_TTL + 1  # an expired token is gone for good
    assert not gate.agent_ok(f"Bearer {token}")


def test_the_agent_token_file_is_never_overwritten(env: Env) -> None:
    import os
    import subprocess
    import sys

    existing = env.tmp / "token.txt"
    existing.write_text("keep me\n", "utf-8")
    environ = {k: v for k, v in os.environ.items() if not k.startswith("PROMETHEOS_")}
    environ["PROMETHEOS_HOME"] = str(env.home)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "prometheos",
            "hub",
            "serve",
            "--agent-token-file",
            str(existing),
            "--project",
            env.project_id,
        ],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0 and "agent token file" in done.stderr
    assert existing.read_text("utf-8") == "keep me\n"
