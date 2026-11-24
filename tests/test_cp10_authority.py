"""CP10.2 authority (C10-AUTH, C10-PARITY denied requests; decision 0016).

A request that needs authority the caller lacks stores its exact canonical plan and answers a
typed ``approval_required``; nothing commits. The owner grants or denies it in an owner
channel; a grant is a single-use exact-plan capability held by the server. ``approval resume``
executes the stored plan once, in one transaction with the capability use, after rechecking
expiry, revocation and the plan's base. Denied, expired, withdrawn and revoked requests never
run; a changed plan needs a new request; requests are bound to their project and caller rank.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env, create_entry, request
from hub_support import running_hub
from mcp import Client
from test_cp1_cli import Cli

from prometheos.app import approvals
from prometheos.app.authority import OwnerAct
from prometheos.domain.models import (
    ApprovalRequiredDetails,
    ApprovalView,
    CapabilityGrantReceipt,
    PrometheosError,
)
from prometheos.mcp_adapter import build_server
from prometheos.store import capabilities


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def settings_change(env: Env, objective: str = "Changed objective.") -> list[dict[str, Any]]:
    """A transaction with one protected effect ("project settings change")."""
    project = env.svc.get(env.project_id, OWNER, env.project_id, None)
    settings = dict(project["body"]) | {"current_objective": objective}
    return [{"op": "project_update", "base_rev": project["rev"], "settings": settings}]


def refused(fn: Any) -> PrometheosError:
    with pytest.raises(PrometheosError) as err:
        fn()
    return err.value


def ask(env: Env, ops: list[dict[str, Any]], caller: Any = AGENT, rid: str | None = None) -> Any:
    """Submit a protected transaction without a capability; return (raw, error)."""
    raw = request(env.project_id, ops, env.seq(), rid)
    return raw, refused(lambda: env.svc.apply(env.project_id, raw, caller))


def rows(env: Env, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def grant(env: Env, approval_id: str) -> dict[str, Any]:
    shown = env.svc.approval_owner_view(env.project_id, approval_id)
    return env.svc.approval_decide(
        env.project_id, approval_id, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )


def deny(env: Env, approval_id: str) -> dict[str, Any]:
    shown = env.svc.approval_owner_view(env.project_id, approval_id)
    return env.svc.approval_decide(
        env.project_id, approval_id, grant=False, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )


def objective(env: Env) -> str:
    return str(env.svc.project_info(env.project_id, OWNER)["settings"]["current_objective"])


# ----- the typed answer and the stored plan ----------------------------------------------------


def test_a_protected_request_stores_its_exact_plan_and_commits_nothing(env: Env) -> None:
    seq = env.seq()
    raw, err = ask(env, settings_change(env))
    assert err.code == "approval_required" and err.exit_code == 4
    details = ApprovalRequiredDetails.model_validate(err.details, strict=True)
    assert env.seq() == seq and objective(env) != "Changed objective."
    # The digest is exactly what `decision review` would show for this request.
    preview = env.svc.review_request(env.project_id, raw, OWNER)
    assert details.plan_sha256 == preview["digest"]
    assert details.required_scope == "exact_request"
    assert details.owner_route.hub == f"/approvals/{details.approval_request_id}"
    assert details.owner_route.cli.endswith(f"--request {details.approval_request_id}")
    (row,) = rows(env, "SELECT * FROM approval_requests")
    stored = json.loads(row["plan"])
    assert stored == {k: v for k, v in json.loads(raw).items() if k != "request_id"}
    assert row["state"] == "pending" and row["caller_rank"] == 0
    assert row["request_id"] == json.loads(raw)["request_id"]
    assert json.loads(row["summary"]) == ["project settings change"]
    assert row["expires_at"] == details.expires_at and row["capability_id"] is None
    # The HTTP and CLI exit mapping of the same envelope.
    assert err.to_response().model_dump(mode="json")["error"]["details"] == err.details


def test_an_identical_retry_returns_the_same_request_and_reuse_conflicts(env: Env) -> None:
    raw, first = ask(env, settings_change(env))
    again = refused(lambda: env.svc.apply(env.project_id, raw, AGENT))
    assert again.details == first.details
    assert len(rows(env, "SELECT id FROM approval_requests")) == 1
    rid = json.loads(raw)["request_id"]
    # The same request ID for another plan, or at another rank, is a key conflict.
    other = request(env.project_id, settings_change(env, "Another"), env.seq(), rid)
    assert (
        refused(lambda: env.svc.apply(env.project_id, other, AGENT)).code == "request_key_conflict"
    )
    owner_retry = refused(lambda: env.svc.apply(env.project_id, raw, OWNER))
    assert owner_retry.code == "request_key_conflict"
    assert len(rows(env, "SELECT id FROM approval_requests")) == 1


# ----- grant, resume once ----------------------------------------------------------------------


def test_a_granted_plan_runs_exactly_once_with_the_server_held_grant(env: Env) -> None:
    raw, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    seq = env.seq()
    granted = grant(env, approval_id)
    assert granted["state"] == "granted" and "capability_id" not in granted
    (row,) = rows(env, "SELECT capability_id FROM approval_requests")
    (cap,) = rows(env, "SELECT * FROM capabilities WHERE id = ?", (row["capability_id"],))
    assert cap["operation"] == "exact_request" and cap["uses"] == 0 and cap["max_uses"] == 1
    assert cap["digest"] == err.details["plan_sha256"]
    first = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert first["replayed"] is False and first["result"]["ok"] is True
    assert env.seq() == seq + 1 and objective(env) == "Changed objective."
    second = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert second["replayed"] is True and second["result"] == first["result"]
    assert env.seq() == seq + 1
    (cap,) = rows(env, "SELECT uses FROM capabilities WHERE id = ?", (row["capability_id"],))
    assert cap["uses"] == 1
    used = env.svc.approval_get(env.project_id, AGENT, approval_id)
    assert (
        used["state"] == "used"
        and used["used_seq"] == seq + 1
        and used["result"] == first["result"]
    )
    # The agent's own retry of the original request replays the committed result.
    assert env.svc.apply(env.project_id, raw, AGENT)["replayed"] is True
    audit = rows(env, "SELECT operation FROM audit WHERE seq = ?", (seq + 1,))
    assert f"owner_decision:{row['capability_id']}" in {a["operation"] for a in audit}


def test_a_pending_request_does_not_run_and_answers_approval_required_again(env: Env) -> None:
    _, err = ask(env, settings_change(env))
    out = refused(
        lambda: env.svc.approval_resume(env.project_id, AGENT, err.details["approval_request_id"])
    )
    assert out.code == "approval_required" and out.details == err.details


@pytest.mark.parametrize("ending", ["denied", "withdrawn", "revoked", "expired"])
def test_denied_withdrawn_revoked_and_expired_requests_never_run(
    env: Env, ending: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = env.seq()
    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    if ending == "denied":
        deny(env, approval_id)
    else:
        grant(env, approval_id)
        if ending == "withdrawn":
            env.svc.approval_withdraw(env.project_id, AGENT, approval_id)
        elif ending == "revoked":
            env.svc.approval_revoke(env.project_id, approval_id, owner=OWNER_ACT)
        else:
            later = datetime.now(UTC) + timedelta(days=2)
            monkeypatch.setattr(approvals, "_now", lambda: later)
            monkeypatch.setattr(
                capabilities, "utc_now", lambda: later.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            )
    out = refused(lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id))
    assert out.code == "policy_denied" and out.details["state"] == ending
    assert env.seq() == seq and objective(env) != "Changed objective."
    (row,) = rows(env, "SELECT * FROM approval_requests")
    assert row["state"] == ending and row["closed_at"] is not None
    if row["capability_id"] is not None:  # a grant that ended is unusable in the same commit
        (cap,) = rows(
            env, "SELECT revoked_at, uses FROM capabilities WHERE id = ?", (row["capability_id"],)
        )
        assert cap["revoked_at"] is not None and cap["uses"] == 0
        assert row["decided_at"] is not None  # the decision is kept (decision 0015 R2)


def test_a_pending_request_expires_without_a_decision(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ask(env, settings_change(env))
    later = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr(approvals, "_now", lambda: later)
    listed = env.svc.approval_list(env.project_id, AGENT)["approvals"]
    assert [a["state"] for a in listed] == ["expired"] and listed[0]["decided_at"] is None
    shown = listed[0]
    late = refused(
        lambda: env.svc.approval_decide(
            env.project_id,
            shown["approval_request_id"],
            grant=True,
            plan_sha256=shown["plan_sha256"],
            owner=OWNER_ACT,
        )
    )
    assert late.code == "revision_conflict"


def test_an_edited_plan_is_a_new_request_and_the_granted_one_stays_exact(env: Env) -> None:
    _, err = ask(env, settings_change(env, "Granted text"))
    granted_id = err.details["approval_request_id"]
    grant(env, granted_id)
    _, edited = ask(env, settings_change(env, "Edited text"))
    assert edited.details["approval_request_id"] != granted_id
    assert edited.details["plan_sha256"] != err.details["plan_sha256"]
    assert (
        refused(
            lambda: env.svc.approval_resume(
                env.project_id, AGENT, edited.details["approval_request_id"]
            )
        ).code
        == "approval_required"
    )
    env.svc.approval_resume(env.project_id, AGENT, granted_id)
    assert objective(env) == "Granted text"


def test_a_changed_base_refuses_the_resume_and_keeps_the_grant_unused(env: Env) -> None:
    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    env.apply([create_entry()])  # the project moves on: the plan's base is stale
    seq = env.seq()
    out = refused(lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id))
    assert out.code == "revision_conflict" and env.seq() == seq
    (row,) = rows(env, "SELECT state, capability_id FROM approval_requests")
    (cap,) = rows(env, "SELECT uses FROM capabilities WHERE id = ?", (row["capability_id"],))
    assert row["state"] == "granted" and cap["uses"] == 0


def test_the_capability_use_and_the_mutation_commit_together(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the grant is consumed, before COMMIT, leaves both unchanged."""
    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    seq = env.seq()
    from prometheos.store import project as project_mod

    def crash(name: str) -> None:
        if name == "commit_before":  # after the handler (grant consumed), before COMMIT
            raise RuntimeError("injected fault")

    monkeypatch.setattr(project_mod, "fault_point", crash)
    with pytest.raises(RuntimeError):
        env.svc.approval_resume(env.project_id, AGENT, approval_id)
    monkeypatch.setattr(project_mod, "fault_point", lambda name: None)
    (row,) = rows(env, "SELECT state, capability_id FROM approval_requests")
    (cap,) = rows(env, "SELECT uses FROM capabilities WHERE id = ?", (row["capability_id"],))
    assert row["state"] == "granted" and cap["uses"] == 0 and env.seq() == seq
    env.svc.approval_resume(env.project_id, AGENT, approval_id)  # still usable, once
    assert env.seq() == seq + 1


