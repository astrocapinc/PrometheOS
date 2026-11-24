"""Decision 0014 items 6-9: follow-ups from the cp7-a round 2 review.

6  a slot's problem notice is committed before its state changes, and a crash between the
   two neither loses nor duplicates the notice;
7  the two lease conditions of ``JobContext._renew`` fail separately and say which;
8  built-in handlers reach ``ctx.cancelled()`` between small units of work;
9  an owner can resolve an ``unknown`` slot by hand, with a reason, authorised by an exact-plan
   owner decision, and every reader shows the resolution.
Round 2 (review 0014): a busy store at a checkpoint fails the attempt instead of recording an
observation, and a cancel after an external request began notifies the owner.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry
from cp7_support import PaperProject
from test_cp1_cli import Cli, synthetic_capability
from test_cp7_jobs import add, interval, job, minute_now, slot_rows

from prometheos.app import jobs, sources
from prometheos.domain.models import PrometheosError
from prometheos.faults import FAULT_EXIT_CODE
from prometheos.store.project import ProjectStore


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def notices(env: Env) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = env.svc.inbox(env.project_id, OWNER, True)["items"]
    return items


def raw_slots(env: Env) -> list[sqlite3.Row]:
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM job_slots ORDER BY due_utc").fetchall()
    finally:
        conn.close()


# ----- item 6: notice first, state second ------------------------------------------------------


def unknown_after_crash(env: Env, now: datetime, job_id: str) -> None:
    """Leave one running slot that has an external mark and an expired lease."""
    died = env.worker(
        "jobs-tick", env.project_id, now.isoformat(), "external", fault="job_after_external_mark"
    )
    assert died.returncode == FAULT_EXIT_CODE, died.stderr
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == "running" and slot["external_started_at"]


def test_recover_commits_the_notice_before_the_state_and_a_crash_between_is_safe(env: Env) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    now = start + timedelta(minutes=16)
    unknown_after_crash(env, now, job_id)
    assert notices(env) == []
    seq_before = env.seq()

    # Recovery dies right after committing the notice, before it changes the slot's state.
    died = env.worker(
        "jobs-tick", env.project_id, (now + timedelta(minutes=10)).isoformat(),
        fault="job_after_notice",
    )  # fmt: skip
    assert died.returncode == FAULT_EXIT_CODE, died.stderr
    assert [n["category"] for n in notices(env)] == ["slot_unknown"]  # notice is durable...
    assert env.seq() == seq_before + 1
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == "running"  # ...while the state it reports is not yet changed

    # The next tick finishes the recovery: the state changes and the notice is not repeated.
    resumed = env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=20))
    assert [r["state"] for r in resumed["recovered"]] == ["unknown"]
    assert [n["category"] for n in notices(env)] == ["slot_unknown"]
    assert env.seq() == seq_before + 1  # the replayed notice wrote nothing
    crashed = next(r for r in slot_rows(env, job_id) if r["external_started_at"])
    assert crashed["state"] == "unknown"


def test_failed_handler_commits_the_notice_before_the_state_and_a_crash_between_is_safe(
    env: Env,
) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    now = start + timedelta(minutes=16)
    died = env.worker(
        "jobs-tick", env.project_id, now.isoformat(), "fail-external", fault="job_after_notice"
    )
    assert died.returncode == FAULT_EXIT_CODE, died.stderr
    assert [n["category"] for n in notices(env)] == ["slot_unknown"]
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == "running" and slot["external_started_at"]
    seq = env.seq()
    resumed = env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=10))
    assert [r["state"] for r in resumed["recovered"]] == ["unknown"]
    assert [n["category"] for n in notices(env)] == ["slot_unknown"] and env.seq() == seq


def test_run_slot_has_the_notice_before_it_finishes_the_slot(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same order without a crash: when ``_finish`` records unknown/failed, the notice
    for that slot is already committed."""

    def failing(ctx: jobs.JobContext) -> jobs.Outcome:
        ctx.before_external("synthetic request (test)")
        raise RuntimeError("synthetic failure")

    seen: list[tuple[str, list[str]]] = []
    real_finish = jobs._finish

    def spying_finish(store: ProjectStore, *args: Any, **kwargs: Any) -> bool:
        state = args[3]
        seen.append((state, [n["category"] for n in notices(env)]))
        return real_finish(store, *args, **kwargs)

    monkeypatch.setitem(jobs.HANDLERS, "digest", failing)
    monkeypatch.setattr(jobs, "_finish", spying_finish)
    start = minute_now()
    add(env, job("digest", interval(15, start)))
    env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert seen == [("unknown", ["slot_unknown"])]


