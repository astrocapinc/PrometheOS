"""Decision 0027: the scheduler holds no admitted store connection while a handler works.

With decision 0025 a live connection is also the project's admission lease, so a tick that
kept one open across a handler blocked every other caller (cancellation, status, a second
tick) for the handler's whole duration. These controls run the real service from real
threads: a handler blocks on an event (holding no connection) while another caller reads the
slot, cancels it or recovers it. Waits are bounded events, never sleeps; every thread is
owned, joined and has its exception kept.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry
from cp7_support import PaperProject, text_evidence
from test_cp7_jobs import add, budget, interval, job, minute_now, slot_rows

from prometheos.app import jobs, reads, sources
from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.store import admission
from prometheos.store.project import ProjectStore

WAIT = 30.0  # seconds; a bound for events that normally fire within milliseconds


@pytest.fixture
def env(tmp_path: Any) -> Env:
    return Env(tmp_path)


class Owned(threading.Thread):
    """A thread this test owns: it keeps its result or exception, and sets ``ended`` however
    it ends, so the main thread never waits on an event the thread can no longer set."""

    def __init__(self, target: Callable[[], Any], *ended: threading.Event) -> None:
        super().__init__(daemon=True)
        self.work = target
        self.ended = ended
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self.work()
        except BaseException as exc:  # kept for the main thread's assertions
            self.error = exc
        finally:
            for event in self.ended:
                event.set()


def holds_admission() -> bool:
    """Whether this thread holds a project admission lease (a live admitted connection)."""
    try:
        admission.require_before_maintenance()
    except PrometheosError:
        return True
    return False


def store_read(env: Env, sql: str, *params: Any) -> list[sqlite3.Row]:
    """A short read through the real admitted store (never a raw writable handle)."""
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def store_write(env: Env, *statements: tuple[str, tuple[Any, ...]]) -> None:
    """One write transaction through the real admitted store (never a raw writable handle);
    the caller holds no admitted connection on this thread."""
    conn = env.svc.open(env.project_id).connect()
    try:
        with conn:
            for sql, params in statements:
                conn.execute(sql, params)
    finally:
        conn.close()


def notifications(env: Env, job_id: str, due: str) -> list[tuple[str, str]]:
    rows = store_read(
        env,
        "SELECT category, title FROM job_notifications WHERE job_id = ? AND due_utc = ? "
        "ORDER BY id",
        job_id,
        due,
    )
    return [(r["category"], r["title"]) for r in rows]


def request_keys(env: Env, job_id: str, due: str, *labels: str) -> list[int]:
    """How many commits each slot-derived request key recorded."""
    return [
        len(
            store_read(
                env,
                "SELECT request_id FROM requests WHERE request_id = ?",
                jobs.slot_request_id(job_id, f"{due}:{label}"),
            )
        )
        for label in labels
    ]


def finding(ctx: jobs.JobContext, n: int) -> jobs.Outcome:
    """What an attempt produces if it is allowed to finish: a result and a finding."""
    return jobs.Outcome(
        {"usage": jobs.USAGE, "attempt": n},
        f"digest-{n}",
        jobs.Finding(title=f"Attempt {n} result", summary="synthetic", record_ids=[ctx.job["id"]]),
    )


# ----- (a) a real concurrent cancellation ----------------------------------------------------


@pytest.mark.parametrize("external", [False, True], ids=["local", "marked_external"])
def test_a_concurrent_cancellation_reaches_a_blocked_handler(
    env: Env, monkeypatch: pytest.MonkeyPatch, external: bool
) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    due = jobs.fmt(start + timedelta(minutes=15))
    entered, release = threading.Event(), threading.Event()
    seen: dict[str, Any] = {"calls": 0, "effects": 0}

    def blocking(ctx: jobs.JobContext) -> jobs.Outcome:
        seen["calls"] += 1
        if external:
            ctx.before_external("synthetic scripted request (test)")
            seen["effects"] += 1  # the one scripted effect, after the durable mark
        seen["admission_held"] = holds_admission()
        entered.set()
        if not release.wait(WAIT):
            raise AssertionError("the handler was never released")
        if ctx.cancelled():
            raise jobs.Cancelled()
        return finding(ctx, 1)

    monkeypatch.setitem(jobs.HANDLERS, "digest", blocking)
    seq = env.seq()
    ticker = Owned(lambda: env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16)),
                   entered)  # fmt: skip
    ticker.start()
    try:
        assert entered.wait(WAIT), "the handler was never entered"
        assert ticker.is_alive() and ticker.error is None, ticker.error
        # Another caller, through the real service, while the handler is blocked.
        (slot,) = slot_rows(env, job_id)
        assert (slot["due_utc"], slot["state"], slot["attempts"]) == (due, "running", 1)
        cancel = env.svc.job_cancel(env.project_id, AGENT, job_id, due)
        assert cancel["outcome"] == "cancel_requested"
        assert ticker.is_alive()  # cancellation completed while the handler stayed blocked
        (row,) = store_read(
            env, "SELECT cancel_requested, attempts, state FROM job_slots WHERE job_id = ?", job_id
        )
        assert tuple(row) == (1, 1, "running")
        # A second tick is refused by the durable scheduler lease, not by admission.
        with pytest.raises(PrometheosError) as busy:
            env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=17))
        assert busy.value.code == "busy" and "until" in busy.value.details, busy.value
    finally:
        release.set()
        ticker.join(WAIT * 2)
    assert not ticker.is_alive() and ticker.error is None, ticker.error
    assert seen["admission_held"] is False  # the handler ran with no admitted connection
    expected = "unknown" if external else "skipped"
    assert [r["state"] for r in ticker.result["ran"]] == [expected]
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == expected and slot["attempts"] == 1 and seen["calls"] == 1
    categories = [c for c, _ in notifications(env, job_id, due)]
    if external:
        assert "cannot be recalled" in slot["reason"] and seen["effects"] == 1
        assert categories == ["slot_unknown"] and env.seq() == seq + 1  # the notice only
        later = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=26))
        assert later["ran"] == [] and later["recovered"] == []  # never retried
        assert seen["calls"] == 1 and seen["effects"] == 1
        assert slot_rows(env, job_id)[0]["state"] == "unknown"
    else:
        assert slot["reason"] == "cancelled" and seen["effects"] == 0
        assert categories == [] and env.seq() == seq  # no finding, no output commit


# ----- (b) an old handler released after its slot was recovered ------------------------------


@pytest.mark.parametrize("effect", [False, True], ids=["before_effect", "after_effect"])
def test_an_old_handler_released_after_recovery_reports_lease_lost(
    env: Env, monkeypatch: pytest.MonkeyPatch, effect: bool
) -> None:
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    due = jobs.fmt(start + timedelta(minutes=15))
    first = start + timedelta(minutes=16)
    recovery = first + timedelta(minutes=5)  # beyond both leases of the first tick
    gates = {n: (threading.Event(), threading.Event()) for n in (1, 2)}  # entered, release
    owners: list[str] = []
    effects: list[str] = []

    def blocking(ctx: jobs.JobContext) -> jobs.Outcome:
        owners.append(ctx.owner)
        n = len(owners)
        if effect and n == 1:
            ctx.before_external("synthetic scripted request (test)")
            effects.append(ctx.owner)
        entered, release = gates[n]
        entered.set()
        if not release.wait(WAIT):
            raise AssertionError(f"attempt {n} was never released")
        return finding(ctx, n)

    monkeypatch.setitem(jobs.HANDLERS, "digest", blocking)
    seq = env.seq()
    old = Owned(lambda: env.svc.jobs_tick(env.project_id, now=first), gates[1][0])
    replacement: Owned | None = None
    old.start()
    try:
        assert gates[1][0].wait(WAIT) and old.is_alive(), old.error
        recovered = env.svc.jobs_tick(env.project_id, now=recovery)
        assert recovered["ran"] == []
        assert [(r["due_utc"], r["state"]) for r in recovered["recovered"]] == [
            (due, "unknown" if effect else "pending")
        ]
        if not effect:
            # The retry is claimed by another tick and is itself still running.
            replacement = Owned(
                lambda: env.svc.jobs_tick(env.project_id, now=recovery + timedelta(seconds=31)),
                gates[2][0],
            )
            replacement.start()
            assert gates[2][0].wait(WAIT) and replacement.is_alive(), replacement.error
        gates[1][1].set()  # release the old handler: it wants to record its finding
        old.join(WAIT * 2)
        assert not old.is_alive() and old.error is None, old.error
        assert old.result["ran"] == [
            {"job_id": job_id, "due_utc": due, "state": "lease_lost", "lost": "slot"}
        ]
        (row,) = store_read(
            env,
            "SELECT state, lease_owner, attempts, reason FROM job_slots WHERE job_id = ?",
            job_id,
        )
        scheduler = store_read(env, "SELECT owner FROM scheduler_lease")
        if effect:
            assert (row["state"], row["lease_owner"], row["attempts"]) == ("unknown", None, 1)
            assert "not repeated" in row["reason"] and scheduler == []
        else:
            # The replacement's slot and scheduler leases survive the old tick's completion.
            assert (row["state"], row["lease_owner"], row["attempts"]) == (
                "running",
                owners[1],
                2,
            )
            assert [r["owner"] for r in scheduler] == [owners[1]]
        assert all(c != "finding" for c, _ in notifications(env, job_id, due))
    finally:
        for _, release in gates.values():
            release.set()
        old.join(WAIT * 2)
        if replacement is not None:
            replacement.join(WAIT * 2)
    if effect:
        assert effects == owners == [owners[0]]  # exactly one scripted effect
        assert notifications(env, job_id, due) == [
            ("slot_unknown", "Job slot unknown: decision needed")
        ]
        assert request_keys(env, job_id, due, "slot-unknown", "finding") == [1, 0]
        assert env.seq() == seq + 1
        later = env.svc.jobs_tick(env.project_id, now=recovery + timedelta(minutes=2))
        assert later["ran"] == [] and later["recovered"] == [] and len(owners) == 1
        assert slot_rows(env, job_id)[0]["state"] == "unknown"
    else:
        assert replacement is not None
        assert not replacement.is_alive() and replacement.error is None, replacement.error
        assert [(r["state"], r.get("finding")) for r in replacement.result["ran"]] == [
            ("succeeded", True)
        ]
        (slot,) = slot_rows(env, job_id)
        assert (slot["state"], slot["attempts"], slot["result"]["attempt"]) == ("succeeded", 2, 2)
        assert notifications(env, job_id, due) == [("finding", "Attempt 2 result")]
        assert request_keys(env, job_id, due, "finding") == [1]
        assert env.seq() == seq + 1 and effects == []


# ----- every completion branch honours a lost slot -------------------------------------------


def take_over(env: Env, job_id: str) -> None:
    """Another tick recovered and re-claimed the slot (and holds the scheduler lease)."""
    store_write(
        env,
        (
            "UPDATE job_slots SET lease_owner = 'tick:replacement', attempts = attempts + 1 "
            "WHERE job_id = ? AND state = 'running'",
            (job_id,),
        ),
        ("UPDATE scheduler_lease SET owner = 'tick:replacement'", ()),
    )


@pytest.mark.parametrize("branch", ["finding", "quiet", "error", "cancelled"])
def test_every_completion_branch_honours_a_lost_slot(
    env: Env, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    """The slot is taken over after the handler ended and before this tick records anything:
    nothing is committed or overwritten, and the tick says ``lease_lost``. ``quiet`` is a
    success without a finding, where only the owner-qualified finish can notice the loss."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    due = jobs.fmt(start + timedelta(minutes=15))

    def handler(ctx: jobs.JobContext) -> jobs.Outcome:
        if branch == "error":
            raise RuntimeError("synthetic failure")
        if branch == "cancelled":
            raise jobs.Cancelled()
        if branch == "quiet":
            return jobs.Outcome({"usage": jobs.USAGE, "attempt": 1}, "quiet-1")
        return finding(ctx, 1)

    real_commit, real_finish = jobs._commit_finding, jobs._finish

    def commit_after_takeover(*args: Any, **kwargs: Any) -> Any:
        take_over(env, job_id)
        return real_commit(*args, **kwargs)

    def finish_after_takeover(*args: Any, **kwargs: Any) -> Any:
        take_over(env, job_id)
        return real_finish(*args, **kwargs)

    monkeypatch.setitem(jobs.HANDLERS, "digest", handler)
    if branch == "finding":
        monkeypatch.setattr(jobs, "_commit_finding", commit_after_takeover)
    else:
        monkeypatch.setattr(jobs, "_finish", finish_after_takeover)
    seq = env.seq()
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert notifications(env, job_id, due) == [] and env.seq() == seq  # nothing committed
    (row,) = store_read(
        env,
        "SELECT state, lease_owner, attempts, reason, result FROM job_slots WHERE job_id = ?",
        job_id,
    )
    assert tuple(row)[:4] == ("running", "tick:replacement", 2, None)  # the replacement's record
    assert "attempt" not in json.loads(row["result"] or "{}")  # this attempt wrote no result
    assert [r["owner"] for r in store_read(env, "SELECT owner FROM scheduler_lease")] == [
        "tick:replacement"
    ]
    assert [(r["state"], r.get("lost")) for r in tick["ran"]] == [("lease_lost", "slot")]