# ----- caller rank, project scope and owner channels ------------------------------------------


def test_requests_are_bound_to_the_caller_rank(env: Env) -> None:
    _, agent_err = ask(env, settings_change(env, "agent plan"))
    agent_id = agent_err.details["approval_request_id"]
    _, owner_err = ask(env, settings_change(env, "owner plan"), caller=OWNER)
    owner_id = owner_err.details["approval_request_id"]
    # An owner-scope request reads as absent to an agent (its plan may name hidden records).
    listed = {
        a["approval_request_id"] for a in env.svc.approval_list(env.project_id, AGENT)["approvals"]
    }
    assert listed == {agent_id}
    assert (
        refused(lambda: env.svc.approval_get(env.project_id, AGENT, owner_id)).code == "not_found"
    )
    grant(env, agent_id)
    grant(env, owner_id)
    assert (
        refused(lambda: env.svc.approval_resume(env.project_id, AGENT, owner_id)).code
        == "not_found"
    )
    # Another rank cannot resume or withdraw it (the replay rule of decision 0010).
    assert (
        refused(lambda: env.svc.approval_resume(env.project_id, OWNER, agent_id)).code
        == "request_key_conflict"
    )
    assert (
        refused(lambda: env.svc.approval_withdraw(env.project_id, OWNER, agent_id)).code
        == "request_key_conflict"
    )
    out = env.svc.approval_resume(env.project_id, AGENT, agent_id)
    assert out["result"]["ok"] is True