# ----- item 7: the two lease conditions --------------------------------------------------------


def claimed(env: Env) -> tuple[jobs.JobContext, datetime]:
    """A job context for a slot this owner has claimed while holding the scheduler lease."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    now = start + timedelta(minutes=16)
    store = env.svc.open(env.project_id)
    conn = store.connect()
    try:
        jobs._acquire(conn, "tick:me", now)
        jobs._schedule_slots(conn, now)
        due = jobs.fmt(start + timedelta(minutes=15))
        assert jobs._claim(conn, job_id, due, "tick:me", now)
        record = jobs.current_record(conn, job_id)
    finally:
        conn.close()
    assert record is not None
    return jobs.JobContext(store, record, due, "tick:me", now), now


def lease_times(env: Env) -> tuple[str, str]:
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        slot = conn.execute("SELECT lease_expires_at FROM job_slots").fetchone()[0]
        scheduler = conn.execute("SELECT expires_at FROM scheduler_lease").fetchone()[0]
        return str(slot), str(scheduler)
    finally:
        conn.close()


def execute(env: Env, sql: str, *params: Any) -> None:
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_renew_extends_both_leases_when_both_are_held(env: Env) -> None:
    ctx, _ = claimed(env)
    slot_before, scheduler_before = lease_times(env)
    assert ctx.cancelled() is False
    slot_after, scheduler_after = lease_times(env)
    assert slot_after > slot_before and scheduler_after > scheduler_before


def test_renew_reports_a_lost_slot_lease_with_the_scheduler_lease_still_held(env: Env) -> None:
    ctx, _ = claimed(env)
    _, scheduler_before = lease_times(env)
    # Recovered by another tick: the slot is pending again; this tick still holds the scheduler.
    execute(env, "UPDATE job_slots SET state='pending', lease_owner=NULL, lease_expires_at=NULL")
    with pytest.raises(jobs.LeaseLost) as lost:
        ctx.cancelled()
    assert lost.value.lost == "slot"
    assert lease_times_or_none(env)[1] == scheduler_before  # nothing was renewed


def test_renew_reports_a_slot_lease_that_expired_without_recovery(env: Env) -> None:
    ctx, _ = claimed(env)
    execute(env, "UPDATE job_slots SET lease_expires_at = '1970-01-01T00:00:00.000000Z'")
    with pytest.raises(jobs.LeaseLost) as lost:
        ctx.cancelled()
    assert lost.value.lost == "slot"


def lease_times_or_none(env: Env) -> tuple[str, str | None]:
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        slot = conn.execute("SELECT lease_expires_at FROM job_slots").fetchone()[0]
        row = conn.execute("SELECT expires_at FROM scheduler_lease").fetchone()
        return str(slot), (str(row[0]) if row else None)
    finally:
        conn.close()


@pytest.mark.parametrize("how", ["taken", "expired", "released"])
def test_renew_reports_a_lost_scheduler_lease_with_the_slot_lease_still_held(
    env: Env, how: str
) -> None:
    ctx, _ = claimed(env)
    slot_before, _ = lease_times(env)
    if how == "taken":
        execute(env, "UPDATE scheduler_lease SET owner = 'tick:other'")
    elif how == "expired":
        execute(env, "UPDATE scheduler_lease SET expires_at = '1970-01-01T00:00:00.000000Z'")
    else:
        execute(env, "DELETE FROM scheduler_lease")
    with pytest.raises(jobs.LeaseLost) as lost:
        ctx.cancelled()
    assert lost.value.lost == "scheduler"
    # The slot renewal ran first but was rolled back with the failed scheduler renewal.
    assert lease_times_or_none(env)[0] == slot_before


def test_renew_names_the_slot_when_both_leases_are_gone(env: Env) -> None:
    ctx, _ = claimed(env)
    execute(env, "UPDATE job_slots SET state='pending', lease_owner=NULL, lease_expires_at=NULL")
    execute(env, "DELETE FROM scheduler_lease")
    with pytest.raises(jobs.LeaseLost) as lost:
        ctx.cancelled()
    assert lost.value.lost == "slot"


def test_lost_lease_is_visible_in_the_tick_result(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def pauses(ctx: jobs.JobContext) -> jobs.Outcome:
        execute(env, "UPDATE scheduler_lease SET owner = 'tick:other'")
        ctx.before_external("synthetic call")
        raise AssertionError("unreachable: the lease is lost")

    monkeypatch.setitem(jobs.HANDLERS, "digest", pauses)
    start = minute_now()
    add(env, job("digest", interval(15, start)))
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [(r["state"], r["lost"]) for r in tick["ran"]] == [("lease_lost", "scheduler")]


# ----- item 8: built-in handlers are cancellable -------------------------------------------------


@pytest.fixture
def eager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every checkpoint checks for cancellation (the production interval is seconds)."""
    monkeypatch.setattr(jobs, "CHECKPOINT_SECONDS", 0.0)


