"""C07-JOBS (S17): single due-slot acquisition, DST and catch-up rules, cancellation, and
an unknown external outcome that is never silently repeated.

Times are injected into the scheduler; job records themselves are created at the real
clock, so every scenario uses instants after today. DST dates are computed from the
time-zone database, not hard-coded."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry
from cp7_support import PaperProject, text_evidence
from test_cp1_cli import Cli

from prometheos.app import jobs
from prometheos.domain.models import PrometheosError
from prometheos.faults import FAULT_EXIT_CODE

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")


def budget(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "max_inputs": 20,
        "max_output_chars": 2000,
        "max_provider_calls": 0,
        "max_concurrent_runs": 1,
        "spend_cap_estimate_usd": None,
    }
    value.update(overrides)
    return value


def job(
    handler: str, schedule: dict[str, Any], parameters: Any = None, **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "handler": handler,
        "parameters": parameters if parameters is not None else {},
        "schedule": schedule,
        "enabled": True,
        "budget": budget(),
        "last_slot": None,
    }
    body.update(extra)
    return body


def interval(minutes: int, start: datetime) -> dict[str, Any]:
    return {"type": "interval", "minutes": minutes, "start_utc": jobs.fmt(start)}


def weekly(day: str, at: str, zone: str) -> dict[str, Any]:
    return {"type": "weekly", "day": day, "time": at, "zone": zone}


def minute_now() -> datetime:
    return datetime.now(UTC).replace(second=0, microsecond=0)


def transition_day(zone: ZoneInfo, at: str, kind: str) -> date:
    """Next Sunday after today where local ``at`` is repeated ("fall") or missing ("spring")."""
    hour, minute = (int(p) for p in at.split(":"))
    day = datetime.now(UTC).date() + timedelta(days=1)
    for _ in range(800):
        if day.weekday() == 6:
            first = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
            second = first.replace(fold=1)
            utc_first, utc_second = first.astimezone(UTC), second.astimezone(UTC)
            missing = utc_first.astimezone(zone).replace(tzinfo=None) != first.replace(tzinfo=None)
            if kind == "fall" and utc_first != utc_second and not missing:
                return day
            if kind == "spring" and missing:
                return day
        day += timedelta(days=1)
    raise AssertionError("no transition found")


def slot_rows(env: Env, job_id: str) -> list[dict[str, Any]]:
    return env.svc.job_slots(env.project_id, OWNER, job_id)["slots"]


def add(env: Env, body: dict[str, Any], caller: Any = AGENT) -> str:
    from prometheos.domain.ids import new_request_id

    return str(env.svc.job_add(env.project_id, caller, body, new_request_id())["job_id"])


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ----- schedules ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("zone", "name"), [(LONDON, "Europe/London"), (NEW_YORK, "America/New_York")]
)
def test_repeated_local_time_runs_once_at_first_occurrence(zone: ZoneInfo, name: str) -> None:
    day = transition_day(zone, "01:30", "fall")
    schedule = weekly("sunday", "01:30", name)
    start = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(days=1)
    due, total = jobs.due_between(schedule, start, start + timedelta(days=3))
    first = datetime(day.year, day.month, day.day, 1, 30, tzinfo=zone, fold=0).astimezone(UTC)
    second = datetime(day.year, day.month, day.day, 1, 30, tzinfo=zone, fold=1).astimezone(UTC)
    assert second - first == timedelta(hours=1)
    assert due == [first] and total == 1
    preview = jobs.upcoming(schedule, start, 1)
    assert preview[0]["due_utc"] == jobs.fmt(first) and "first occurrence" in preview[0]["note"]


@pytest.mark.parametrize(
    ("zone", "name", "at"),
    [(LONDON, "Europe/London", "01:30"), (NEW_YORK, "America/New_York", "02:30")],
)
def test_missing_local_time_skips_that_week(zone: ZoneInfo, name: str, at: str) -> None:
    day = transition_day(zone, at, "spring")
    schedule = weekly("sunday", at, name)
    start = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(days=8)
    due, total = jobs.due_between(schedule, start, start + timedelta(days=16))
    local_days = [d.astimezone(zone).date() for d in due]
    assert day not in local_days and total == 2  # the week before and the week after
    assert local_days == [day - timedelta(days=7), day + timedelta(days=7)]
    preview = jobs.upcoming(schedule, start, 3)
    assert {"local_date": day.isoformat(), "skipped": preview[1]["skipped"]} == preview[1]


def test_interval_backlog_coalesces_to_one_slot(env: Env) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    env.apply([create_entry(title="Something happened")])
    later = start + timedelta(hours=2, minutes=1)
    first = env.svc.jobs_tick(env.project_id, now=later)
    assert [r["state"] for r in first["ran"]] == ["succeeded"]
    rows = slot_rows(env, job_id)
    ran = [r for r in rows if r["state"] == "succeeded"]
    skipped = [r for r in rows if r["state"] == "skipped"]
    assert [r["due_utc"] for r in ran] == [jobs.fmt(start + timedelta(minutes=120))]
    assert len(skipped) == 7 and all(r["reason"].startswith("coalesced") for r in skipped)
    assert len({r["due_utc"] for r in rows}) == len(rows)
    # The same tick again records nothing new and runs nothing.
    again = env.svc.jobs_tick(env.project_id, now=later)
    assert again["ran"] == [] and slot_rows(env, job_id) == rows
    nxt = env.svc.jobs_tick(env.project_id, now=later + timedelta(minutes=15))
    assert [r["due_utc"] for r in nxt["ran"]] == [jobs.fmt(start + timedelta(minutes=135))]


def test_one_scheduler_and_one_claim_per_slot(env: Env) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    now = start + timedelta(minutes=16)
    conn = env.svc.open(env.project_id).connect()
    try:
        jobs._acquire(conn, "another-process", now)
    finally:
        conn.close()
    with pytest.raises(PrometheosError) as err:
        env.svc.jobs_tick(env.project_id, now=now)
    assert err.value.code == "busy"
    # Simultaneous ticks: whatever interleaving, the slot runs exactly once.
    outcomes: list[Any] = []
    barrier = threading.Barrier(4)

    def racer() -> None:
        barrier.wait()
        try:
            outcomes.append(env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=3)))
        except PrometheosError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=racer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    ran = [
        r for o in outcomes if isinstance(o, dict) for r in o["ran"] if r["state"] == "succeeded"
    ]
    assert len(ran) == 1
    assert all(o == "busy" for o in outcomes if not isinstance(o, dict))
    assert [r["state"] for r in slot_rows(env, job_id)] == ["succeeded"]


# ----- S17: unknown external outcome -----------------------------------------------------------


def test_crash_after_external_request_is_never_repeated(env: Env) -> None:
    day = transition_day(LONDON, "01:30", "fall")
    job_id = add(env, job("digest", weekly("sunday", "01:30", "Europe/London")))
    first = datetime(day.year, day.month, day.day, 1, 30, tzinfo=LONDON, fold=0).astimezone(UTC)
    repeat = datetime(day.year, day.month, day.day, 1, 30, tzinfo=LONDON, fold=1).astimezone(UTC)
    now = repeat + timedelta(minutes=30)  # both wall-clock 01:30s have passed

    died = env.worker("jobs-tick", env.project_id, now.isoformat(), "external",
                      fault="job_after_external_mark")  # fmt: skip
    assert died.returncode == FAULT_EXIT_CODE, died.stderr
    rows = {r["due_utc"]: r for r in slot_rows(env, job_id)}
    assert jobs.fmt(repeat) not in rows  # the repeated local time is not a second slot
    current = rows[jobs.fmt(first)]
    assert current["state"] == "running" and current["external_started_at"]
    assert current["attempts"] == 1
    assert all(r["reason"].startswith("coalesced") for d, r in rows.items() if d != jobs.fmt(first))

    # Tick again while the dead process's leases are still current: nothing runs.
    with pytest.raises(PrometheosError) as err:
        env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=1))
    assert err.value.code == "busy"
    seq_before = env.seq()
    resumed = env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=10))
    assert resumed["ran"] == []
    assert resumed["recovered"] == [
        {
            "job_id": job_id,
            "due_utc": jobs.fmt(first),
            "state": "unknown",
            "reason": resumed["recovered"][0]["reason"],
        }
    ]
    assert "not repeated" in resumed["recovered"][0]["reason"]
    after = {r["due_utc"]: r for r in slot_rows(env, job_id)}[jobs.fmt(first)]
    assert after["state"] == "unknown" and after["attempts"] == 1
    inbox = env.svc.inbox(env.project_id, OWNER)["items"]
    assert [i["category"] for i in inbox] == ["slot_unknown"]
    assert "decision needed" in inbox[0]["title"]
    assert env.seq() == seq_before + 1  # the notification is the only research-visible write
    # Later ticks never pick it up again.
    later = env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=30))
    assert later["ran"] == [] and later["recovered"] == []
    assert {r["due_utc"]: r for r in slot_rows(env, job_id)}[jobs.fmt(first)]["state"] == "unknown"


def test_crash_before_any_side_effect_is_retried(env: Env) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    now = start + timedelta(minutes=16)
    died = env.worker("jobs-tick", env.project_id, now.isoformat(), fault="job_after_claim")
    assert died.returncode == FAULT_EXIT_CODE, died.stderr
    assert [r["state"] for r in slot_rows(env, job_id)] == ["running"]
    resumed = env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=5))
    assert resumed["recovered"][0]["state"] == "pending"
    retried = env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=6))
    assert [r["state"] for r in retried["ran"]] == ["succeeded"]
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == "succeeded" and slot["attempts"] == 2


def test_failing_local_handler_retries_three_times_then_fails(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(ctx: jobs.JobContext) -> jobs.Outcome:
        raise RuntimeError("synthetic failure")

    monkeypatch.setitem(jobs.HANDLERS, "digest", broken)
    start = minute_now()
    job_id = add(env, job("digest", interval(60, start)))
    now = start + timedelta(minutes=61)
    states = []
    for offset in (0, 31, 92, 300):  # backoff 30 s then 60 s
        tick = env.svc.jobs_tick(env.project_id, now=now + timedelta(seconds=offset))
        states.append([r["state"] for r in tick["ran"]])
    assert states == [["pending"], ["pending"], ["failed"], []]
    (slot,) = slot_rows(env, job_id)
    assert slot["attempts"] == 3 and "after 3 attempts" in slot["reason"]
    assert [i["category"] for i in env.svc.inbox(env.project_id, OWNER)["items"]] == ["slot_failed"]


# ----- cancellation and enable/disable -----------------------------------------------------


def test_cancellation(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    store = env.svc.open(env.project_id)
    conn = store.connect()
    try:
        jobs._schedule_slots(conn, start + timedelta(minutes=16))
    finally:
        conn.close()
    pending = jobs.fmt(start + timedelta(minutes=15))
    assert env.svc.job_cancel(env.project_id, AGENT, job_id, pending)["outcome"] == "skipped"
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert tick["ran"] == []
    assert slot_rows(env, job_id)[0]["reason"] == "cancelled"

    def cancels_itself(external: bool) -> Any:
        def handler(ctx: jobs.JobContext) -> jobs.Outcome:
            if external:
                ctx.before_external("synthetic request (test)")
            env.svc.job_cancel(env.project_id, AGENT, job_id, ctx.due_utc)
            assert ctx.cancelled()
            raise jobs.Cancelled()

        return handler

    monkeypatch.setitem(jobs.HANDLERS, "digest", cancels_itself(False))
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=31))
    assert [r["state"] for r in tick["ran"]] == ["skipped"]
    monkeypatch.setitem(jobs.HANDLERS, "digest", cancels_itself(True))
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=46))
    assert [r["state"] for r in tick["ran"]] == ["unknown"]
    last = slot_rows(env, job_id)[-1]
    assert "cannot be recalled" in last["reason"]
    with pytest.raises(PrometheosError):
        env.svc.job_cancel(env.project_id, AGENT, job_id, last["due_utc"])  # already final


def test_disable_skips_pending_and_reenable_does_not_backfill(env: Env) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    store = env.svc.open(env.project_id)
    conn = store.connect()
    try:
        jobs._schedule_slots(conn, start + timedelta(minutes=16))
    finally:
        conn.close()
    env.svc.job_set_enabled(
        env.project_id, AGENT, job_id, 1, False, "1d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    )
    assert slot_rows(env, job_id)[0]["state"] == "skipped"
    assert env.svc.jobs_tick(env.project_id, now=start + timedelta(hours=3))["ran"] == []
    env.svc.job_set_enabled(
        env.project_id, AGENT, job_id, 2, True, "2d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    )
    # Simulate the re-enable happening two hours later (records are written at the real
    # clock): slots of the disabled period are never back-filled.
    reenabled = start + timedelta(hours=2)
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    with conn:
        conn.execute(
            "UPDATE revisions SET recorded_at = ? WHERE record_id = ? AND rev = 3",
            (jobs.fmt_precise(reenabled), job_id),
        )
    conn.close()
    tick = env.svc.jobs_tick(env.project_id, now=reenabled + timedelta(minutes=16))
    assert [r["due_utc"] for r in tick["ran"]] == [jobs.fmt(reenabled + timedelta(minutes=15))]
    assert [(r["due_utc"], r["state"]) for r in slot_rows(env, job_id)] == [
        (jobs.fmt(start + timedelta(minutes=15)), "skipped"),
        (jobs.fmt(reenabled + timedelta(minutes=15)), "succeeded"),
    ]


# ----- handlers: quiet unless something changed -------------------------------------------


def test_digest_is_quiet_without_changes(env: Env) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    env.apply([create_entry(title="New observation")])
    seq = env.seq()
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert tick["ran"][0]["finding"] is True and env.seq() == seq + 1
    (note,) = env.svc.inbox(env.project_id, OWNER)["items"]
    assert note["category"] == "digest" and "entry: 1" in note["summary"]
    assert env.svc.get(env.project_id, OWNER, job_id, None)["body"]["last_slot"] == jobs.fmt(
        start + timedelta(minutes=15)
    )
    quiet = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=31))
    assert quiet["quiet"] and env.seq() == seq + 1
    assert len(env.svc.inbox(env.project_id, OWNER)["items"]) == 1
    result = slot_rows(env, job_id)[-1]["result"]
    assert result["usage"] == {"provider_calls": 0, "estimated_cost": None}
    env.svc.inbox_dismiss(env.project_id, OWNER, note["id"])
    assert env.svc.inbox(env.project_id, OWNER)["items"] == [] and env.seq() == seq + 1


def test_paper_check_job_reports_only_changes(tmp_path: Path) -> None:
    pp = PaperProject(Env(tmp_path))
    env = pp.env
    venue = {"id": pp.venue["version_id"], "rev": 1}
    rows = pp.svc.requirements_draft(pp.pid, AGENT, pp.venue["version_id"])["requirements"]
    paper = pp.paper(venue_source_refs=[venue], requirements=rows)
    start = minute_now()
    add(env, job("paper_check", interval(60, start), {"paper_id": paper}))
    first = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=61))
    assert first["ran"][0]["finding"] is True
    unchanged = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=121))
    assert unchanged["quiet"]
    met = dict(
        rows[0],
        applicability="applicable",
        status="met",
        last_checked_at="2026-10-04T10:00:00Z",
        evidence=[text_evidence(pp.manuscript, 1)],
    )
    pp.revise(paper, dict(pp.get(paper)["body"], requirements=[met, rows[1], rows[2]]))
    changed = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=181))
    assert changed["ran"][0]["finding"] is True
    newest = env.svc.inbox(env.project_id, OWNER)["items"][0]
    assert "resolved: requirement_unchecked" in newest["summary"]
    assert newest["title"].startswith("Paper readiness changed (0 new, 1 resolved)")


def test_source_refresh_records_changed_state_once(env: Env) -> None:
    imported = env.import_files(["baseline.md"])["plan"]["items"][0]
    start = minute_now()
    job_id = add(
        env, job("source_refresh", interval(15, start), {"source_ids": [imported["source_id"]]})
    )
    quiet = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert quiet["quiet"]  # intact, nothing to record
    (env.research / "baseline.md").write_bytes(b"changed on disk\n")
    seq = env.seq()
    found = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=31))
    assert found["ran"][0]["finding"] is True and env.seq() == seq + 1
    note = env.svc.inbox(env.project_id, OWNER)["items"][0]
    assert "origin changed since this version" in note["summary"]
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        saved = conn.execute("SELECT commit_seq FROM source_checks").fetchall()
    finally:
        conn.close()
    assert saved == [(seq + 1,)]  # observation and notification: one commit
    again = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=46))
    assert again["quiet"] and env.seq() == seq + 1
    del job_id


# ----- validation, plan and CLI --------------------------------------------------------------


def test_job_validation_and_plan(env: Env) -> None:
    start = minute_now()
    bad = [
        job("digest", interval(10, start)),
        job("digest", weekly("sunday", "01:30", "BST")),
        job("digest", weekly("sunday", "25:00", "Europe/London")),
        job("digest", interval(15, start), budget=budget(max_concurrent_runs=2)),
        job(
            "digest",
            interval(15, start),
            {"paper_id": "paper_00000000-0000-4000-8000-000000000001"},
        ),
        job("source_refresh", interval(15, start), {"source_ids": []}),
        job("digest", interval(15, start), enabled=False),
    ]
    for body in bad:
        with pytest.raises(PrometheosError) as err:
            env.svc.job_plan(env.project_id, AGENT, body)
        assert err.value.code == "validation_error", body
    missing = job(
        "paper_check",
        interval(15, start),
        {"paper_id": "paper_00000000-0000-4000-8000-000000000001"},
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.job_plan(env.project_id, AGENT, missing)
    assert err.value.code == "not_found"
    plan = env.svc.job_plan(
        env.project_id, AGENT, job("digest", weekly("monday", "09:00", "Europe/London"))
    )
    assert plan["transmits_data"] is False and len(plan["next_slots"]) == 5
    assert "nothing runs while the computer is off" in plan["runs_only_when"]


def test_cli_job_commands_emit_one_document(env: Env) -> None:
    cli = Cli(env.tmp)
    pid = env.project_id
    code, plan, _ = cli("job", "plan", "--handler", "digest", "--every", "30", "--project", pid)
    assert code == 0 and plan["job"]["schedule"]["minutes"] == 30
    code, added, _ = cli("job", "add", "--handler", "digest", "--weekly", "monday@09:00",
                         "--zone", "Europe/London", "--project", pid)  # fmt: skip
    assert code == 0 and added["job_id"].startswith("job_")
    code, listed, _ = cli("job", "list", "--project", pid)
    assert code == 0 and listed["jobs"][0]["job"]["id"] == added["job_id"]
    code, ticked, _ = cli("jobs", "tick", "--project", pid)
    assert code == 0 and "ran" in ticked
    code, inbox, _ = cli("jobs", "inbox", "--project", pid)
    assert code == 0 and inbox["items"] == []
    code, err, _ = cli("job", "add", "--handler", "digest", "--every", "5", "--project", pid)
    assert code == 2 and err["error"]["code"] == "validation_error"
    assert json.dumps(listed)  # serialisable as one document