def test_requests_are_bound_to_their_project(env: Env, tmp_path: Path) -> None:
    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    other = env.svc.init_project(
        title="Other", objective="Other.", timezone="UTC", roots={"research": str(env.research)}
    )["project_id"]
    assert refused(lambda: env.svc.approval_resume(other, AGENT, approval_id)).code == "not_found"
    assert refused(lambda: env.svc.approval_get(other, AGENT, approval_id)).code == "not_found"


def test_owner_channel_operations_need_an_owner_act(env: Env) -> None:
    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    digest = err.details["plan_sha256"]
    for pretender in (None, AGENT, OWNER, "owner", {"channel": "hub-owner"}):
        out = refused(
            lambda p=pretender: env.svc.approval_decide(
                env.project_id, approval_id, grant=True, plan_sha256=digest, owner=p
            )
        )
        assert out.code == "policy_denied"
        assert (
            refused(
                lambda p=pretender: env.svc.approval_revoke(env.project_id, approval_id, owner=p)
            ).code
            == "policy_denied"
        )
    assert rows(env, "SELECT state FROM approval_requests")[0]["state"] == "pending"
    # The owner decides exactly the plan shown: another digest is refused.
    wrong = refused(
        lambda: env.svc.approval_decide(
            env.project_id, approval_id, grant=True, plan_sha256="0" * 64, owner=OWNER_ACT
        )
    )
    assert wrong.code == "revision_conflict"


def test_a_server_held_grant_works_only_for_its_own_request(env: Env) -> None:
    _, one = ask(env, settings_change(env, "one"))
    _, two = ask(env, settings_change(env, "two"))
    grant(env, one.details["approval_request_id"])
    (row,) = rows(env, "SELECT capability_id FROM approval_requests WHERE state = 'granted'")
    store = env.svc.open(env.project_id)
    forged = capabilities.HeldGrant(row["capability_id"], two.details["approval_request_id"])
    out = refused(
        lambda: approvals.spend(store, forged, "exact_request", two.details["plan_sha256"])
    )
    assert out.code == "policy_denied"
    # A held grant never matches as a secret string, and a secret string never matches by ID.
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        conn.row_factory = sqlite3.Row
        for secret in (row["capability_id"], f"held:{row['capability_id']}"):
            with pytest.raises(PrometheosError):
                capabilities.verify(
                    conn,
                    secret=secret,
                    project_id=env.project_id,
                    operation="exact_request",
                    digest=one.details["plan_sha256"],
                )


def test_no_view_carries_a_secret_or_the_capability_id(env: Env) -> None:
    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    (row,) = rows(env, "SELECT capability_id FROM approval_requests")
    documents = [
        env.svc.approval_list(env.project_id, AGENT),
        env.svc.approval_get(env.project_id, AGENT, approval_id),
        env.svc.approval_queue(env.project_id),
        env.svc.approval_owner_view(env.project_id, approval_id),
        err.to_response().model_dump(mode="json"),
    ]
    for document in documents:
        text = json.dumps(document)
        assert "pcap_" not in text and row["capability_id"] not in text
    for item in documents[0]["approvals"]:
        ApprovalView.model_validate(item, strict=True)


# ----- the grant operations an agent requests ---------------------------------------------------


def test_authority_gap_operations_refuse_without_an_owner_act(env: Env) -> None:
    from prometheos.audio import recorder

    calls = [
        lambda: env.svc.tex_engine_install("0" * 64, authority=None),
        lambda: env.svc.audio_models_install("faster-whisper-base", "0" * 64, authority=OWNER),
        lambda: env.svc.audio_models_remove("faster-whisper-base", authority=AGENT),
        lambda: env.svc.skill_trust("anything", authority="owner"),
        lambda: env.svc.migrate(env.project_id, authority=None),
        lambda: env.svc.recording_start(
            env.project_id,
            OWNER,
            recorder.SessionRequest("T", ("synthetic-mic",), "normal", 30, True),
            None,
            authority=None,
        ),
    ]
    for call in calls:
        out = refused(call)
        assert out.code == "policy_denied" and "owner decision" in out.message


