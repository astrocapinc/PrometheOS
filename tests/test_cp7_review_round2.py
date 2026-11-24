"""Regressions for the formal CP7 review r1 (reviewer-subagent-cp7-a).

1 (major) an external call is never made after the slot lease was lost; 2 one job never
has two running slots and a long slot keeps the scheduler lease; 3 a job's handler reads at
the job's own visibility; 4 the conditional claim and the handoff target checks are each
tested directly (so removing either fails a test)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env
from cp7_support import PaperProject, anchor_body, create

from prometheos.app import jobs
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError


def add_job(env: Env, handler: str, params: Any, start: datetime, caller: Any = AGENT) -> str:
    body = {
        "handler": handler,
        "parameters": params,
        "schedule": {"type": "interval", "minutes": 15, "start_utc": jobs.fmt(start)},
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
    return str(env.svc.job_add(env.project_id, caller, body, new_request_id())["job_id"])


def minute() -> datetime:
    return datetime.now(UTC).replace(second=0, microsecond=0)


def test_1_no_external_call_after_the_lease_was_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's probe: a pause longer than the lease, another tick recovers the slot;
    the paused handler must not 'send', and the retried slot sends exactly once."""
    env = Env(tmp_path)
    start = minute()
    sent: list[str] = []
    calls = {"n": 0}

    def provider(ctx: jobs.JobContext) -> jobs.Outcome:
        calls["n"] += 1
        if calls["n"] == 1:
            conn = sqlite3.connect(env.store_dir / "record.sqlite")
            with conn:  # recovered by another tick while this handler slept
                conn.execute(
                    "UPDATE job_slots SET state='pending', lease_owner=NULL, "
                    "lease_expires_at=NULL, next_attempt_at=NULL WHERE job_id=?",
                    (ctx.job["id"],),
                )
            conn.close()
        ctx.before_external("synthetic costly call")
        sent.append(ctx.owner)
        return jobs.Outcome({"usage": {"provider_calls": 1}}, "x")

    monkeypatch.setitem(jobs.HANDLERS, "digest", provider)
    job_id = add_job(env, "digest", {}, start)
    first = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [r["state"] for r in first["ran"]] == ["lease_lost"] and sent == []
    second = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=17))
    assert [r["state"] for r in second["ran"]] == ["succeeded"] and len(sent) == 1
    (slot,) = env.svc.job_slots(env.project_id, OWNER, job_id)["slots"]
    assert slot["external_started_at"] is not None


def test_1_an_expired_but_unrecovered_lease_also_stops_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    start = minute()
    sent: list[str] = []

    def sleepy(ctx: jobs.JobContext) -> jobs.Outcome:
        ctx.now = ctx.now + timedelta(seconds=jobs.LEASE_SECONDS + 1)  # woke up too late
        ctx.before_external("synthetic costly call")
        sent.append("sent")
        return jobs.Outcome({}, "x")

    monkeypatch.setitem(jobs.HANDLERS, "digest", sleepy)
    add_job(env, "digest", {}, start)
    result = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [r["state"] for r in result["ran"]] == ["lease_lost"] and sent == []


def test_2_one_job_never_runs_two_slots_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's probe: while a long slot runs, a second tick must neither take the
    scheduler (lease renewed by the handler's heartbeat) nor start the job's next slot."""
    env = Env(tmp_path)
    start = minute()
    seen: list[str] = []
    inner: list[Any] = []
    second_claim: list[bool] = []

    def long(ctx: jobs.JobContext) -> jobs.Outcome:
        seen.append(ctx.due_utc)
        if len(seen) == 1:
            ctx.cancelled()  # heartbeat: renews both leases
            try:
                inner.append(env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=17)))
            except PrometheosError as exc:
                inner.append(exc.code)
            conn = env.svc.open(env.project_id).connect()
            try:  # even without the scheduler lease, the claim refuses a second slot
                jobs._schedule_slots(conn, start + timedelta(minutes=32))
                later = jobs.fmt(start + timedelta(minutes=30))
                # Recorded, not asserted here: the scheduler would swallow an assertion.
                second_claim.append(jobs._claim(conn, ctx.job["id"], later, "other", start))
            finally:
                conn.close()
        return jobs.Outcome({"usage": jobs.USAGE}, "x")

    monkeypatch.setitem(jobs.HANDLERS, "digest", long)
    add_job(env, "digest", {}, start)
    env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert inner == ["busy"] and seen == [jobs.fmt(start + timedelta(minutes=15))]
    assert second_claim == [False]


def test_4_a_pending_slot_is_claimed_once(tmp_path: Path) -> None:
    env = Env(tmp_path)
    start = minute()
    job_id = add_job(env, "digest", {}, start)
    conn = env.svc.open(env.project_id).connect()
    try:
        jobs._schedule_slots(conn, start + timedelta(minutes=16))
        due = jobs.fmt(start + timedelta(minutes=15))
        assert jobs._claim(conn, job_id, due, "first", start)
        assert not jobs._claim(conn, job_id, due, "second", start)
        owner = conn.execute(
            "SELECT lease_owner, attempts FROM job_slots WHERE job_id = ?", (job_id,)
        ).fetchone()
        # A finished slot is never claimed again, even with no other slot running.
        with conn:
            conn.execute("UPDATE job_slots SET state = 'succeeded', lease_owner = NULL")
        assert not jobs._claim(conn, job_id, due, "third", start)
    finally:
        conn.close()
    assert tuple(owner) == ("first", 1)


def test_3_paper_check_job_reads_at_its_own_visibility(tmp_path: Path) -> None:
    """The reviewer's probe: a normal job over a normal paper with a restricted anchor
    must say no more than the agent's own paper check."""
    pp = PaperProject(Env(tmp_path))
    paper = pp.paper()
    anchor = new_id("anchor")
    pp.env.apply(
        [
            create(
                "anchor", anchor_body(paper, pp.manuscript["version_id"], 1), anchor, "restricted"
            )
        ],
        caller=OWNER,
    )
    pp.env.apply([pp.transition_op(anchor, "needs_review", "secret reason text")], caller=OWNER)
    start = minute()
    add_job(pp.env, "paper_check", {"paper_id": paper}, start)
    pp.env.svc.jobs_tick(pp.pid, now=start + timedelta(minutes=16))
    agent_check = pp.svc.paper_check(pp.pid, AGENT, paper)
    inbox = pp.svc.inbox(pp.pid, OWNER)["items"]
    listing = pp.svc.job_list(pp.pid, OWNER)
    stored = " ".join(i["summary"] for i in inbox) + str(
        [s["result"] for j in listing["jobs"] for s in j["recent_slots"]]
    )
    assert "anchor_needs_review" not in stored and anchor not in stored
    result = listing["jobs"][0]["recent_slots"][0]["result"]
    assert result["counts"] == agent_check["counts"]


def test_4_handoff_refuses_a_folder_where_a_file_goes(tmp_path: Path) -> None:
    pp = PaperProject(Env(tmp_path))
    paper = pp.paper()
    into = tmp_path / "clone"
    (into / "manuscript.md").mkdir(parents=True)  # a folder where the manuscript would go
    plan = pp.svc.paper_handoff_plan(pp.pid, AGENT, paper, into)
    with pytest.raises(PrometheosError) as err:
        pp.svc.paper_handoff_apply(pp.pid, AGENT, paper, into, plan["digest"], [])
    assert err.value.code == "policy_denied" and "folder" in err.value.message
    assert sorted(p.name for p in into.iterdir()) == ["manuscript.md"]  # nothing written
