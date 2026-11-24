"""CP10.3 kill and restart probes and the S25 scenario (C10-RUNS; decision 0017).

Workers are real ``prometheos run resume`` processes on a disposable home. They are stopped
between stages either by a named fault point (``os._exit``: no cleanup at all) or by a real
``Popen.kill`` (TerminateProcess on Windows) while a later stage is in progress, and the run is
then recovered and resumed by another process. Assertions are on the stored state: one run ID
throughout, truthful states, no duplicate outputs, an uncertain external call never sent again.
"""

from __future__ import annotations

import http.server
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from audio_support import SCRIPT, SPEECH, AudioEnv, write_wav
from cp1_support import AGENT
from provider_support import ProviderEnv, config_doc

from prometheos.audio import engines
from prometheos.audio.transcribe import RunConfig
from prometheos.domain.models import PrometheosError
from prometheos.faults import FAULT_EXIT_CODE

LONG_PLAN = [(2000, SPEECH)] * 8  # 16 s of audio: seven 4 s chunks with 2 s overlap


def refused(fn: Any) -> PrometheosError:
    with pytest.raises(PrometheosError) as err:
        fn()
    return err.value


def worker_env(home: Path, **extra: str) -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PROMETHEOS_")}
    environment["PROMETHEOS_HOME"] = str(home)
    environment[engines.SYNTHETIC_ENV] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(extra)
    return environment


class Worker:
    """One ``prometheos run resume`` child this test started and therefore owns.

    The fixture below reaps every owned worker whatever the test body did, so no pipe or
    process outlives its test, and every early exit or timeout carries the child's own exit
    code, stdout and stderr in the assertion message (never a bare timeout)."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self.popen = popen
        self.streams: tuple[bytes, bytes] | None = None
        self.output_lost = False

    def reap(self, timeout: float = 30) -> tuple[bytes, bytes]:
        """Stop this exact child if it still runs, collect its output and close both pipes.
        Idempotent: the first call's output is what every later call returns."""
        if self.streams is None:
            if self.popen.poll() is None:
                self.popen.kill()
            try:
                self.streams = self.popen.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:  # a grandchild holds a pipe open: close ours
                for pipe in (self.popen.stdout, self.popen.stderr):
                    if pipe is not None:
                        pipe.close()
                self.popen.wait(timeout=timeout)
                self.streams = (b"", b"")
                self.output_lost = True  # describe() says so: empty is not "printed nothing"
        return self.streams

    def describe(self, note: str) -> str:
        out, err = self.reap()
        lost = (
            "\n(the worker's output is LOST: its pipes did not drain after the kill and were "
            "closed)"
            if self.output_lost
            else ""
        )
        return (
            f"{note}: worker pid {self.popen.pid} {self.popen.args!r} "
            f"returncode {self.popen.returncode}{lost}\n"
            f"--- worker stdout ---\n{out.decode('utf-8', 'replace')[-6000:]}\n"
            f"--- worker stderr ---\n{err.decode('utf-8', 'replace')[-6000:]}"
        )


_OWNED: list[Worker] = []


def reap_owned_workers() -> list[int]:
    """Reap every worker this test started: still-running ones are killed (only the exact
    children it started), all pipes are closed. Returns the PIDs that were still running."""
    owned, stragglers = list(_OWNED), []
    _OWNED.clear()
    for worker in owned:
        if worker.popen.poll() is None:
            stragglers.append(worker.popen.pid)
        worker.reap()
    return stragglers


@pytest.fixture(autouse=True)
def _reap_owned_workers() -> Iterator[None]:
    """Whatever happens in the test body, every worker it started ends up reaped."""
    _OWNED.clear()
    try:
        yield
    finally:
        if stragglers := reap_owned_workers():  # alive at test end: cleaned, but not silently
            warnings.warn(f"reaped worker(s) still running at test end: {stragglers}", stacklevel=1)