@pytest.mark.parametrize(
    ("change", "expected"),
    [("cancelled", "not_claimed"), ("retry_later", "not_claimed"), ("cancel_flag", "skipped")],
)
def test_a_candidate_changed_after_selection_is_read_again_before_its_claim(
    env: Env, monkeypatch: pytest.MonkeyPatch, change: str, expected: str
) -> None:
    """A tick selects its candidates first; a row it selected is not claim authority. Job A's
    slot runs first and, through the real service, changes job B's selected slot before B's
    turn: B is re-read in its claim transaction and neither runs nor counts an attempt."""
    start = minute_now()
    job_a = add(env, job("digest", interval(15, start)))
    job_b = add(env, job("digest", interval(15, start + timedelta(minutes=1))))
    due_b = jobs.fmt(start + timedelta(minutes=16))
    ran: list[str] = []

    def handler(ctx: jobs.JobContext) -> jobs.Outcome:
        ran.append(ctx.job["id"])
        if ctx.job["id"] == job_a:
            if change == "cancelled":
                env.svc.job_cancel(env.project_id, AGENT, job_b, due_b)
            else:
                column = "next_attempt_at" if change == "retry_later" else "cancel_requested"
                value: Any = "2999-01-01T00:00:00.000000Z" if change == "retry_later" else 1
                conn = env.svc.open(env.project_id).connect()
                try:
                    with conn:
                        conn.execute(
                            f"UPDATE job_slots SET {column} = ? WHERE job_id = ?",  # noqa: S608
                            (value, job_b),
                        )
                finally:
                    conn.close()
        return jobs.Outcome({"usage": jobs.USAGE}, "x")

    monkeypatch.setitem(jobs.HANDLERS, "digest", handler)
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=17))
    assert [(r["job_id"], r["state"]) for r in tick["ran"]] == [
        (job_a, "succeeded"),
        (job_b, expected),
    ]
    assert ran == [job_a]  # B's handler never ran
    (slot,) = [s for s in slot_rows(env, job_b) if s["due_utc"] == due_b]
    assert slot["attempts"] == 0
    state = {"cancelled": "skipped", "retry_later": "pending", "cancel_flag": "skipped"}[change]
    assert slot["state"] == state
    if state == "skipped":
        assert slot["reason"] == "cancelled"