def test_tex_engine_install_is_requested_granted_and_resumed_once(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app import texengine

    empty_engine = env.home / "empty-synthetic-engine"
    empty_engine.mkdir()
    # This install-call control requires absence; keep an explicit disposable override.
    # The approved staged engine remains untouched and real-engine tests retain it.
    monkeypatch.setenv(texengine.ENGINE_DIR_ENV, str(empty_engine))
    installed: list[str] = []

    def fake_install(home: Path, digest: str, archive: Any = None) -> dict[str, Any]:
        installed.append(digest)
        return {"state": "installed", "location": str(home / "tools")}

    monkeypatch.setattr(texengine, "install", fake_install)
    plan = env.svc.tex_engine_plan(redact=True)
    stale = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "tex_engine_install", {"digest": "1" * 64}
        )
    )
    assert stale.code == "revision_conflict" and not rows(env, "SELECT id FROM approval_requests")
    err = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "tex_engine_install", {"digest": plan["plan_digest"]}
        )
    )
    assert err.code == "approval_required"
    approval_id = err.details["approval_request_id"]
    stored = env.svc.approval_get(env.project_id, AGENT, approval_id)["plan"]
    assert stored["plan_digest"] == plan["plan_digest"] and str(env.home) not in json.dumps(stored)
    grant(env, approval_id)
    out = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert out["result"]["state"] == "installed" and installed == [plan["plan_digest"]]
    assert env.svc.approval_resume(env.project_id, AGENT, approval_id)["replayed"] is True
    assert installed == [plan["plan_digest"]]


def test_a_changed_install_plan_refuses_the_resume_before_the_grant_is_spent(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app import texengine

    installed: list[str] = []
    monkeypatch.setattr(
        texengine, "install", lambda home, digest, archive=None: installed.append(digest) or {}
    )
    plan = env.svc.tex_engine_plan(redact=True)
    err = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "tex_engine_install", {"digest": plan["plan_digest"]}
        )
    )
    grant(env, err.details["approval_request_id"])
    changed = dict(env.svc.tex_engine_plan(), plan_digest="f" * 64)
    monkeypatch.setattr(type(env.svc), "tex_engine_plan", lambda self, redact=False: changed)
    out = refused(
        lambda: env.svc.approval_resume(env.project_id, AGENT, err.details["approval_request_id"])
    )
    assert out.code == "revision_conflict" and installed == []
    (row,) = rows(env, "SELECT state, capability_id FROM approval_requests")
    (cap,) = rows(env, "SELECT uses FROM capabilities WHERE id = ?", (row["capability_id"],))
    assert row["state"] == "granted" and cap["uses"] == 0


def test_model_install_and_remove_are_requested_and_resumed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.audio import models

    done: list[tuple[str, str]] = []
    monkeypatch.setattr(
        models,
        "install",
        lambda home, key, digest, progress=None: done.append(("install", key)) or {"model": key},
    )
    monkeypatch.setattr(
        models, "remove", lambda home, key: done.append(("remove", key)) or {"removed": True}
    )
    plan = env.svc.audio_models_plan("faster-whisper-base")
    for operation, params in (
        (
            "audio_models_install",
            {"model": "faster-whisper-base", "plan_sha256": plan["plan_sha256"]},
        ),
        ("audio_models_remove", {"model": "faster-whisper-base"}),
    ):
        before = list(done)
        err = refused(
            lambda o=operation, p=params: env.svc.approval_request(env.project_id, AGENT, o, p)
        )
        assert err.code == "approval_required" and done == before  # nothing ran yet
        grant(env, err.details["approval_request_id"])
        env.svc.approval_resume(env.project_id, AGENT, err.details["approval_request_id"])
    assert done == [("install", "faster-whisper-base"), ("remove", "faster-whisper-base")]


def test_skill_trust_is_requested_and_granted_in_the_queue(env: Env, tmp_path: Path) -> None:
    from cp4_support import import_package, write_package

    import_package(env.svc, write_package(tmp_path / "pkg"))
    ref = "synthetic-evidence@1.0.0"
    env.svc.skill_revoke(ref)
    err = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "skill_trust", {"ref": ref})
    )
    assert err.code == "approval_required"
    assert env.svc.catalog.get(ref)["status"] == "revoked"
    grant(env, err.details["approval_request_id"])
    out = env.svc.approval_resume(env.project_id, AGENT, err.details["approval_request_id"])
    assert env.svc.catalog.get(ref)["status"] != "revoked", out


def test_purge_commit_is_requested_by_plan_id_without_revealing_the_closure(env: Env) -> None:
    entry = create_entry(sensitivity="restricted", title="Hidden")
    env.apply([entry], caller=OWNER)
    plan = env.svc.purge_plan(env.project_id, [entry["id"]], OWNER)
    err = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "retention_purge_commit", {"plan_id": plan["plan_id"]}
        )
    )
    shown = env.svc.approval_get(env.project_id, AGENT, err.details["approval_request_id"])
    assert entry["id"] not in json.dumps(shown) and shown["required_scope"] == "purge"
    grant(env, err.details["approval_request_id"])
    out = env.svc.approval_resume(env.project_id, AGENT, err.details["approval_request_id"])
    receipt = out["result"]["purge_receipt"]
    assert receipt["records_purged"] >= 1
    used = env.svc.approval_get(env.project_id, AGENT, err.details["approval_request_id"])
    assert used["used_seq"] == receipt["project_seq"]
    tombstone = env.svc.get(env.project_id, OWNER, entry["id"], None)
    assert tombstone["body"].get("purged") is True and "Hidden" not in json.dumps(tombstone)