def start_worker(home: Path, project_id: str, run_id: str, **extra: str) -> Worker:
    popen = subprocess.Popen(
        [sys.executable, "-m", "prometheos", "run", "resume", run_id, "--project", project_id,
         "--json"],
        env=worker_env(home, **extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )  # fmt: skip
    worker = Worker(popen)
    _OWNED.append(worker)
    return worker


def finish(worker: Worker, timeout: float = 120) -> tuple[int, dict[str, Any]]:
    try:
        out, err = worker.popen.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            worker.describe(f"the worker did not finish within {timeout}s (killed by the test)")
        ) from None
    worker.streams = (out, err)
    lines = [line for line in out.decode("utf-8").splitlines() if line.strip()]
    try:
        return int(worker.popen.returncode), (json.loads(lines[-1]) if lines else {})
    except ValueError:
        raise AssertionError(worker.describe("the worker's last output line is not JSON")) from None


def kill(worker: Worker) -> None:
    """A real kill (TerminateProcess on Windows): no cleanup runs in the worker."""
    worker.popen.kill()
    worker.reap()


def connect(store_dir: Path) -> sqlite3.Connection:
    """The only way these tests observe a store directly, and it is always READ-ONLY.

    A raw writable handle is, by decision 0025, an external writer: it makes a worker's guarded
    admission answer ``busy``. It also hides product state: opening a store writable rolls a
    killed worker's hot journal back IN PLACE, which the app itself never does (it refuses such
    a store with ``needs_journal_recovery`` and recovers only a new guarded copy). A read-only
    connection leaves the journal untouched and fails loudly on it
    (``SQLITE_READONLY_ROLLBACK``), so a test sees the product's real state or goes through the
    product's own recovery path."""
    path = store_dir / "record.sqlite"
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)


SQLITE_READONLY_ROLLBACK = 776  # SQLITE_READONLY | (3 << 8)
HOT_JOURNAL_NOTE = (
    "the store has a hot journal: a worker was killed inside a write transaction. A read-only "
    "connection refuses it (the product recovers only a guarded copy). A kill probe must stop the "
    "worker between transactions: wait for the synthetic engine's delay marker (in_engine_delay), "
    "not for a time or a progress count."
)


def stored(store_dir: Path, run_id: str) -> sqlite3.Row:
    try:
        with closing(connect(store_dir)) as conn:
            conn.row_factory = sqlite3.Row
            found: sqlite3.Row = conn.execute(
                "SELECT * FROM operation_runs WHERE id = ?", (run_id,)
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) == SQLITE_READONLY_ROLLBACK:
            # int7 CP10-F2: say what happened, beside SQLite's bare "readonly database".
            exc.add_note(HOT_JOURNAL_NOTE)
        raise
    return found


def wait_until(check: Callable[[], bool], seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.05)


def progress_done(store_dir: Path, run_id: str) -> int:
    row = stored(store_dir, run_id)
    return int(json.loads(row["progress"])["done"]) if row["progress"] else 0


def wait_for_progress(
    worker: Worker, store_dir: Path, run_id: str, at_least: int, seconds: float = 90
) -> None:
    """Wait until the run has recorded ``at_least`` units of progress. A worker that exits
    first, or makes no such progress in time, fails with its own exit code and output."""
    deadline = time.monotonic() + seconds
    while (done := progress_done(store_dir, run_id)) < at_least:
        if worker.popen.poll() is not None:
            raise AssertionError(
                worker.describe(f"the worker exited with progress {done} < {at_least}")
            )
        if time.monotonic() > deadline:
            raise AssertionError(
                worker.describe(
                    f"progress stayed at {done} < {at_least} for {seconds}s "
                    f"(run state {stored(store_dir, run_id)['state']!r}; killed by the test)"
                )
            )
        time.sleep(0.05)


def in_engine_delay(worker: Worker, marker: Path, chunks: int, seconds: float = 90) -> None:
    """Wait until the synthetic engine has entered its delay for chunk number ``chunks`` (it
    appends a line to ``marker`` as it starts sleeping). Then no transaction is open: the chunk's
    charge is committed and its result not yet written, so a kill leaves no hot journal (int7
    CP10-F2; decision 0048). The 15 s lease heartbeat is the only other writer, far from a
    300-400 ms delay."""
    deadline = time.monotonic() + seconds
    while not marker.exists() or len(marker.read_text("utf-8").split()) < chunks:
        if worker.popen.poll() is not None:
            raise AssertionError(worker.describe(f"the worker exited before chunk {chunks}"))
        if time.monotonic() > deadline:
            raise AssertionError(worker.describe(f"chunk {chunks} did not start in {seconds}s"))
        time.sleep(0.01)