def test_the_tick_and_its_handler_hold_no_admission_between_steps(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-thread callers inside a handler use ordinary guarded open and identity: a nested
    service read, a nested tick (refused by the scheduler lease) and a status read all work."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    seen: dict[str, Any] = {}

    def nested(ctx: jobs.JobContext) -> jobs.Outcome:
        seen["admission_held"] = holds_admission()
        seen["slots"] = [s["state"] for s in slot_rows(env, job_id)]
        try:
            env.svc.jobs_tick(env.project_id, now=ctx.now + timedelta(seconds=1))
        except PrometheosError as exc:
            seen["nested_tick"] = (exc.code, "until" in exc.details)
        seen["inbox"] = env.svc.inbox(env.project_id, OWNER)["items"]
        return jobs.Outcome({"usage": jobs.USAGE}, "x")

    monkeypatch.setitem(jobs.HANDLERS, "digest", nested)
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [r["state"] for r in tick["ran"]] == ["succeeded"]
    assert seen == {
        "admission_held": False,
        "slots": ["running"],
        "nested_tick": ("busy", True),
        "inbox": [],
    }
    assert holds_admission() is False  # the finished tick left nothing admitted


# ----- the error branch when the slot was recovered before the completion read --------------

RECOVERED = "lease expired after an external request began; recovered by another tick (test)"


def test_a_failing_handler_whose_slot_was_recovered_meanwhile_adds_no_notice(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempt 3 marks an external request and then fails. Before this tick reads the slot
    again, another tick recovered it as ``unknown`` and notified the owner. The old owner
    reports ``lease_lost`` and adds nothing: no contradictory ``slot_failed`` notice beside
    the ``unknown`` one, and no state."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    due = jobs.fmt(start + timedelta(minutes=15))
    owners: list[str] = []

    def handler(ctx: jobs.JobContext) -> jobs.Outcome:
        owners.append(ctx.owner)
        if len(owners) < 3:
            raise RuntimeError("synthetic failure before any external request")
        ctx.before_external("synthetic scripted request (test)")
        # Meanwhile another tick recovers the slot: notice first, then the state.
        jobs._notify_problem(ctx.store, job_id, due, "unknown", RECOVERED, "tick:other")
        store_write(
            env,
            (
                "UPDATE job_slots SET state = 'unknown', lease_owner = NULL, "
                "lease_expires_at = NULL, reason = ? WHERE job_id = ? AND state = 'running'",
                (RECOVERED, job_id),
            ),
        )
        raise RuntimeError("synthetic failure after the external mark")

    monkeypatch.setitem(jobs.HANDLERS, "digest", handler)
    for minutes in (16, 17):  # attempts 1 and 2 fail; backoff 30 s, then 60 s
        tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=minutes))
        assert [r["state"] for r in tick["ran"]] == ["pending"]
    seq = env.seq()
    third = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=19))
    assert len(owners) == 3
    assert notifications(env, job_id, due) == [
        ("slot_unknown", "Job slot unknown: decision needed")
    ]
    assert env.seq() == seq + 1  # the other tick's notice only
    (slot,) = slot_rows(env, job_id)
    assert (slot["state"], slot["attempts"], slot["reason"]) == ("unknown", 3, RECOVERED)
    assert [(r["state"], r.get("lost")) for r in third["ran"]] == [("lease_lost", "slot")]


# ----- a purge between a handler's read and its completion -----------------------------------