def test_job_resolve_is_requested_and_granted_in_the_queue(env: Env) -> None:
    """Review 0014 R2-L1: a non-interactive route to an exact_request grant for job resolve."""
    from test_jobs_followups import make_unknown, slot_rows

    job_id, due = make_unknown(env)
    params = {
        "job_id": job_id,
        "due_utc": due,
        "resolution": "failed",
        "reason": "provider console",
    }
    err = refused(lambda: env.svc.approval_request(env.project_id, AGENT, "job_resolve", params))
    assert err.code == "approval_required"
    assert next(s for s in slot_rows(env, job_id) if s["due_utc"] == due)["state"] == "unknown"
    grant(env, err.details["approval_request_id"])
    (row,) = rows(env, "SELECT capability_id FROM approval_requests")
    (cap,) = rows(env, "SELECT digest FROM capabilities WHERE id = ?", (row["capability_id"],))
    assert cap["digest"] == env.svc.job_resolve_digest(
        env.project_id, job_id, due, "failed", "provider console"
    )
    out = env.svc.approval_resume(env.project_id, AGENT, err.details["approval_request_id"])
    assert out["result"]["state"] == "failed"
    assert next(s for s in slot_rows(env, job_id) if s["due_utc"] == due)["state"] == "failed"
    # A slot that is no longer unknown cannot be requested again.
    again = refused(lambda: env.svc.approval_request(env.project_id, AGENT, "job_resolve", params))
    assert again.code == "validation_error"


def test_provider_configure_and_run_are_requested_and_resumed(tmp_path: Path) -> None:
    from provider_support import ProviderEnv, completion, config_doc

    penv = ProviderEnv(tmp_path)
    # Egress expansion: from no AI to a remote route is an owner decision.
    configure = {"config": config_doc(), "revision": penv.revision()}
    err = refused(
        lambda: penv.svc.approval_request(penv.project_id, AGENT, "provider_configure", configure)
    )
    assert (
        err.code == "approval_required"
        and penv.svc.provider_status(penv.project_id, AGENT)["policy"] == "no-ai"
    )
    assert "sk-CANARY" not in json.dumps(
        penv.svc.approval_get(penv.project_id, AGENT, err.details["approval_request_id"])
    )
    grant(penv, err.details["approval_request_id"])
    penv.svc.approval_resume(penv.project_id, AGENT, err.details["approval_request_id"])
    assert penv.svc.provider_status(penv.project_id, AGENT)["policy"] == "remote-allowed"
    from provider_support import REF, SECRET

    digest = penv.svc.provider_secret_digest("set", REF)
    cap = penv.svc.grant(
        penv.project_id,
        operation="provider_configure",
        digest=digest,
        label="SYNTHETIC-TEST",
        synthetic=True,
    )
    penv.svc.provider_secret_set(penv.project_id, REF, SECRET, cap.secret)
    shown = penv.preview()
    transport = penv.script(completion(penv.items_reply(3)))
    params = {
        "purpose": "meeting_extract",
        "meeting_id": penv.meeting["meeting_id"],
        "digest": shown["digest"],
    }
    run = refused(lambda: penv.svc.approval_request(penv.project_id, AGENT, "provider_run", params))
    assert run.code == "approval_required" and transport.requests == []
    grant(penv, run.details["approval_request_id"])
    out = penv.svc.approval_resume(penv.project_id, AGENT, run.details["approval_request_id"])
    assert len(transport.requests) == 1 and out["result"]
    assert penv.svc.approval_resume(penv.project_id, AGENT, run.details["approval_request_id"])[
        "replayed"
    ]
    assert len(transport.requests) == 1
    # Credential references only: no tool output carries the stored value.
    for document in (
        penv.svc.provider_status(penv.project_id, AGENT),
        penv.svc.provider_status(penv.project_id, OWNER),
        penv.svc.capabilities(penv.project_id),
        penv.svc.approval_list(penv.project_id, AGENT),
        out,
    ):
        assert SECRET not in json.dumps(document)


# ----- transports: MCP, HTTP agent token, CLI --------------------------------------------------


def _mcp(env: Env, steps: Any) -> Any:
    async def run() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await steps(client)

    return anyio.run(run)


def _tool_error(result: Any) -> dict[str, Any]:
    assert result.is_error
    text = result.content[0].text
    error: dict[str, Any] = json.loads(text[text.index("{") :])["error"]
    return error