def count(store_dir: Path, sql: str, params: tuple[Any, ...] = ()) -> int:
    with closing(connect(store_dir)) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def transcripts(store_dir: Path) -> int:
    return count(
        store_dir,
        "SELECT COUNT(*) FROM bodies b JOIN revisions v ON v.body_id = b.id "
        'WHERE b.encoded LIKE \'%"payload_schema":"transcript_v1"%\'',
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AudioEnv:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    return AudioEnv(tmp_path)


# ----- the worker fixture itself ------------------------------------------------------------------
# S25 once waited 90 s for progress that could not come: the test's own raw WRITABLE polling
# connection made the worker's guarded admission answer ``busy`` (decision 0025) and the worker
# failed the run at once. These controls keep that diagnosis visible and the fixture honest.


def test_an_observer_is_read_only_and_leaves_guarded_admission_free(env: AudioEnv) -> None:
    with closing(connect(env.store_dir)) as watcher:
        assert watcher.execute("SELECT COUNT(*) FROM operation_runs").fetchone()[0] >= 0
        env.svc.open(env.project_id)  # admitted with the observer's handle open
        with pytest.raises(sqlite3.OperationalError):  # it can neither write nor roll back
            watcher.execute("UPDATE operation_runs SET attempts = attempts")
    # The premise: an idle raw writable handle is what the guard answers busy to.
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as writer:
        writer.execute("SELECT COUNT(*) FROM operation_runs").fetchone()
        assert refused(lambda: env.svc.open(env.project_id)).code == "busy"
    env.svc.open(env.project_id)


HOT_WRITER = """
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute('PRAGMA cache_size=5')
c.execute('CREATE TABLE hot_probe (id INTEGER PRIMARY KEY, b BLOB)')
c.execute('BEGIN')
c.execute('WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3000) '
          'INSERT INTO hot_probe (b) SELECT randomblob(1000) FROM n')
c.execute('COMMIT')
c.execute('BEGIN')
c.execute('UPDATE hot_probe SET b = randomblob(1000)')  # spills: journal synced, pages overwritten
print('ready', flush=True)
time.sleep(120)
"""
ROLLBACK_JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")


def test_a_killed_writers_hot_journal_is_left_for_the_product_not_rolled_back_by_a_test(
    env: AudioEnv,
) -> None:
    """The product never applies a hot journal in place (decision 0025): it refuses the store
    and recovers only a new guarded copy. A raw WRITABLE connection would silently roll it back
    and make a store the product refuses look healthy, so the fixture observes read-only."""
    journal = env.store_dir / "record.sqlite-journal"
    popen = subprocess.Popen(
        [sys.executable, "-c", HOT_WRITER, str(env.store_dir / "record.sqlite")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    writer = Worker(popen)
    _OWNED.append(writer)
    assert popen.stdout is not None
    assert popen.stdout.readline().strip() == b"ready", writer.describe("the writer did not spill")
    kill(writer)  # killed mid-transaction, after its journal was synced
    assert journal.read_bytes()[:8] == ROLLBACK_JOURNAL_MAGIC  # a HOT journal
    with pytest.raises(sqlite3.OperationalError) as unreadable:  # fails loudly, rolls nothing back
        stored(env.store_dir, "oprun_none")
    assert unreadable.value.sqlite_errorname == "SQLITE_READONLY_ROLLBACK"
    assert HOT_JOURNAL_NOTE in getattr(unreadable.value, "__notes__", [])  # CP10-F2
    assert journal.read_bytes()[:8] == ROLLBACK_JOURNAL_MAGIC  # still there, untouched
    err = refused(lambda: env.svc.open(env.project_id))  # the product's own refusal
    assert err.code == "io_error" and "recovery" in err.details
    assert err.details["store_health"] == "needs_journal_recovery"
    assert journal.read_bytes()[:8] == ROLLBACK_JOURNAL_MAGIC  # ...and it did not apply it either


def test_reaping_at_teardown_stops_a_straggler_and_says_so(env: AudioEnv) -> None:
    """The autouse fixture's path: a worker still alive at the end of a test is killed, its
    pipes are closed and it is reported (the fixture turns the report into a warning).

    The two workers never overlap and transcribe different recordings: workers on identical
    audio share one decode cache file, and that race (not this control's subject) made an
    earlier form of this test fail about one run in ten."""
    short = [(2000, SPEECH)] * 3  # a different recording (different bytes) from LONG_PLAN's
    finished_run, straggler_run = (
        env.svc.transcribe_start(
            env.project_id,
            AGENT,
            env.import_audio(name, sensitivity="normal", plan=plan)["version_id"],
            env.synthetic_config(),
        )["run_id"]
        for name, plan in (("short.wav", short), ("long.wav", LONG_PLAN))
    )
    done, _ = finish(start_worker(env.home, env.project_id, finished_run))
    assert done == 0  # an owned worker that already finished is not reported below
    straggler = start_worker(
        env.home, env.project_id, straggler_run, PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="60000"
    )
    wait_until(  # observed running, not assumed after a pause
        lambda: (
            straggler.popen.poll() is not None
            or stored(env.store_dir, straggler_run)["state"] == "running"
        ),
        60,
    )
    assert straggler.popen.poll() is None, straggler.describe("the straggler is not running")
    pid = straggler.popen.pid
    stragglers = reap_owned_workers()
    assert stragglers == [pid], straggler.describe("the straggler was not reported")
    assert straggler.popen.poll() is not None and not _OWNED, straggler.describe("not reaped")
    assert straggler.popen.stdout is not None and straggler.popen.stdout.closed
    assert straggler.popen.stderr is not None and straggler.popen.stderr.closed


class _StuckPipes:
    """A child whose pipes never drain after the kill (a grandchild holds them open)."""

    pid = 4242
    args = ("stuck",)
    returncode = -9
    stdout = stderr = None

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        pass

    def communicate(self, timeout: float) -> tuple[bytes, bytes]:
        raise subprocess.TimeoutExpired("stuck", timeout)

    def wait(self, timeout: float) -> int:
        return self.returncode


def test_output_lost_to_a_pipe_that_did_not_drain_is_stated_not_shown_as_empty() -> None:
    worker = Worker(_StuckPipes())
    message = worker.describe("the worker hung")
    assert worker.output_lost and "output is LOST" in message and "returncode -9" in message


def test_a_worker_that_exits_early_fails_the_wait_with_its_own_exit_code_and_output(
    env: AudioEnv,
) -> None:
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    rid = run["run_id"]
    worker = start_worker(
        env.home, env.project_id, rid, PROMETHEOS_FAULT_POINT="run_after:chunk:00001"
    )
    with pytest.raises(AssertionError) as err:  # more progress than the worker can make
        wait_for_progress(worker, env.store_dir, rid, 99, seconds=60)
    message = str(err.value)
    assert "exited with progress" in message
    assert f"returncode {FAULT_EXIT_CODE}" in message and "--- worker stdout ---" in message
    assert f"resuming {rid}" in message  # the child's own stderr, not the test's words
    assert worker.popen.stdout is not None and worker.popen.stdout.closed  # pipes are closed
    assert worker.popen.stderr is not None and worker.popen.stderr.closed


def test_reaping_stops_exactly_the_owned_worker_and_closes_its_pipes(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    rid = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )["run_id"]
    bystander = subprocess.Popen(  # not a Worker: nothing here may touch it
        [sys.executable, "-c", "import time; time.sleep(120)"], stdout=subprocess.DEVNULL
    )
    worker = start_worker(
        env.home, env.project_id, rid, PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="60000"
    )
    try:
        wait_until(
            lambda: (
                worker.popen.poll() is not None or stored(env.store_dir, rid)["state"] == "running"
            ),
            60,
        )
        assert worker.popen.poll() is None, worker.describe("the worker exited early")  # mid-run
        first = worker.reap()
        assert worker.popen.poll() is not None and worker.reap() is first  # idempotent
        assert worker.popen.stdout is not None and worker.popen.stdout.closed
        assert worker.popen.stderr is not None and worker.popen.stderr.closed
        assert bystander.poll() is None  # only the exact owned child was stopped
    finally:
        bystander.kill()
        bystander.wait(timeout=30)


# ----- probes ----------------------------------------------------------------------------------


def test_a_fault_between_chunks_resumes_in_another_process_without_duplicates(
    env: AudioEnv,
) -> None:
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    rid = run["run_id"]
    code, _ = finish(
        start_worker(env.home, env.project_id, rid, PROMETHEOS_FAULT_POINT="run_after:chunk:00001")
    )
    assert code == FAULT_EXIT_CODE  # stopped dead after the second chunk was recorded
    assert stored(env.store_dir, rid)["state"] == "running"  # as the dead worker left it
    view = env.svc.run_get(env.project_id, AGENT, rid)  # another process recovers it
    assert view["state"] == "queued" and view["stages_completed"] == 3  # decode + two chunks
    before = {s["name"]: s["completed_at"] for s in view["stages"]}
    code, done = finish(start_worker(env.home, env.project_id, rid))
    assert code == 0 and done["run_id"] == rid and done["state"] == "completed"
    assert done["attempts"] == 2
    after = {s["name"]: s["completed_at"] for s in done["stages"]}
    assert all(after[name] == at for name, at in before.items())  # never redone
    assert sorted(n for n in after if n.startswith("chunk:")) == [
        f"chunk:{i:05d}" for i in range(7)
    ]
    assert transcripts(env.store_dir) == 1
    assert count(env.store_dir, "SELECT COUNT(*) FROM transcription_chunks") == 0


def test_a_killed_worker_is_recognised_at_once_and_its_run_resumes(env: AudioEnv) -> None:
    audio = env.import_audio(sensitivity="normal", plan=LONG_PLAN)
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    rid = run["run_id"]
    marker = env.tmp / "engine-delay.txt"
    proc = start_worker(env.home, env.project_id, rid, PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="400",
                        PROMETHEOS_SYNTHETIC_ENGINE_MARKER=str(marker))  # fmt: skip
    in_engine_delay(proc, marker, 3)
    kill(proc)  # a real kill while the third chunk is in the engine, between transactions
    left = stored(env.store_dir, rid)
    assert left["state"] == "running" and left["lease_owner"]  # the lease is still "live"
    done_before = progress_done(env.store_dir, rid)
    view = env.svc.run_get(env.project_id, AGENT, rid)
    assert view["state"] == "queued"  # its process is gone: no waiting for the lease
    assert view["stages_completed"] == 1 + done_before
    code, done = finish(start_worker(env.home, env.project_id, rid))
    assert code == 0 and done["state"] == "completed" and done["run_id"] == rid
    assert transcripts(env.store_dir) == 1


# ----- the S25 scenario --------------------------------------------------------------------------


class HoldingProvider:
    """A loopback stand-in for a provider that receives a request and does not answer until
    released: the worker is stopped while its external call is in flight."""

    def __init__(self) -> None:
        self.received = threading.Event()
        self.release = threading.Event()
        self.requests = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests += 1
                outer.received.set()
                outer.release.wait(60)
                try:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass  # the worker that sent it was killed

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def test_s25_kill_restart_resume_changed_input_budget_cancel_and_unknown_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a long extraction run, a run whose external provider call has started, and a fake
    budget close to its limit: kill the process between stages, restart and resume, change an
    input and resume again, exhaust the budget, then cancel. One run ID throughout with
    truthful states; a compatible resume finishes without duplicate outputs; the changed input
    is refused; paused_budget then resumes; the uncertain external call becomes
    unknown_external and is not repeated."""
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    env = ProviderEnv(tmp_path)
    pid, home, store_dir = env.project_id, env.home, env.store_dir
    config = RunConfig("synthetic", chunk_ms=4000, overlap_ms=2000, script=SCRIPT)

    def import_audio(name: str) -> dict[str, Any]:
        write_wav(env.research / name, LONG_PLAN)
        return env.svc.audio_import(
            pid, AGENT, root_alias="research", path=name, sensitivity="normal"
        )

    # --- 1. A long extraction run with a budget close to its limit (7 chunks x 4 s = 28 s;
    # 22 s pays for five). Kill its worker between stages, restart, resume.
    audio = import_audio("long.wav")
    run = env.svc.transcribe_start(pid, AGENT, audio["version_id"], config, budget_seconds=22)
    extraction = run["run_id"]
    marker = tmp_path / "engine-delay.txt"
    proc = start_worker(home, pid, extraction, PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="300",
                        PROMETHEOS_SYNTHETIC_ENGINE_MARKER=str(marker))  # fmt: skip
    in_engine_delay(proc, marker, 3)
    kill(proc)  # between transactions (decision 0048)
    assert env.svc.run_get(pid, AGENT, extraction)["state"] == "queued"
    code, view = finish(start_worker(home, pid, extraction))
    # --- 2. The resumed run spends its budget: paused_budget, nothing recharged.
    assert code == 2 and view["run_id"] == extraction and view["state"] == "paused_budget"
    assert view["budget"]["charged"] == "20.000000" and view["progress"]["done"] == 5
    paused_stages = {s["name"]: s["completed_at"] for s in view["stages"]}
    assert transcripts(store_dir) == 0

    # --- 3. Change an input of another extraction (an import of planned bytes) between its
    # stages: the resume is refused; with the original bytes back it finishes once.
    (env.research / "extra.md").write_text("first measurement\n", "utf-8")
    plan = env.svc.import_plan(pid, AGENT, root_alias="research", paths=["extra.md"])
    queued = env.svc.import_commit(pid, plan["plan_id"], AGENT, mode="none")
    importing = queued["run_id"]
    code, _ = finish(
        start_worker(home, pid, importing, PROMETHEOS_FAULT_POINT="run_checkpoint:commit")
    )
    assert code == FAULT_EXIT_CODE
    assert env.svc.run_get(pid, AGENT, importing)["state"] == "queued"
    (env.research / "extra.md").write_text("an edited measurement\n", "utf-8")
    err = refused(lambda: env.svc.run_resume(pid, AGENT, importing, mode="foreground"))
    assert err.code == "stale_task" and err.details["changed"] == ["input_digest"]
    assert env.svc.run_get(pid, AGENT, importing)["state"] == "queued"
    (env.research / "extra.md").write_text("first measurement\n", "utf-8")
    code, imported = finish(start_worker(home, pid, importing))
    assert code == 0 and imported["state"] == "completed" and imported["run_id"] == importing
    request_id = imported["result"]["request_id"]
    assert (
        count(store_dir, "SELECT COUNT(*) FROM requests WHERE request_id = ?", (request_id,)) == 1
    )

    # --- 4. The budget-paused extraction resumes with a larger budget and completes once.
    done = env.svc.run_resume(pid, AGENT, extraction, budget_amount="40", mode="foreground")
    assert done["state"] == "completed" and done["run_id"] == extraction
    assert done["budget"]["charged"] == "28.000000"
    assert all(
        {s["name"]: s["completed_at"] for s in done["stages"]}[n] == at
        for n, at in paused_stages.items()
    )
    assert transcripts(store_dir) == 1

    # --- 5. Cancel: a running run records the request until its worker confirms; a paused one
    # is cancelled at once.
    second = import_audio("second.wav")
    run2 = env.svc.transcribe_start(pid, AGENT, second["version_id"], config)
    running = run2["run_id"]
    proc = start_worker(home, pid, running, PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS="300")
    wait_for_progress(proc, store_dir, running, 1)
    reply = env.svc.run_cancel(pid, AGENT, running)
    assert reply["outcome"] == "cancel_requested" and reply["run"]["cancel_confirmed"] is False
    code, cancelled = finish(proc)
    assert code == 3 and cancelled["state"] == "cancelled" and cancelled["cancel_confirmed"]
    assert transcripts(store_dir) == 1  # the cancelled run committed nothing

    # --- 6. A provider call starts, the worker is killed while it is in flight: after restart
    # the run is unknown_external and the call is never sent again.
    provider = HoldingProvider()
    try:
        env.svc.provider_transport = None  # the real transport, to the loopback stand-in
        env.configure(
            config_doc(policy="local-only", endpoint=provider.endpoint, credential_ref=None,
                       price_input_per_mtok_usd=None, price_output_per_mtok_usd=None,
                       budget={"spend_cap_usd": None}),
            store_secret=False,
        )  # fmt: skip
        created = env.run(caller=AGENT, mode="none")
        external = created["run_id"]
        proc = start_worker(home, pid, external)
        assert provider.received.wait(60)
        assert stored(store_dir, external)["external_effect"] == "started"
        kill(proc)
        view = env.svc.run_get(pid, AGENT, external)
        assert view["state"] == "unknown_external" and view["external_effect"] == "uncertain"
        assert view["external_call"]["request_sha256"] == view["input_digests"][0]["sha256"]
        for attempt in (
            lambda: env.svc.run_resume(pid, AGENT, external, mode="foreground"),
            lambda: env.svc.run_cancel(pid, AGENT, external),
        ):
            assert refused(attempt).code == "validation_error"
        code, again = finish(start_worker(home, pid, external))
        assert code != 0 and again["error"]["code"] == "validation_error"
        assert provider.requests == 1  # never repeated
        ledger = env.svc.provider_calls(pid, AGENT)["calls"]
        assert [c["state"] for c in ledger] == ["unknown"]
        closed = env.svc.run_acknowledge(pid, AGENT, external, "the provider has no record")
        assert closed["state"] == "failed" and closed["external_effect"] == "uncertain"
        assert provider.requests == 1
    finally:
        provider.close()

    # One run ID per operation throughout, every final state truthful.
    states = {r["run_id"]: r["state"] for r in env.svc.run_list(pid, AGENT, limit=50)["runs"]}
    assert states[extraction] == "completed" and states[importing] == "completed"
    assert states[running] == "cancelled" and states[external] == "failed"


def test_cli_no_wait_answers_the_run_at_once_and_a_detached_worker_finishes_it(
    env: AudioEnv,
) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "prometheos", "source", "import", "baseline.md", "--plan",
         "--root", "research", "--project", env.project_id, "--no-wait", "--json"],
        env=worker_env(env.home),
        capture_output=True,
        timeout=120,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    view = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
    assert view["run_id"].startswith("oprun_") and view["state"] in ("queued", "running")
    wait_until(lambda: stored(env.store_dir, view["run_id"])["state"] == "completed", 120)
    done = env.svc.run_get(env.project_id, AGENT, view["run_id"])
    assert done["result"]["plan_id"] in view["run_id"] and done["attempts"] == 1


def test_a_worker_killed_right_after_its_commit_replays_instead_of_duplicating(
    env: AudioEnv,
) -> None:
    audio = env.import_audio(sensitivity="normal")
    run = env.svc.transcribe_start(
        env.project_id, AGENT, audio["version_id"], env.synthetic_config()
    )
    rid = run["run_id"]
    seq = env.seq()
    code, _ = finish(
        start_worker(env.home, env.project_id, rid, PROMETHEOS_FAULT_POINT="commit_after")
    )
    assert code == FAULT_EXIT_CODE  # the transcript committed; the run was not marked done
    assert env.seq() == seq + 1 and transcripts(env.store_dir) == 1
    assert env.svc.run_get(env.project_id, AGENT, rid)["state"] == "queued"
    code, done = finish(start_worker(env.home, env.project_id, rid))
    assert code == 0 and done["state"] == "completed" and done["run_id"] == rid
    assert done["result"]["commit"]["replayed"] is True  # the same commit, replayed
    assert env.seq() == seq + 1 and transcripts(env.store_dir) == 1