def purge_now(env: Env, *targets: str) -> list[str]:
    """An owner purge (synthetic capability) as the hub or CLI commits it; returns its closure."""
    plan = env.svc.purge_plan(env.project_id, list(targets), OWNER)
    grant = env.svc.grant(
        env.project_id,
        operation="purge",
        digest=plan["plan_sha256"],
        label="SYNTHETIC-TEST purge",
        synthetic=True,
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    return list(plan["closure"])


def named_anywhere(env: Env, record_id: str) -> list[str]:
    """Job notifications and slot details that name ``record_id`` (purge removes them all)."""
    texts = [
        f"{r['title']} {r['summary']} {r['record_ids']}"
        for r in store_read(env, "SELECT title, summary, record_ids FROM job_notifications")
    ] + [
        f"{r['result']} {r['reason']}"
        for r in store_read(env, "SELECT result, reason FROM job_slots")
    ]
    return [t for t in texts if record_id in t]


@pytest.mark.parametrize("case", ["race_only_record", "race_one_of_two", "after_purge"])
def test_a_digest_never_names_a_record_purged_before_its_finding_commits(
    env: Env, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """An owner purge can commit between the digest's read and its finding, because the
    handler holds no connection. Purge removes every notification naming a purged record, so
    the finding must not name one afterwards: a purged ID is dropped, and a finding left
    listing nothing is withheld (the slot still succeeds, quietly). ``after_purge``: no race;
    the digest's range includes the purge's own revision of the record, and a purged record
    is outside the digest's query (r3): nothing to list, count or withhold."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    doomed = create_entry(title="Entry to purge")
    env.apply([doomed])
    kept = create_entry(title="Entry that stays")
    if case == "race_one_of_two":
        env.apply([kept])
    if case == "after_purge":
        purge_now(env, doomed["id"])
    else:
        steps: list[str] = []
        real_seq = jobs.project_seq
        real_checkpoint = jobs.JobContext.checkpoint

        def spy_seq(conn: sqlite3.Connection) -> int:
            steps.append("read")  # inside the digest's read snapshot
            return int(real_seq(conn))

        def checkpoint(ctx: jobs.JobContext) -> None:
            if steps == ["read"]:  # the first moment another caller is admitted
                steps.append("purge")
                assert purge_now(env, doomed["id"]) == [doomed["id"]]
            real_checkpoint(ctx)

        monkeypatch.setattr(jobs, "project_seq", spy_seq)
        monkeypatch.setattr(jobs.JobContext, "checkpoint", checkpoint)
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert named_anywhere(env, doomed["id"]) == []
    (slot,) = slot_rows(env, job_id)
    assert slot["state"] == "succeeded"
    notes = store_read(env, "SELECT record_ids FROM job_notifications WHERE job_id = ?", job_id)
    if case == "race_one_of_two":
        assert [json.loads(r["record_ids"]) for r in notes] == [[kept["id"]]]
        assert tick["ran"][0]["finding"] is True
    else:
        assert notes == [] and tick["ran"][0]["finding"] is False
        if case == "after_purge":
            assert slot["result"]["kinds"] == {} and "finding_withheld" not in slot["result"]
        else:
            assert slot["result"]["finding_withheld"] == jobs.WITHHELD


def test_a_refresh_never_records_a_version_purged_while_it_was_hashed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observed version is purged while the origin is hashed (no connection is held). The
    finding names it, so nothing of it is committed (no notification, no source check), and
    the slot's result, which names it too, is recorded as purge records slot details."""
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    source_id, version_id = item["source_id"], item["version_id"]
    start = minute_now()
    job_id = add(env, job("source_refresh", interval(15, start), {"source_ids": [source_id]}))
    (env.research / "baseline.md").write_bytes(b"changed origin bytes\n")
    real_observe = sources._observe
    closure: list[str] = []

    def observe(store: Any, settings: Any, target: Any, progress: Any = None) -> Any:
        if not closure:  # after the short read, before hashing
            closure.extend(purge_now(env, version_id))
        return real_observe(store, settings, target, progress)

    monkeypatch.setattr(sources, "_observe", observe)
    seq = env.seq()
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert version_id in closure and job_id not in closure  # the job survives the purge
    assert env.seq() == seq + 1  # the purge only
    assert named_anywhere(env, version_id) == []
    checks = store_read(env, "SELECT COUNT(*) AS n FROM source_checks WHERE version_id = ?",
                        version_id)  # fmt: skip
    assert checks[0]["n"] == 0
    (slot,) = slot_rows(env, job_id)
    assert (slot["state"], slot["result"], slot["reason"]) == (
        "succeeded",
        None,
        "details removed by purge",
    )
    assert [(r["state"], r["finding"]) for r in tick["ran"]] == [("succeeded", False)]


# ----- a refused completion -----------------------------------------------------------------


def consistent(env: Env, job_id: str, due: str) -> tuple[str, int]:
    """The slot's finding is committed exactly when it succeeded, at most once, and no
    failure notice stands beside it. Returns the slot state and the number of findings."""
    (slot,) = slot_rows(env, job_id)
    categories = [c for c, _ in notifications(env, job_id, due)]
    found = categories.count("digest")
    assert found <= 1, categories
    assert (found == 1) == (slot["state"] == "succeeded"), (slot, categories)
    assert not (found and "slot_failed" in categories), categories
    return str(slot["state"]), found


@pytest.mark.parametrize("refused", [1, 2, 3])
@pytest.mark.parametrize("attempt", [1, 3])
def test_a_refused_admission_after_the_handler_never_splits_a_finding_from_its_success(
    env: Env, monkeypatch: pytest.MonkeyPatch, attempt: int, refused: int
) -> None:
    """The ``refused``-th store admission after the handler returned is refused as busy (as an
    external writable handle makes it, decision 0025): the post-handler renewal, the
    completion or the final release. Whatever it hits, the record stays consistent: the
    finding is committed exactly when the slot succeeded, an attempt that recorded nothing
    is retried (or at the limit fails, without a finding), and later ticks finish it."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    due = jobs.fmt(start + timedelta(minutes=15))
    env.apply([create_entry(title="Something changed")])
    seq = env.seq()
    real_digest = jobs.HANDLERS["digest"]
    real_connect = ProjectStore.connect
    state: dict[str, Any] = {"calls": 0, "armed": None}

    def connect(self: ProjectStore, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if state["armed"] is not None:
            state["armed"] += 1
            if state["armed"] == refused:
                state["armed"] = None
                raise PrometheosError(
                    "busy", "synthetic admission refusal (test)", {"lock": "project_admission"}
                )
        return real_connect(self, *args, **kwargs)

    def handler(ctx: jobs.JobContext) -> jobs.Outcome:
        state["calls"] += 1
        if state["calls"] < attempt:
            raise RuntimeError("synthetic failure of an earlier attempt")
        outcome = real_digest(ctx)
        if state["calls"] == attempt:
            state["armed"] = 0  # count admissions from here
        return outcome

    monkeypatch.setitem(jobs.HANDLERS, "digest", handler)
    monkeypatch.setattr(ProjectStore, "connect", connect)
    minutes = [16, 17, 19][:attempt]
    for m in minutes[:-1]:
        env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=m))
    armed_at = start + timedelta(minutes=minutes[-1])
    with contextlib.suppress(PrometheosError):
        env.svc.jobs_tick(env.project_id, now=armed_at)
    state["armed"] = None
    consistent(env, job_id, due)
    for later in (5, 6, 7, 8):  # beyond both leases of the refused tick
        slot_state, _ = consistent(env, job_id, due)
        if slot_state in ("succeeded", "failed"):
            break
        env.svc.jobs_tick(env.project_id, now=armed_at + timedelta(minutes=later))
    slot_state, found = consistent(env, job_id, due)
    assert slot_state in ("succeeded", "failed")
    if found:
        assert env.svc.get(env.project_id, OWNER, job_id, None)["body"]["last_slot"] == due
    failure_notices = 0 if found else int(slot_state == "failed")
    assert env.seq() == seq + found + failure_notices


def test_a_refused_final_release_keeps_the_tick_result(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick's last step, releasing the scheduler lease, is refused as busy. What the tick
    recorded is returned; the owner-qualified lease simply expires."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    env.apply([create_entry(title="Something changed")])
    real_release = jobs._release

    def refused(conn: sqlite3.Connection, owner: str) -> None:
        raise PrometheosError("busy", "synthetic release refusal (test)", {})

    monkeypatch.setattr(jobs, "_release", refused)
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    monkeypatch.setattr(jobs, "_release", real_release)
    assert [(r["state"], r["finding"]) for r in tick["ran"]] == [("succeeded", True)]
    assert slot_rows(env, job_id)[0]["state"] == "succeeded"
    with pytest.raises(PrometheosError) as busy:  # until the lease expires
        env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=17))
    assert busy.value.code == "busy" and "until" in busy.value.details
    later = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=21))
    assert later["ran"] == [] and later["recovered"] == []


def test_a_store_still_busy_at_completion_leaves_the_slot_to_recovery(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent raw reader (busy for any new admission, decision 0025) appears while a
    refresh hashes and stays past the tick. The checkpoint fails the attempt and its
    completion cannot be admitted either: the tick raises ``busy``, nothing is recorded, the
    slot stays running, and recovery after its lease retries it."""
    monkeypatch.setattr(jobs, "CHECKPOINT_SECONDS", 0.0)
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    start = minute_now()
    job_id = add(
        env, job("source_refresh", interval(15, start), {"source_ids": [item["source_id"]]})
    )
    assert env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))["quiet"]
    due = jobs.fmt(start + timedelta(minutes=30))
    holders: list[sqlite3.Connection] = []
    real = sources.open_verified

    class Handle:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def read(self, size: int = -1) -> bytes:
            if not holders:
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

    monkeypatch.setattr(sources, "open_verified", wrapper)
    seq = env.seq()
    try:
        with pytest.raises(PrometheosError) as busy:
            env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=31))
        assert busy.value.code == "busy" and holders
    finally:
        for reader in holders:
            reader.close()
    monkeypatch.setattr(sources, "open_verified", real)
    (slot,) = [s for s in slot_rows(env, job_id) if s["due_utc"] == due]
    assert (slot["state"], slot["attempts"]) == ("running", 1)
    assert store_read(env, "SELECT COUNT(*) AS n FROM source_checks")[0]["n"] == 0
    assert notifications(env, job_id, due) == [] and env.seq() == seq
    recovery = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=36))
    assert [(r["due_utc"], r["state"]) for r in recovery["recovered"]] == [(due, "pending")]
    retry = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=37))
    assert [(r["due_utc"], r["state"]) for r in retry["ran"]] == [(due, "succeeded")]
    (slot,) = [s for s in slot_rows(env, job_id) if s["due_utc"] == due]
    assert (slot["state"], slot["attempts"]) == ("succeeded", 2)