def test_mcp_offers_requests_and_never_owner_decisions(env: Env) -> None:
    async def steps(client: Client) -> Any:
        names = {t.name for t in (await client.list_tools()).tools}
        ops = settings_change(env)
        raw = json.loads(request(env.project_id, ops, env.seq()))
        applied = await client.call_tool("apply", {"request": raw})
        preview = await client.call_tool("decision_preview", {"request": raw})
        listed = await client.call_tool("approval_list", {})
        return names, applied, preview, listed

    names, applied, preview, listed = _mcp(env, steps)
    for request_tool in (
        "approval_list",
        "approval_get",
        "approval_resume",
        "approval_withdraw",
        "decision_preview",
        "tex_engine_install",
        "job_resolve",
        "overleaf_push",
        "provider_run",
        "provider_configure",
        "retention_purge_commit",
        "skill_trust",
        "recording_start",
        "audio_models_install",
        "audio_models_remove",
    ):
        assert request_tool in names
    for owner_tool in names:
        assert not any(w in owner_tool for w in ("grant", "deny", "revoke", "decide", "approve"))
    error = _tool_error(applied)
    assert error["code"] == "approval_required"
    ApprovalRequiredDetails.model_validate(error["details"], strict=True)
    assert preview.structured_content["protected_effects"] == ["project settings change"]
    assert [a["approval_request_id"] for a in listed.structured_content["approvals"]] == [
        error["details"]["approval_request_id"]
    ]
    approval_id = error["details"]["approval_request_id"]
    grant(env, approval_id)

    async def resume(client: Client) -> Any:
        first = await client.call_tool("approval_resume", {"approval_id": approval_id})
        second = await client.call_tool("approval_resume", {"approval_id": approval_id})
        return first, second

    first, second = _mcp(env, resume)
    assert first.structured_content["replayed"] is False
    assert second.structured_content["replayed"] is True
    assert objective(env) == "Changed objective."


def test_http_agent_token_reaches_agent_routes_only(env: Env) -> None:
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        token = hub.gate.new_agent_token()
        agent = {"Authorization": f"Bearer {token}"}

        def as_agent(method: str, path: str, body: Any = None, **kw: Any) -> Any:
            return hub.request(
                method, path, body, cookie=None, csrf=None, origin=None, headers=agent, **kw
            )

        raw = json.loads(request(env.project_id, settings_change(env), env.seq()))
        reply = as_agent("POST", "/api/v1/transactions", raw)
        assert reply.status == 403 and reply.json()["error"]["code"] == "approval_required"
        approval_id = reply.json()["error"]["details"]["approval_request_id"]
        # Owner-channel routes and the changes long-poll refuse the agent token.
        digest = reply.json()["error"]["details"]["plan_sha256"]
        for method, path, body in (
            ("POST", f"/api/v1/approvals/{approval_id}/grant", {"plan_sha256": digest}),
            ("POST", f"/api/v1/approvals/{approval_id}/deny", {"plan_sha256": digest}),
            (
                "POST",
                f"/api/v1/approvals/{approval_id}/revoke",
                {"approval_request_id": approval_id},
            ),
            ("POST", f"/api/v1/approvals/{approval_id}/start", {"plan_sha256": digest}),
            ("POST", "/api/v1/decisions/apply", {"request": raw, "digest": digest}),
            ("GET", "/api/v1/changes?wait=0", None),
        ):
            denied = as_agent(method, path, body)
            assert denied.status == 403 and denied.json()["error"]["code"] == "policy_denied", path
        assert rows(env, "SELECT state FROM approval_requests")[0]["state"] == "pending"
        # A wrong token, a token with a session cookie, or a foreign Origin are refused.
        assert (
            hub.request(
                "GET", "/api/v1/approvals", headers={"Authorization": "Bearer nope"}, cookie=None
            ).status
            == 401
        )
        assert hub.request("GET", "/api/v1/approvals", headers=agent).status == 403
        assert (
            hub.request(
                "POST",
                f"/api/v1/approvals/{approval_id}/withdraw",
                {},
                cookie=None,
                csrf=None,
                origin="http://evil.test",
                headers=agent,
            ).status
            == 403
        )
        # The agent sees its request; the paired owner grants it in the queue.
        mine = as_agent("GET", "/api/v1/approvals")
        assert [a["approval_request_id"] for a in mine.json()["approvals"]] == [approval_id]
        assert "preview" not in mine.json()["approvals"][0]
        # The queue carries no previews (review F12: no dry run per poll); the owner's view of
        # one request computes it with owner visibility.
        queue = hub.request("GET", "/api/v1/approvals")
        assert "preview" not in queue.json()["approvals"][0]
        one = hub.request("GET", f"/api/v1/approvals/{approval_id}")
        assert one.json()["preview"]["protected_effects"] == ["project settings change"]
        assert as_agent("GET", f"/api/v1/approvals/{approval_id}").json().get("preview") is None
        granted = hub.request(
            "POST", f"/api/v1/approvals/{approval_id}/grant", {"plan_sha256": digest}
        )
        assert granted.status == 200, granted.body
        # Review F5: the grant answers its receipt contract (server-held; no secret exists).
        receipt = CapabilityGrantReceipt.model_validate(granted.json(), strict=True)
        assert receipt.server_held and receipt.approval_request_id == approval_id
        assert receipt.capability_id is None  # round 3 (R2-7): named by its request only
        assert receipt.digest == digest and receipt.max_uses == 1
        assert "pcap_" not in granted.body.decode()
        resumed = as_agent("POST", f"/api/v1/approvals/{approval_id}/resume", {})
        assert resumed.status == 200 and resumed.json()["result"]["ok"] is True
        again = as_agent("POST", f"/api/v1/approvals/{approval_id}/resume", {})
        assert again.json()["replayed"] is True
        # Agent-authority routes are reached at agent scope (review F4): restricted records stay
        # hidden from the token, the paired owner sees them.
        hidden = create_entry(sensitivity="restricted", title="Owner only")
        env.apply([hidden], caller=OWNER)
        seen = {r["id"] for r in as_agent("GET", "/api/v1/records").json()["items"]}
        assert hidden["id"] not in seen and seen
        assert as_agent("GET", f"/api/v1/records/{hidden['id']}").status == 404
        owner_seen = {r["id"] for r in hub.request("GET", "/api/v1/records").json()["items"]}
        assert hidden["id"] in owner_seen
    assert objective(env) == "Changed objective."