def request_cancel(env: Env, job_id: str) -> None:
    due = next(r["due_utc"] for r in slot_rows(env, job_id) if r["state"] == "running")
    assert env.svc.job_cancel(env.project_id, AGENT, job_id, due)["outcome"] == "cancel_requested"


def test_checkpoint_is_throttled_to_the_interval(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = claimed(env)
    calls = []
    monkeypatch.setattr(ctx, "cancelled", lambda: calls.append(1) or False)
    for _ in range(50):
        ctx.checkpoint()
    assert calls == []  # inside the interval: no database work at all
    monkeypatch.setattr(jobs, "CHECKPOINT_SECONDS", 0.0)
    ctx.checkpoint()
    ctx.checkpoint()
    assert len(calls) == 2
    assert jobs.CHECKPOINT_SECONDS == 0.0 and 0 < jobs.LEASE_SECONDS


def test_production_checkpoint_interval_is_well_under_the_lease() -> None:
    assert 0 < jobs.CHECKPOINT_SECONDS <= jobs.LEASE_SECONDS / 4


def test_checkpoint_raises_cancelled_when_requested(env: Env, eager: None) -> None:
    ctx, _ = claimed(env)
    ctx.checkpoint()  # nothing requested: returns
    execute(env, "UPDATE job_slots SET cancel_requested = 1")
    with pytest.raises(jobs.Cancelled):
        ctx.checkpoint()


def spy_on_reads(env: Env, job_id: str, reads: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap ``open_verified`` so the second ``read`` of the origin file requests cancellation."""
    real = sources.open_verified

    class Handle:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            if len(reads) == 2:
                request_cancel(env, job_id)
            data: bytes = self.inner.read(size)
            return data

    @contextlib.contextmanager
    def wrapper(resolved: Any) -> Iterator[Handle]:
        with real(resolved) as handle:
            yield Handle(handle)

    monkeypatch.setattr(sources, "open_verified", wrapper)


def test_source_refresh_stops_hashing_a_large_file_when_cancelled(
    env: Env, eager: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sources, "CHUNK", 1024)
    (env.research / "big.bin").write_bytes(b"x" * (40 * 1024))
    big = env.import_files(["big.bin"])["plan"]["items"][0]
    start = minute_now()
    job_id = add(
        env, job("source_refresh", interval(15, start), {"source_ids": [big["source_id"]]})
    )
    reads: list[int] = []
    spy_on_reads(env, job_id, reads, monkeypatch)
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [r["state"] for r in tick["ran"]] == ["skipped"]
    assert slot_rows(env, job_id)[0]["reason"] == "cancelled"
    # 40 chunks plus the end-of-file read would be 41 reads; cancellation stopped it early.
    assert 2 <= len(reads) <= 4


def test_paper_check_honours_a_cancellation_requested_before_it_opens_its_snapshot(
    tmp_path: Path, eager: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paper check holds one read snapshot (journal_mode=DELETE), inside which no write
    can commit, so it is bracketed by cancellation checks instead of having checkpoints."""
    pp = PaperProject(Env(tmp_path))
    env = pp.env
    paper = pp.paper()
    start = minute_now()
    job_id = add(env, job("paper_check", interval(15, start), {"paper_id": paper}))
    analysed: list[str] = []
    real_fault = jobs.fault_point

    def cancel_after_claim(name: str) -> None:
        if name == "job_after_claim":
            request_cancel(env, job_id)
        real_fault(name)

    monkeypatch.setattr(jobs, "fault_point", cancel_after_claim)
    monkeypatch.setattr(jobs.papers, "check", lambda *a, **k: analysed.append("ran"))
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [r["state"] for r in tick["ran"]] == ["skipped"] and analysed == []


def test_a_write_cannot_commit_while_a_handler_holds_a_read_snapshot(env: Env) -> None:
    """Why the paper check has no inner checkpoint: pins the store property that rules it out."""
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT COUNT(*) FROM records").fetchone()
        writer = sqlite3.connect(env.store_dir / "record.sqlite", timeout=0.05)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE job_slots SET cancel_requested = 1")
            with pytest.raises(sqlite3.OperationalError):
                writer.execute("COMMIT")
        finally:
            writer.close()
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_digest_stops_before_producing_a_result_when_cancelled(
    env: Env, eager: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    env.apply([create_entry(title="Something changed")])
    hashed: list[Any] = []
    steps: list[str] = []
    real_seq = jobs.project_seq
    real_checkpoint = jobs.JobContext.checkpoint

    def spy_seq(conn: sqlite3.Connection) -> int:
        steps.append("read")  # the handler is inside its read snapshot
        return int(real_seq(conn))

    def checkpoint(ctx: jobs.JobContext) -> None:
        if steps == ["read"]:
            # A cancellation requested while the handler was reading: another caller is
            # admitted only once the handler's read connection has closed (decision 0025),
            # which is the first moment it can commit (decision 0027).
            steps.append("cancel")
            request_cancel(env, job_id)
        real_checkpoint(ctx)

    with monkeypatch.context() as patched:
        patched.setattr(jobs, "project_seq", spy_seq)
        patched.setattr(jobs.JobContext, "checkpoint", checkpoint)
        patched.setattr(jobs, "canonical_sha256", lambda value: hashed.append(value) or "x")
        tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert steps == ["read", "cancel"]
    assert [r["state"] for r in tick["ran"]] == ["skipped"]
    assert hashed == []  # the handler never reached its result
    assert notices(env) == []


# ----- round 2: a busy store at a checkpoint is not an observation of the origin -----------------


def test_a_failing_progress_callback_propagates_instead_of_becoming_an_observation(
    env: Env,
) -> None:
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    store = env.svc.open(env.project_id)
    conn = store.connect(readonly=True)
    try:
        settings, _ = sources._settings(conn, env.project_id)

        def busy() -> None:
            raise PrometheosError("busy", "the project store is busy; retry shortly", {})

        with pytest.raises(PrometheosError) as err:
            sources.observe_version(store, conn, settings, item["version_id"], progress=busy)
        assert err.value.code == "busy"
        # An ordinary origin problem is still an observation, with or without a callback.
        (env.research / "baseline.md").unlink()
        for progress in (None, lambda: None):
            seen = sources.observe_version(store, conn, settings, item["version_id"], progress)
            assert seen["origin_reachable"] is False and seen["error"] is not None
    finally:
        conn.close()


def test_a_busy_store_at_a_checkpoint_fails_the_attempt_for_retry_not_as_unreachable(
    env: Env, eager: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent reader holds the store while a scheduled refresh renews its lease at a
    checkpoint: the renewal is busy. That must not become a saved "origin unreachable".

    The reader is a raw SQLite connection holding a read snapshot; decision 0025 treats its
    open handle conservatively as busy for any new admission. It ends as soon as the
    checkpoint has seen busy (as a hub read ends), so the attempt's own completion, which
    holds no connection across the handler (decision 0027), is admitted and records a retry.
    No timer: the order is fixed by the checkpoint itself."""
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    start = minute_now()
    job_id = add(
        env, job("source_refresh", interval(15, start), {"source_ids": [item["source_id"]]})
    )
    quiet = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert quiet["quiet"]  # a first intact observation exists to compare with
    holders: list[sqlite3.Connection] = []
    busy_seen: list[str] = []
    real = sources.open_verified
    real_cancelled = jobs.JobContext.cancelled

    class Handle:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def read(self, size: int = -1) -> bytes:
            if not holders:  # a concurrent reader takes its snapshot
                reader = sqlite3.connect(env.store_dir / "record.sqlite", check_same_thread=False)
                reader.execute("BEGIN")
                reader.execute("SELECT COUNT(*) FROM records").fetchone()
                holders.append(reader)
            data: bytes = self.inner.read(size)
            return data

    @contextlib.contextmanager
    def wrapper(resolved: Any) -> Iterator[Handle]:
        with real(resolved) as handle:
            yield Handle(handle)

    def cancelled(ctx: jobs.JobContext) -> bool:
        try:
            return real_cancelled(ctx)
        except PrometheosError as exc:
            if exc.code == "busy" and holders and not busy_seen:
                busy_seen.append(exc.message)  # the checkpoint saw the busy store...
                holders[0].close()  # ...and then the concurrent reader ends
            raise

    monkeypatch.setattr(sources, "open_verified", wrapper)
    monkeypatch.setattr(jobs.JobContext, "cancelled", cancelled)
    seq = env.seq()
    try:
        tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=31))
    finally:
        for reader in holders:
            with contextlib.suppress(sqlite3.Error):
                reader.close()
    assert len(busy_seen) == 1, busy_seen  # the renewal at the checkpoint was refused as busy
    (ran,) = tick["ran"]
    assert ran["state"] == "pending" and "busy" in ran["reason"], ran  # retried, not succeeded
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        saved = conn.execute("SELECT origin_reachable FROM source_checks").fetchall()
    finally:
        conn.close()
    assert saved == []  # no observation was recorded, least of all "unreachable"
    assert [n for n in notices(env) if n["category"] == "finding"] == []
    assert env.seq() == seq
    # Once the reader is gone the retry runs normally.
    monkeypatch.setattr(sources, "open_verified", real)
    retry = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=32))
    assert [r["state"] for r in retry["ran"]] == ["succeeded"]
    assert slot_rows(env, job_id)[-1]["state"] == "succeeded"


# ----- round 2: cancelling after an external request began tells the owner -----------------------


def cancelling_handler(env: Env, job_id: str, external: bool) -> Any:
    def handler(ctx: jobs.JobContext) -> jobs.Outcome:
        if external:
            ctx.before_external("synthetic request (test)")
        env.svc.job_cancel(env.project_id, AGENT, job_id, ctx.due_utc)
        assert ctx.cancelled()
        raise jobs.Cancelled()

    return handler


def test_cancelling_after_an_external_request_began_notifies_the_owner(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    monkeypatch.setitem(jobs.HANDLERS, "digest", cancelling_handler(env, job_id, True))
    seen: list[tuple[str, list[str]]] = []
    real_finish = jobs._finish

    def spying_finish(store: ProjectStore, *args: Any, **kwargs: Any) -> bool:
        seen.append((args[3], [n["category"] for n in notices(env)]))
        return real_finish(store, *args, **kwargs)

    monkeypatch.setattr(jobs, "_finish", spying_finish)
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [r["state"] for r in tick["ran"]] == ["unknown"]
    (note,) = notices(env)
    assert note["category"] == "slot_unknown" and "decision needed" in note["title"]
    assert "cannot be recalled" in note["summary"] and len(note["summary"]) < 300
    assert note["job_id"] == job_id and note["record_ids"] == [job_id]
    assert seen == [("unknown", ["slot_unknown"])]  # notice first, state second


def test_cancelling_before_any_external_request_stays_quiet(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    monkeypatch.setitem(jobs.HANDLERS, "digest", cancelling_handler(env, job_id, False))
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [r["state"] for r in tick["ran"]] == ["skipped"] and notices(env) == []


# ----- item 9: manual resolution of an unknown slot -------------------------------------------


def make_unknown(env: Env) -> tuple[str, str]:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    now = start + timedelta(minutes=16)
    unknown_after_crash(env, now, job_id)
    env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=10))
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == "unknown"
    return job_id, str(slot["due_utc"])


def exact_capability(
    env: Env, job_id: str, due: str, resolution: str, reason: str, uses: int = 1
) -> str:
    """A synthetic exact-plan owner decision for exactly this resolution (tests only)."""
    digest = env.svc.job_resolve_digest(env.project_id, job_id, due, resolution, reason)
    grant = env.svc.grant(
        env.project_id,
        operation="exact_request",
        digest=digest,
        max_uses=uses,
        label="SYNTHETIC-TEST owner decision",
        synthetic=True,
    )
    return grant.secret


def resolve(
    env: Env, job_id: str, due: str, resolution: str, reason: str, caller: Any = OWNER
) -> dict[str, Any]:
    secret = exact_capability(env, job_id, due, resolution, reason)
    out: dict[str, Any] = env.svc.job_resolve_slot(
        env.project_id, caller, job_id, due, resolution, reason, secret
    )
    return out


@pytest.mark.parametrize(
    "resolution,state", [("succeeded", "succeeded"), ("failed", "failed"), ("abandoned", "skipped")]
)
def test_owner_resolves_an_unknown_slot_with_a_reason(
    env: Env, resolution: str, state: str
) -> None:
    job_id, due = make_unknown(env)
    seq = env.seq()
    out = resolve(env, job_id, due, resolution, "checked the provider's console")
    assert out == {"job_id": job_id, "due_utc": due, "resolution": resolution, "state": state}
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == state and slot["attempts"] == 1
    assert (
        f"resolved by the owner as {resolution}: checked the provider's console" in slot["reason"]
    )
    assert slot["external_started_at"]  # the evidence that a request began is kept
    manual = slot["result"]["manual_resolution"]
    assert manual["from"] == "unknown" and manual["as"] == resolution
    assert manual["reason"] == "checked the provider's console" and manual["by"]
    assert env.seq() == seq  # slot bookkeeping: no research-visible write
    # A resolved slot is final: not run again, not resolvable twice.
    later = env.svc.jobs_tick(env.project_id, now=datetime.now(UTC) + timedelta(days=1))
    assert later["recovered"] == [] and not any(r["due_utc"] == due for r in later["ran"])
    assert next(r for r in slot_rows(env, job_id) if r["due_utc"] == due)["state"] == state
    with pytest.raises(PrometheosError) as err:
        resolve(env, job_id, due, "failed", "again")
    assert err.value.code == "validation_error" and "unknown slot" in err.value.message


def test_resolution_needs_an_exact_plan_capability_and_nothing_changes_otherwise(
    env: Env,
) -> None:
    """The write is authorised by an owner decision bound to this exact resolution; neither
    owner scope nor a read_sensitive capability is enough."""
    job_id, due = make_unknown(env)
    before = [tuple(r) for r in raw_slots(env)]
    digest = env.svc.job_resolve_digest(env.project_id, job_id, due, "succeeded", "I say so")
    for caller in (AGENT, OWNER):  # no capability: refused, and the digest to grant is named
        with pytest.raises(PrometheosError) as err:
            env.svc.job_resolve_slot(env.project_id, caller, job_id, due, "succeeded", "I say so")
        assert err.value.code == "policy_denied"
        assert err.value.details == {"operation": "exact_request", "digest": digest}
    wrong = [
        ("a read_sensitive capability", synthetic_secret(env, "read_sensitive")),
        ("bound to another reason", exact_capability(env, job_id, due, "succeeded", "different")),
        ("bound to another outcome", exact_capability(env, job_id, due, "failed", "I say so")),
        ("not a capability", "pcap_not-a-real-secret"),
    ]
    for label, secret in wrong:
        with pytest.raises(PrometheosError) as err:
            env.svc.job_resolve_slot(
                env.project_id, OWNER, job_id, due, "succeeded", "I say so", secret
            )
        assert err.value.code == "policy_denied", label
    assert [tuple(r) for r in raw_slots(env)] == before
    # The right capability works for any caller (the capability is the authority) once only.
    secret = exact_capability(env, job_id, due, "succeeded", "I say so")
    out = env.svc.job_resolve_slot(
        env.project_id, AGENT, job_id, due, "succeeded", "I say so", secret
    )
    assert out["state"] == "succeeded"
    with pytest.raises(PrometheosError) as err:
        env.svc.job_resolve_slot(
            env.project_id, AGENT, job_id, due, "succeeded", "I say so", secret
        )
    assert err.value.code == "policy_denied" and "already used" in err.value.message


def synthetic_secret(env: Env, operation: str) -> str:
    return env.svc.grant(
        env.project_id, operation=operation, digest=None, label="SYNTHETIC-TEST", synthetic=True
    ).secret


def test_a_failed_resolution_does_not_spend_the_capability(env: Env) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    (slot,) = slot_rows(env, job_id)  # a succeeded slot: not resolvable
    secret = exact_capability(env, job_id, slot["due_utc"], "failed", "no")
    with pytest.raises(PrometheosError) as err:
        env.svc.job_resolve_slot(
            env.project_id, OWNER, job_id, slot["due_utc"], "failed", "no", secret
        )
    assert err.value.code == "validation_error"
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        assert conn.execute(
            "SELECT uses FROM capabilities WHERE operation='exact_request'"
        ).fetchone() == (0,)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "resolution,reason,code",
    [("retry", "x", "validation_error"), ("succeeded", "", "validation_error"),
     ("succeeded", "   ", "validation_error"), ("failed", "x" * 501, "validation_error")],
)  # fmt: skip
def test_resolution_needs_a_known_outcome_and_a_reason(
    env: Env, resolution: str, reason: str, code: str
) -> None:
    job_id, due = make_unknown(env)
    with pytest.raises(PrometheosError) as err:
        env.svc.job_resolve_slot(env.project_id, OWNER, job_id, due, resolution, reason, "x")
    assert err.value.code == code
    assert slot_rows(env, job_id)[0]["state"] == "unknown"


def test_only_unknown_slots_can_be_resolved(env: Env) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == "succeeded"
    with pytest.raises(PrometheosError) as err:
        resolve(env, job_id, slot["due_utc"], "failed", "no")
    assert err.value.code == "validation_error" and "only an unknown slot" in err.value.message
    for job_arg, due_arg, code in ((job_id, "2000-01-01T00:00:00Z", "not_found"),
                                   ("job_00000000-0000-4000-8000-000000000000", slot["due_utc"],
                                    "not_found")):  # fmt: skip
        with pytest.raises(PrometheosError) as err:
            resolve(env, job_arg, due_arg, "failed", "x")
        assert err.value.code == code


def test_a_slot_resolved_as_succeeded_does_not_become_the_previous_result(env: Env) -> None:
    """The next run compares with the last real handler result, not the owner's note."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    real = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [r["state"] for r in real["ran"]] == ["succeeded"]
    (first,) = slot_rows(env, job_id)
    # Force the next slot unknown, then resolve it as succeeded.
    second_due = jobs.fmt(start + timedelta(minutes=30))
    execute(
        env,
        "INSERT INTO job_slots (job_id, due_utc, state, attempts, external_started_at, reason, "
        "created_at, updated_at) "
        "VALUES (?, ?, 'unknown', 1, '2026-01-01T00:00:00Z', 'x', 'a', 'a')",
        job_id,
        second_due,
    )
    resolve(env, job_id, second_due, "succeeded", "it ran")
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        previous = jobs._previous_result(conn, job_id, jobs.fmt(start + timedelta(minutes=45)))
    finally:
        conn.close()
    assert previous == first["result"]  # the first slot's real handler result


def test_every_reader_shows_an_abandoned_slot_as_abandoned_not_as_nothing_ran(env: Env) -> None:
    job_id, due = make_unknown(env)
    resolve(env, job_id, due, "abandoned", "provider confirmed nothing was sent")
    (slot,) = slot_rows(env, job_id)  # `job slots` / Service.job_slots
    assert slot["state"] == "skipped"  # the fixed state set
    assert slot["outcome"] == "abandoned"
    assert slot["manual_resolution"]["as"] == "abandoned"
    assert slot["manual_resolution"]["reason"] == "provider confirmed nothing was sent"
    assert slot["external_started_at"]  # a request did begin
    listed = env.svc.job_list(env.project_id, OWNER)["jobs"][0]  # `job list` / the hub listing
    (recent,) = listed["recent_slots"]
    assert recent["outcome"] == "abandoned" and recent["manual_resolution"]["as"] == "abandoned"
    assert listed["slot_counts"] == {"abandoned": 1}  # not counted as skipped
    # The owner's note is visible to agent scope readers of a normal job (accepted).
    agent_view = env.svc.job_slots(env.project_id, AGENT, job_id)["slots"][0]
    assert agent_view["manual_resolution"]["reason"] == "provider confirmed nothing was sent"


def test_an_ordinary_skipped_slot_keeps_its_state_as_outcome(env: Env) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    conn = env.svc.open(env.project_id).connect()
    try:
        jobs._schedule_slots(conn, start + timedelta(minutes=16))
    finally:
        conn.close()
    due = jobs.fmt(start + timedelta(minutes=15))
    env.svc.job_cancel(env.project_id, AGENT, job_id, due)
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == "skipped" and slot["outcome"] == "skipped"
    assert slot["manual_resolution"] is None
    assert env.svc.job_list(env.project_id, OWNER)["jobs"][0]["slot_counts"] == {"skipped": 1}


def test_cli_resolve_needs_an_exact_plan_capability_and_records_the_decision(
    env: Env, tmp_path: Path
) -> None:
    job_id, due = make_unknown(env)
    cli = Cli(tmp_path)
    reason = "provider confirmed nothing was sent"
    args = ("job", "resolve", job_id, "--slot", due, "--as", "abandoned", "--reason", reason,
            "--project", env.project_id)  # fmt: skip
    code, doc, _ = cli(*args)  # no capability and no terminal: an approval request (CP10.2)
    digest = env.svc.job_resolve_digest(env.project_id, job_id, due, "abandoned", reason)
    assert code == 4 and doc["error"]["code"] == "approval_required"
    assert doc["error"]["details"]["required_scope"] == "exact_request"
    assert digest  # the owner's grant binds this digest (tests/test_cp10_authority.py)
    assert slot_rows(env, job_id)[0]["state"] == "unknown"
    # An owner-scope read capability is not an exact-plan decision.
    read = tmp_path / "read.txt"
    read.write_text(synthetic_capability(tmp_path, env.project_id, "read_sensitive"), "utf-8")
    code, doc, _ = cli(*args, "--capability-file", str(read))
    assert code == 4 and doc["error"]["code"] == "policy_denied"
    assert slot_rows(env, job_id)[0]["state"] == "unknown"
    cap = tmp_path / "cap.txt"
    cap.write_text(exact_capability(env, job_id, due, "abandoned", reason), "utf-8")
    code, doc, _ = cli(*args, "--capability-file", str(cap))
    assert code == 0, doc
    assert doc["state"] == "skipped" and doc["resolution"] == "abandoned"
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == "skipped" and "provider confirmed" in slot["reason"]
    assert slot["outcome"] == "abandoned"