# ----- a finding an earlier attempt recorded -----------------------------------------------------


@pytest.mark.parametrize("left", ["running", "pending"])
def test_a_finding_recorded_by_an_earlier_attempt_finishes_its_slot_once(
    env: Env, monkeypatch: pytest.MonkeyPatch, left: str
) -> None:
    """A store from before decision 0027 can hold a slot whose finding an earlier attempt
    committed while its completion was never recorded: left ``running`` (the process died) or
    ``pending`` (a busy finish was taken for a handler failure). It is finished as succeeded,
    without running the handler again, counting an attempt or adding a notice."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    now = start + timedelta(minutes=16)
    due = jobs.fmt(start + timedelta(minutes=15))
    store = env.svc.open(env.project_id)
    with contextlib.closing(store.connect()) as conn:
        jobs._acquire(conn, "tick:old", now)
        jobs._schedule_slots(conn, now)
        assert jobs._claim(conn, job_id, due, "tick:old", now)
        jobs._release(conn, "tick:old")
    earlier = jobs.Finding(title="Earlier attempt's finding", summary="synthetic",
                           record_ids=[job_id])  # fmt: skip
    jobs._commit_finding(store, job_id, due, earlier, "test")
    if left == "pending":
        assert jobs._finish(store, job_id, due, "tick:old", "pending",
                            reason="attempt 1 failed (busy); will retry")  # fmt: skip
    calls: list[str] = []
    monkeypatch.setitem(jobs.HANDLERS, "digest", lambda ctx: calls.append("ran") or 1 / 0)
    seq = env.seq()
    tick = env.svc.jobs_tick(env.project_id, now=now + timedelta(minutes=5))
    finished = tick["recovered"] if left == "running" else tick["ran"]
    assert [r["state"] for r in finished] == ["succeeded"] and calls == []
    assert env.seq() == seq
    (slot,) = [s for s in slot_rows(env, job_id) if s["due_utc"] == due]
    assert (slot["state"], slot["attempts"]) == ("succeeded", 1)
    assert slot["reason"] == jobs.RECORDED_EARLIER
    if left == "pending":
        assert tick["ran"][0]["finding"] is True
    assert notifications(env, job_id, due) == [("finding", "Earlier attempt's finding")]
    with contextlib.closing(store.connect(readonly=True)) as conn:  # not a result to compare with
        assert jobs._previous_result(conn, job_id, jobs.fmt(now + timedelta(days=1))) is None


# ----- older branches: a retry after its job was disabled; per-slot scheduler renewal --------


def test_a_retry_after_its_job_was_disabled_mid_run_is_skipped(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling skips only pending slots; a running one that then fails returns to pending.
    Its retry re-reads the job in the claim transaction and is skipped, never run."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    calls: list[str] = []

    def handler(ctx: jobs.JobContext) -> jobs.Outcome:
        calls.append(ctx.due_utc)
        env.svc.job_set_enabled(env.project_id, AGENT, job_id, 1, False, new_request_id())
        raise RuntimeError("synthetic failure after the job was disabled")

    monkeypatch.setitem(jobs.HANDLERS, "digest", handler)
    first = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [r["state"] for r in first["ran"]] == ["pending"]
    retry = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=17))
    assert [r["state"] for r in retry["ran"]] == ["skipped"] and len(calls) == 1
    (slot,) = slot_rows(env, job_id)
    assert (slot["state"], slot["reason"], slot["attempts"]) == ("skipped", "job disabled", 1)


def test_each_slot_renews_the_scheduler_lease_before_it_runs(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing handler gets no post-handler renewal; the tick renews the scheduler lease
    before the next slot, so a long pass keeps it (here it had already expired)."""
    start = minute_now()
    job_a = add(env, job("digest", interval(15, start)))
    job_b = add(env, job("digest", interval(15, start + timedelta(minutes=1))))

    def handler(ctx: jobs.JobContext) -> jobs.Outcome:
        if ctx.job["id"] == job_a:
            store_write(
                env, ("UPDATE scheduler_lease SET expires_at = '1970-01-01T00:00:00.000000Z'", ())
            )
            raise RuntimeError("synthetic failure after a long pause")
        ctx.cancelled()  # renews both leases, or raises LeaseLost
        return jobs.Outcome({"usage": jobs.USAGE}, "x")

    monkeypatch.setitem(jobs.HANDLERS, "digest", handler)
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=17))
    assert [(r["job_id"], r["state"]) for r in tick["ran"]] == [
        (job_a, "pending"),
        (job_b, "succeeded"),
    ]