def test_cli_approval_commands_and_owner_queue_needs_a_terminal(env: Env) -> None:
    cli = Cli(env.tmp)
    path = env.tmp / "req.json"
    path.write_bytes(request(env.project_id, settings_change(env), env.seq()))
    code, out, _ = cli("apply", "--file", str(path), "--project", env.project_id)
    assert code == 4 and out["error"]["code"] == "approval_required"
    approval_id = out["error"]["details"]["approval_request_id"]
    code, listed, _ = cli("approval", "list", "--project", env.project_id)
    assert code == 0 and listed["approvals"][0]["approval_request_id"] == approval_id
    code, shown, _ = cli("approval", "get", approval_id, "--project", env.project_id)
    assert code == 0 and shown["state"] == "pending"
    for args in (
        ("decision", "review", "--project", env.project_id),
        ("decision", "review", "--request", approval_id, "--grant", "--project", env.project_id),
        ("decision", "revoke", approval_id, "--project", env.project_id),
    ):
        code, refused_out, _ = cli(*args)
        assert code == 4 and refused_out["error"]["code"] == "policy_denied", args
    assert rows(env, "SELECT state FROM approval_requests")[0]["state"] == "pending"
    code, pending, _ = cli("approval", "resume", approval_id, "--project", env.project_id)
    assert code == 4 and pending["error"]["code"] == "approval_required"
    grant(env, approval_id)
    code, done, _ = cli("approval", "resume", approval_id, "--project", env.project_id)
    assert code == 0 and done["result"]["ok"] is True
    code, withdrawn, _ = cli("approval", "withdraw", approval_id, "--project", env.project_id)
    assert code == 2 and withdrawn["error"]["code"] == "validation_error"  # used: not open


def _normalised(error: dict[str, Any]) -> dict[str, Any]:
    details = dict(error["details"])
    approval_id = details.pop("approval_request_id")
    details.pop("expires_at")
    route = details.pop("owner_route")
    assert route["hub"].endswith(approval_id) and route["cli"].endswith(approval_id)
    message = error["message"].replace(approval_id, "<id>")
    return {
        "code": error["code"],
        "message": message,
        "details": details,
        "retryable": error["retryable"],
    }