def test_a_manual_source_check_hashes_with_no_admitted_connection(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual ``source check`` reads its targets in one connection and hashes them afterwards
    with none open, like the scheduled refresh, so a long check never holds the project's
    admission against other callers (decision 0027)."""
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    held: list[bool] = []
    real_observe = sources._observe

    def observe(store: Any, settings: Any, target: Any, progress: Any = None) -> Any:
        held.append(holds_admission())
        return real_observe(store, settings, target, progress)

    monkeypatch.setattr(sources, "_observe", observe)
    for save in (False, True):
        out = env.svc.source_check(env.project_id, [item["source_id"]], save=save)
        assert [o["version_id"] for o in out["observations"]] == [item["version_id"]]
        assert out["observations"][0]["origin_matches_version"] is True
    assert held == [False, False]


def test_a_scheduled_paper_check_hashes_its_media_before_its_snapshot(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paper check reads in one snapshot, and on a cold verification cache verifying its
    retained media meant hashing inside it, holding the project's admission for seconds per
    GiB. The media are now hashed first with no admitted connection, so inside the snapshot
    every verification is a cache hit (decision 0027)."""
    pp = PaperProject(env)
    (env.research / "dataset.csv").write_bytes(b"t,value\n" + b"0.5,1.25\n" * (1024 * 1024))
    (item,) = env.import_files(["dataset.csv"])["plan"]["items"]
    assert env.svc.get(env.project_id, AGENT, item["version_id"], None)["body"]["storage"] == (
        "media"
    )
    rows = pp.svc.requirements_draft(pp.pid, AGENT, pp.venue["version_id"])["requirements"]
    cited = dict(
        rows[0],
        applicability="applicable",
        status="met",
        last_checked_at="2026-10-04T10:00:00Z",
        evidence=[text_evidence(item, 1)],
    )
    paper = pp.paper(
        venue_source_refs=[{"id": pp.venue["version_id"], "rev": 1}],
        requirements=[cited, *rows[1:]],
    )
    shutil.rmtree(env.store_dir / "cache", ignore_errors=True)  # a cold verification cache
    calls: list[tuple[bool, bool]] = []
    real = reads._media_verified

    def verified(store_dir: Any, key: str, sha256: str, byte_length: int) -> bool:
        cache = store_dir / "cache" / "media-verify.json"
        hit = cache.is_file() and key in json.loads(cache.read_text("utf-8"))
        calls.append((holds_admission(), hit))
        return bool(real(store_dir, key, sha256, byte_length))

    monkeypatch.setattr(reads, "_media_verified", verified)
    start = minute_now()
    add(env, job("paper_check", interval(60, start), {"paper_id": paper}))
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=61))
    assert [r["state"] for r in tick["ran"]] == ["succeeded"]
    assert (False, False) in calls  # hashed once, with no admitted connection
    assert calls and all(hit for held, hit in calls if held), calls  # snapshot: cache hits only


# ----- r3: purged records are redacted, not a reason to lose a finding (review r2 N1-N7) -----

PURGED = "[purged record]"  # the neutral token a purged ID becomes in a finding's text


def digest_race_purge(monkeypatch: pytest.MonkeyPatch, env: Env, record_id: str) -> list[str]:
    """Purge ``record_id`` right after the next digest's read snapshot closes (the first moment
    another caller is admitted); returns the steps seen, so the caller can check it happened."""
    steps: list[str] = []
    real_seq = jobs.project_seq
    real_checkpoint = jobs.JobContext.checkpoint

    def spy_seq(conn: sqlite3.Connection) -> int:
        steps.append("read")  # inside the digest's read snapshot
        return int(real_seq(conn))

    def checkpoint(ctx: jobs.JobContext) -> None:
        if steps == ["read"]:
            steps.append("purge")
            assert purge_now(env, record_id) == [record_id]
        real_checkpoint(ctx)

    monkeypatch.setattr(jobs, "project_seq", spy_seq)
    monkeypatch.setattr(jobs.JobContext, "checkpoint", checkpoint)
    return steps


@pytest.mark.parametrize(
    ("max_inputs", "purged"), [(1, 1), (20, 20)], ids=["one_listed", "default"]
)
def test_a_digest_after_a_purge_reports_its_kept_records_and_never_counts_purged_ones(
    env: Env, max_inputs: int, purged: int
) -> None:
    """Review r2 P6 case D and P6b, no race. A purge's tombstone revisions are the newest in
    the digest's range; they used to fill every listed ID, so the finding was withheld, the
    slot result still advanced the baseline, and the kept change was never reported. Purged
    records are now outside the digest's query: they are neither listed nor counted."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start), budget=budget(max_inputs=max_inputs)))
    kept = create_entry(title="Kept entry that changed")
    env.apply([kept])
    doomed = [create_entry(title=f"Entry the owner purges {n}") for n in range(purged)]
    env.apply(doomed)
    closure = purge_now(env, *(d["id"] for d in doomed))
    assert sorted(closure) == sorted(d["id"] for d in doomed)  # the job survives the purge
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [(r["state"], r["finding"]) for r in tick["ran"]] == [("succeeded", True)]
    (note,) = store_read(
        env, "SELECT title, summary, record_ids FROM job_notifications WHERE job_id = ?", job_id
    )
    assert json.loads(note["record_ids"]) == [kept["id"]]
    assert note["title"].startswith("Project digest: 1 record(s) changed")
    assert note["summary"] == "entry: 1"
    (slot,) = slot_rows(env, job_id)
    assert slot["result"]["kinds"] == {"entry": 1} and slot["result"]["finding"] is True
    assert all(named_anywhere(env, d["id"]) == [] for d in doomed)


def test_a_withheld_finding_is_never_the_next_runs_baseline(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r2 N1 (c). The digest lists one ID (``max_inputs=1``), the newest change, and that
    record is purged after the digest read it: nothing the finding lists is left, so it is
    withheld. The withheld slot's result is not adopted as the comparison baseline, so the
    next run reports the kept change it could not report."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start), budget=budget(max_inputs=1)))
    kept = create_entry(title="Kept entry that changed")
    env.apply([kept])
    doomed = create_entry(title="Entry the owner purges")
    env.apply([doomed])  # the newest change: the one ID this digest lists
    steps = digest_race_purge(monkeypatch, env, doomed["id"])
    first = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    monkeypatch.undo()
    assert steps == ["read", "purge"]
    assert [(r["state"], r["finding"]) for r in first["ran"]] == [("succeeded", False)]
    (slot,) = slot_rows(env, job_id)
    assert slot["result"]["finding_withheld"] == jobs.WITHHELD
    assert store_read(env, "SELECT id FROM job_notifications WHERE job_id = ?", job_id) == []
    second = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=31))
    assert [(r["state"], r["finding"]) for r in second["ran"]] == [("succeeded", True)]
    (note,) = store_read(
        env, "SELECT summary, record_ids FROM job_notifications WHERE job_id = ?", job_id
    )
    assert json.loads(note["record_ids"]) == [kept["id"]] and note["summary"] == "entry: 1"
    assert named_anywhere(env, doomed["id"]) == []


def test_a_paper_check_redacts_an_anchor_purged_during_its_run_and_reports_the_rest(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r2 P6 case P. An anchor is purged after the check's claim and before its
    snapshot, so the in-memory previous result still names it and the summary's ``resolved``
    line does. That line is redacted, not a reason to withhold the ``new`` line about another,
    kept anchor: the finding is committed and names no purged ID."""
    pp = PaperProject(env)
    paper = pp.paper()
    doomed = pp.anchor(paper, 1)  # no claims, no evidence: a readiness finding
    start = minute_now()
    job_id = add(env, job("paper_check", interval(60, start), {"paper_id": paper}))
    first = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=61))
    assert [(r["state"], r["finding"]) for r in first["ran"]] == [("succeeded", True)]
    kept = pp.anchor(paper, 2)  # a new finding about a kept anchor
    real_cancelled = jobs.JobContext.cancelled
    closure: list[str] = []

    def cancelled(ctx: jobs.JobContext) -> bool:
        if not closure:  # the check's first step: after its claim, before its snapshot
            closure.extend(purge_now(env, doomed))
        return bool(real_cancelled(ctx))

    monkeypatch.setattr(jobs.JobContext, "cancelled", cancelled)
    second = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=121))
    monkeypatch.undo()
    assert closure == [doomed]  # the paper and the job survive
    assert [(r["state"], r["finding"]) for r in second["ran"]] == [("succeeded", True)]
    due = jobs.fmt(start + timedelta(minutes=120))
    (note,) = store_read(
        env,
        "SELECT summary, record_ids FROM job_notifications WHERE job_id = ? AND due_utc = ?",
        job_id,
        due,
    )
    lines = note["summary"].splitlines()
    assert any(line.startswith("new: ") and kept in line for line in lines), lines
    assert any(line.startswith("resolved: ") and PURGED in line for line in lines), lines
    assert json.loads(note["record_ids"]) == [paper]
    assert named_anywhere(env, doomed) == []


@pytest.mark.parametrize("cut", [False, True], ids=["named", "past_the_cut"])
def test_a_refresh_writes_no_observation_of_a_version_purged_while_it_ran(
    env: Env, monkeypatch: pytest.MonkeyPatch, cut: bool
) -> None:
    """Review r2 N2/P8. Two watched sources change; the second's version is purged after its
    short read. Its observation is never saved, whether the summary names it (the line is
    redacted) or the summary is cut before it: the rule applies to what is written, not to the
    rendered text. The other source's observation and the finding are recorded."""
    names = ["watched-a.txt", "watched-b.txt"]
    for name in names:
        (env.research / name).write_text(f"original {name}\n", "utf-8")
    items = env.import_files(names)["plan"]["items"]
    source_ids = [i["source_id"] for i in items]
    kept_version, doomed = (i["version_id"] for i in items)
    start = minute_now()
    limit = 100 if cut else 2000  # 100: the first line alone is longer
    job_id = add(env, job("source_refresh", interval(15, start), {"source_ids": source_ids},
                          budget=budget(max_output_chars=limit)))  # fmt: skip
    assert env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))["quiet"]
    for name in names:
        (env.research / name).write_text(f"changed {name}\n", "utf-8")
    real_observe = sources._observe
    closure: list[str] = []

    def observe(store: Any, settings: Any, target: Any, progress: Any = None) -> Any:
        if target.version_id == doomed and not closure:  # after its short read
            closure.extend(purge_now(env, doomed))
        return real_observe(store, settings, target, progress)

    monkeypatch.setattr(sources, "_observe", observe)
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=31))
    monkeypatch.undo()
    assert closure == [doomed]  # the job and both sources survive
    assert [(r["state"], r["finding"]) for r in tick["ran"]] == [("succeeded", True)]
    checks = store_read(env, "SELECT version_id FROM source_checks ORDER BY id")
    assert [r["version_id"] for r in checks] == [kept_version]
    (note,) = store_read(
        env, "SELECT summary, record_ids FROM job_notifications WHERE job_id = ?", job_id
    )
    assert json.loads(note["record_ids"]) == sorted(source_ids)
    assert note["summary"].startswith(f"{source_ids[0]} ")
    if cut:
        assert len(note["summary"]) == limit
    else:
        assert f"{source_ids[0]} {kept_version}: " in note["summary"]
        assert f"{source_ids[1]} {PURGED}: " in note["summary"]
    assert named_anywhere(env, doomed) == []


def test_a_superseded_slot_whose_finding_an_earlier_attempt_recorded_succeeds(env: Env) -> None:
    """Review r2 N3/P9. A pre-0027 store holds a pending slot whose finding an earlier attempt
    committed, and the first tick comes only after a newer slot is due. The superseded slot is
    finished as succeeded (recorded earlier), not coalesced to ``skipped`` beside its finding."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    now = start + timedelta(minutes=16)
    due = jobs.fmt(start + timedelta(minutes=15))
    store = env.svc.open(env.project_id)
    with contextlib.closing(store.connect()) as conn:  # the earlier (pre-0027) attempt
        jobs._acquire(conn, "tick:old", now)
        jobs._schedule_slots(conn, now)
        assert jobs._claim(conn, job_id, due, "tick:old", now)
        jobs._release(conn, "tick:old")
    earlier = jobs.Finding(title="Earlier attempt's finding", summary="synthetic",
                           record_ids=[job_id])  # fmt: skip
    jobs._commit_finding(store, job_id, due, earlier, "test")
    assert jobs._finish(store, job_id, due, "tick:old", "pending",
                        reason="attempt 1 failed (busy); will retry")  # fmt: skip
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=46))
    assert tick["scheduled"] == {"pending": 1, "coalesced": 1}  # only the missed slot
    rows = {s["due_utc"]: s for s in slot_rows(env, job_id)}
    assert (rows[due]["state"], rows[due]["reason"], rows[due]["attempts"]) == (
        "succeeded",
        jobs.RECORDED_EARLIER,
        1,
    )
    missed = jobs.fmt(start + timedelta(minutes=30))
    assert (rows[missed]["state"], rows[missed]["reason"]) == (
        "skipped",
        "coalesced: missed while no scheduler ran",
    )
    assert notifications(env, job_id, due) == [("finding", "Earlier attempt's finding")]
    assert [(r["due_utc"], r["state"]) for r in tick["ran"]] == [
        (jobs.fmt(start + timedelta(minutes=45)), "succeeded")
    ]


def test_an_old_owner_whose_finding_replays_after_a_takeover_reports_lease_lost(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r2 N4/P10. The old owner passed its post-handler renewal and stalled past its
    lease; a replacement re-claimed the slot and committed the same finding with its success.
    The old owner's finding commit then replays (no ownership check runs on a replay), and
    only the owner-qualified finish of the replay branch can say ``lease_lost``."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    due = jobs.fmt(start + timedelta(minutes=15))

    def outcome() -> jobs.Outcome:
        return jobs.Outcome(
            {"usage": jobs.USAGE, "n": 1},
            "d1",
            jobs.Finding(title="Same finding", summary="synthetic", record_ids=[job_id]),
        )

    real_fault = jobs.fault_point
    replaced: list[dict[str, Any]] = []

    def fault(name: str) -> None:
        if name == "job_before_finish" and not replaced:
            take_over(env, job_id)
            same = outcome()
            assert same.finding is not None
            replaced.append(
                jobs._commit_finding(
                    env.svc.open(env.project_id), job_id, due, same.finding, "replacement",
                    owner="tick:replacement",
                    success=(same.result | {"finding": True}, same.input_digest),
                )
            )  # fmt: skip
        real_fault(name)

    monkeypatch.setitem(jobs.HANDLERS, "digest", lambda ctx: outcome())
    monkeypatch.setattr(jobs, "fault_point", fault)
    seq = env.seq()
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert replaced and replaced[0]["replayed"] is False  # the replacement committed it
    assert tick["ran"] == [
        {"job_id": job_id, "due_utc": due, "state": "lease_lost", "lost": "slot"}
    ]
    (slot,) = slot_rows(env, job_id)
    assert (slot["state"], slot["attempts"], slot["result"]["finding"]) == ("succeeded", 2, True)
    assert notifications(env, job_id, due) == [("finding", "Same finding")]
    assert request_keys(env, job_id, due, "finding") == [1] and env.seq() == seq + 1


def test_a_final_release_refused_for_any_reason_but_busy_is_raised(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r2 N7. Only a ``busy`` refusal of the final release is swallowed (the lease
    expires on its own); any other error from it is raised, after what the tick recorded."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    env.apply([create_entry(title="Something changed")])

    def failing(conn: sqlite3.Connection, owner: str) -> None:
        raise PrometheosError("io_error", "synthetic release failure (test)", {})

    monkeypatch.setattr(jobs, "_release", failing)
    with pytest.raises(PrometheosError) as failure:
        env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert failure.value.code == "io_error"
    assert slot_rows(env, job_id)[0]["state"] == "succeeded"


def test_a_finding_committed_for_an_owner_always_finishes_its_slot(env: Env) -> None:
    """Review r2 N5. The in-transaction ownership check is complete only together with the
    owner-qualified ``succeeded`` update, so ``owner`` and ``success`` are given together or
    not at all; either alone is refused before anything is committed."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    due = jobs.fmt(start + timedelta(minutes=15))
    store = env.svc.open(env.project_id)
    note = jobs.Finding(title="A finding", summary="synthetic", record_ids=[job_id])
    seq = env.seq()
    with pytest.raises(TypeError):
        jobs._commit_finding(store, job_id, due, note, "test", owner="tick:x")
    with pytest.raises(TypeError):
        jobs._commit_finding(store, job_id, due, note, "test", success=({"usage": jobs.USAGE}, "d"))
    assert env.seq() == seq and notifications(env, job_id, due) == []