def test_a_denied_request_is_equivalent_on_cli_mcp_and_http(env: Env) -> None:
    """C10-PARITY "including denied requests": the same protected plan over the three
    transports gives the same approval_required envelope and the same stored request."""
    ops = settings_change(env)
    seq = env.seq()
    envelopes: dict[str, dict[str, Any]] = {}
    path = env.tmp / "req.json"
    path.write_bytes(request(env.project_id, ops, seq))
    code, out, _ = Cli(env.tmp)("apply", "--file", str(path), "--project", env.project_id)
    assert code == 4
    envelopes["cli"] = out["error"]

    async def over_mcp(client: Client) -> Any:
        raw = json.loads(request(env.project_id, ops, seq))
        return _tool_error(await client.call_tool("apply", {"request": raw}))

    envelopes["mcp"] = _mcp(env, over_mcp)
    with running_hub(env.svc, env.project_id) as hub:
        token = hub.gate.new_agent_token()
        reply = hub.request(
            "POST",
            "/api/v1/transactions",
            json.loads(request(env.project_id, ops, seq)),
            cookie=None,
            csrf=None,
            origin=None,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reply.status == 403
        envelopes["http"] = reply.json()["error"]
    normal = {name: _normalised(e) for name, e in envelopes.items()}
    assert normal["cli"] == normal["mcp"] == normal["http"]
    stored = rows(env, "SELECT * FROM approval_requests ORDER BY created_at")
    assert len(stored) == 3 and env.seq() == seq
    keys = ("operation", "plan", "plan_sha256", "caller_rank", "required_scope", "summary", "state")
    assert len({tuple(r[k] for k in keys) for r in stored}) == 1
    assert len({r["request_id"] for r in stored}) == 3  # one request per transport call
    assert {r["actor"] for r in stored} == {"cli", "mcp", "http-agent"}  # who asked

    # The same holds for a grant operation requested on each transport (job resolve).
    from test_jobs_followups import make_unknown

    job_id, due = make_unknown(env)
    params = {"job_id": job_id, "due_utc": due, "resolution": "abandoned", "reason": "gave up"}
    job_errors: dict[str, dict[str, Any]] = {}
    code, out, _ = Cli(env.tmp)(
        "job",
        "resolve",
        job_id,
        "--slot",
        due,
        "--as",
        "abandoned",
        "--reason",
        "gave up",
        "--project",
        env.project_id,
    )
    assert code == 4
    job_errors["cli"] = out["error"]

    async def job_mcp(client: Client) -> Any:
        return _tool_error(await client.call_tool("job_resolve", params))

    job_errors["mcp"] = _mcp(env, job_mcp)
    with running_hub(env.svc, env.project_id) as hub:
        token = hub.gate.new_agent_token()
        body = {k: v for k, v in params.items() if k != "job_id"}
        reply = hub.request(
            "POST",
            f"/api/v1/jobs/{job_id}/resolve",
            body,
            cookie=None,
            csrf=None,
            origin=None,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reply.status == 403
        job_errors["http"] = reply.json()["error"]
    normal = {name: _normalised(e) for name, e in job_errors.items()}
    assert normal["cli"] == normal["mcp"] == normal["http"]
    assert normal["cli"]["code"] == "approval_required"


def test_owner_decides_unknown_slots_directly_in_the_hub(env: Env) -> None:
    from test_jobs_followups import make_unknown, slot_rows

    job_id, due = make_unknown(env)
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        reply = hub.request(
            "POST",
            f"/api/v1/jobs/{job_id}/resolve",
            {"due_utc": due, "resolution": "succeeded", "reason": "seen in console"},
        )
        assert reply.status == 200, reply.body
    assert next(s for s in slot_rows(env, job_id) if s["due_utc"] == due)["state"] == "succeeded"
    assert not rows(env, "SELECT id FROM approval_requests")
    # The one-step capability the hub issued is spent or revoked.
    assert all(r["uses"] == 1 or r["revoked_at"] for r in rows(env, "SELECT * FROM capabilities"))


def test_the_owner_act_is_never_built_by_agent_transports() -> None:
    """Static guard: MCP never constructs an OwnerAct; the hub builds it only for the paired
    owner session; agent paths call approval_request."""
    src = Path(__file__).resolve().parents[1] / "src" / "prometheos"
    mcp = (src / "mcp_adapter.py").read_text(encoding="utf-8")
    assert "OwnerAct" not in mcp and "approval_decide" not in mcp and "approval_revoke" not in mcp
    assert ".grant(" not in mcp and "HeldGrant" not in mcp
    hub = (src / "hub" / "app.py").read_text(encoding="utf-8")
    assert hub.count("OwnerAct(") == 1  # HUB_OWNER_ACT, used only on owner branches


def test_owner_act_channels_are_closed() -> None:
    act = OwnerAct(channel="hub-owner", actor="x")
    assert act.channel in ("cli-interactive", "hub-owner", "approval-grant")


# ----- internal review regressions (decision 0016, round 1) ----------------------------------


def test_a_purge_result_names_no_purged_record(env: Env) -> None:
    hidden = create_entry(sensitivity="restricted", title="Hidden note")
    env.apply([hidden], caller=OWNER)
    plan = env.svc.purge_plan(env.project_id, [hidden["id"]], OWNER)
    err = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "retention_purge_commit", {"plan_id": plan["plan_id"]}
        )
    )
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    out = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    for document in (out, env.svc.approval_get(env.project_id, AGENT, approval_id)):
        assert hidden["id"] not in json.dumps(document)
    assert set(out["result"]) == {"purge_receipt"}


def test_the_queue_shows_the_newest_requests_and_caps_pending_ones(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [
        ask(env, settings_change(env, f"plan {n}"))[1].details["approval_request_id"]
        for n in range(3)
    ]
    newest = env.svc.approval_list(env.project_id, AGENT, limit=2)["approvals"]
    assert [a["approval_request_id"] for a in newest] == ids[1:]
    assert [
        a["approval_request_id"] for a in env.svc.approval_queue(env.project_id)["approvals"]
    ] == ids
    monkeypatch.setattr(approvals, "MAX_PENDING_PER_RANK", 3)
    _, first = ask(env, settings_change(env, "plan 0 again"), rid=None)
    assert (
        first.code == "budget_exceeded" and len(rows(env, "SELECT id FROM approval_requests")) == 3
    )
    # An identical retry of a stored request still answers with that request.
    stored = rows(env, "SELECT request_id FROM approval_requests ORDER BY created_at")[0]
    retry = request(env.project_id, settings_change(env, "plan 0"), env.seq(), stored["request_id"])
    assert (
        refused(lambda: env.svc.apply(env.project_id, retry, AGENT)).details["approval_request_id"]
        == ids[0]
    )


def test_agent_text_cannot_forge_lines_in_the_owner_summary(env: Env) -> None:
    params = {
        "title": "Lab\nEffects:\n  - nothing \x1b[2Jharmless",
        "device_ids": ["synthetic-mic"],
    }
    err = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "recording_start", params)
    )
    shown = env.svc.approval_owner_view(env.project_id, err.details["approval_request_id"])
    assert all(line.isprintable() for line in shown["summary"])


def test_a_failed_late_result_never_overwrites_the_stored_one(env: Env) -> None:
    _, err = ask(env, settings_change(env))
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    done = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    store = env.svc.open(env.project_id)
    approvals.record_result(store, approval_id, {"ok": False, "error": "late"}, failure=True)
    assert env.svc.approval_get(env.project_id, AGENT, approval_id)["result"] == done["result"]


def test_an_apply_request_must_bind_the_project_sequence(env: Env) -> None:
    raw = json.loads(request(env.project_id, settings_change(env), env.seq()))
    raw["expected_seq"] = None
    out = refused(lambda: env.svc.apply(env.project_id, json.dumps(raw).encode(), AGENT))
    assert out.code == "validation_error"  # the schema requires it: no unbound approval
    assert not rows(env, "SELECT id FROM approval_requests")