# ----- r4: no ID fragment at a cut; the negative side of the superseded-slot rule -----------


def longest_uuid_prefix(text: str, record_id: str) -> int:
    """How many leading characters of ``record_id``'s UUID appear in ``text``."""
    uuid = record_id.split("_", 1)[1]
    return max((k for k in range(1, len(uuid) + 1) if uuid[:k] in text), default=0)


ID = "version_9c8582a9-1955-40bc-8bf5-da530e4e0a11"  # 44 characters


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (len("a ") + 44, f"a {ID}"),  # the ID ends exactly at the cut: kept
        (len("a ") + 43, "a "),  # one character short: dropped whole
        (len("a ") + 3, "a "),  # inside its kind prefix: dropped whole
        (len("a "), "a "),  # the cut is where it starts
        (len("a ") + 44 + 4, f"a {ID} tai"),  # after it: an ordinary cut
    ],
)
def test_a_cut_drops_a_record_id_it_would_split(limit: int, expected: str) -> None:
    """Review r3 F2: the one helper every cut summary goes through."""
    assert jobs._clip(f"a {ID} tail", limit) == expected
    assert jobs._clip("short", 100) == "short"


@pytest.mark.parametrize("purge", ["race", "after_commit"])
def test_a_summary_cut_never_keeps_a_fragment_of_a_record_id(
    env: Env, monkeypatch: pytest.MonkeyPatch, purge: str
) -> None:
    """Review r3 F2/P11. ``max_output_chars`` falls inside the second line's version ID, 30 of
    its 36 UUID characters in. That prefix used to survive in the owner's and the agent's
    inbox after the version was purged, whether the purge raced the refresh or came after its
    commit: neither the completion-time redaction nor purge's own rule matches a partial ID.
    A cut now drops an ID it would split entirely."""
    names = ["watched-a.txt", "watched-b.txt"]
    for name in names:
        (env.research / name).write_text(f"original {name}\n", "utf-8")
    items = env.import_files(names)["plan"]["items"]
    source_ids = [i["source_id"] for i in items]
    kept_version, doomed = (i["version_id"] for i in items)
    first_line = f"{source_ids[0]} {kept_version}: origin changed since this version"
    kept_text = f"{first_line}\n{source_ids[1]} "
    limit = len(kept_text) + len("version_") + 30  # inside the second version's UUID
    start = minute_now()
    job_id = add(env, job("source_refresh", interval(15, start), {"source_ids": source_ids},
                          budget=budget(max_output_chars=limit)))  # fmt: skip
    assert env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))["quiet"]
    for name in names:
        (env.research / name).write_text(f"changed {name}\n", "utf-8")
    closure: list[str] = []
    if purge == "race":
        real_observe = sources._observe

        def observe(store: Any, settings: Any, target: Any, progress: Any = None) -> Any:
            if target.version_id == doomed and not closure:  # after its short read
                closure.extend(purge_now(env, doomed))
            return real_observe(store, settings, target, progress)

        monkeypatch.setattr(sources, "_observe", observe)
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=31))
    monkeypatch.undo()
    if purge == "after_commit":
        closure.extend(purge_now(env, doomed))
    assert closure == [doomed]
    assert [(r["state"], r["finding"]) for r in tick["ran"]] == [("succeeded", True)]
    for caller in (OWNER, AGENT):
        (note,) = [n for n in env.svc.inbox(env.project_id, caller, True)["items"]
                   if n["job_id"] == job_id]  # fmt: skip
        assert note["summary"] == kept_text, note["summary"]  # the split ID is dropped whole
        assert longest_uuid_prefix(json.dumps(note), doomed) < 8, note
    assert named_anywhere(env, doomed) == []


def test_a_superseded_pending_slot_without_a_finding_is_coalesced(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r3 F1/P13, the negative side of N3. A slot's first attempt fails, so it is
    pending with backoff and no finding is recorded. The next tick comes only after a newer
    slot is due: the old slot is coalesced to ``skipped`` and counted, never finished as
    ``succeeded`` with the claim that an earlier attempt recorded its finding."""
    start = minute_now()
    job_id = add(env, job("digest", interval(15, start)))
    real = jobs.HANDLERS["digest"]
    calls: list[str] = []

    def handler(ctx: jobs.JobContext) -> jobs.Outcome:
        calls.append(ctx.due_utc)
        if len(calls) == 1:
            raise RuntimeError("synthetic first-attempt failure (test)")
        return real(ctx)

    monkeypatch.setitem(jobs.HANDLERS, "digest", handler)
    due = jobs.fmt(start + timedelta(minutes=15))
    first = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=16))
    assert [(r["due_utc"], r["state"]) for r in first["ran"]] == [(due, "pending")]
    second = env.svc.jobs_tick(env.project_id, now=start + timedelta(minutes=31))
    assert second["scheduled"] == {"pending": 1, "coalesced": 1}
    later = jobs.fmt(start + timedelta(minutes=30))
    assert [(r["due_utc"], r["state"]) for r in second["ran"]] == [(later, "succeeded")]
    assert calls == [due, later]  # the superseded slot never ran again
    (old,) = [s for s in slot_rows(env, job_id) if s["due_utc"] == due]
    assert (old["state"], old["reason"], old["attempts"]) == (
        "skipped",
        "coalesced: superseded by a later slot",
        1,
    )
    assert notifications(env, job_id, due) == []
    assert request_keys(env, job_id, due, "finding") == [0]
