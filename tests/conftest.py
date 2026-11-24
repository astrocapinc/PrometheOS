from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

# The private repository's development records (state, receipts, evidence, decisions). The
# public export leaves them out (decision 0072), so the tests that read them skip there; in
# the private repository they always run.
PRIVATE_RECORDS = (REPO / "development" / "state.json").is_file()
requires_private_records = pytest.mark.skipif(
    not PRIVATE_RECORDS,
    reason="reads the private development records, which the public export leaves out",
)

# Real-engine tests (marker ``tectonic``) need the Tectonic installed by
# `prometheos tex engine install`. When it is present they run with the default suite.
# When it is missing they are deselected *visibly* from the default suite (the summary
# says why), and they FAIL -- never skip -- whenever the run asks for them: `-m tectonic`
# or PROMETHEOS_REQUIRE_TECTONIC=1 (the CP9 checkpoint suite does both).
_DESELECTED: list[str] = []


def _tectonic_required(config: pytest.Config) -> bool:
    expression = (config.getoption("markexpr") or "").replace(" ", "")
    return os.environ.get("PROMETHEOS_REQUIRE_TECTONIC") == "1" or (
        "tectonic" in expression and "nottectonic" not in expression
    )


BASETEMP_KEEP_SECONDS = 86400


def short_basetemp(config: pytest.Config) -> None:
    """On Windows, a run given no ``--basetemp`` uses ``%TEMP%\\ptb\\<pid>`` instead of pytest's
    ``pytest-of-<user>\\pytest-NNN``: under xdist (``popen-gwN``) the default left the audio
    cache paths of ``test_cp10_mcp_paths`` past MAX_PATH on a PC without long paths (decision
    0048). Only the controller chooses; xdist hands each worker ``<basetemp>\\popen-gwN``.
    Folders older than a day are removed: a full suite leaves about 1 GB, and age (not a count)
    never removes the folder of another run that is still going (review s10f-a F5)."""
    if sys.platform != "win32" or config.option.basetemp or hasattr(config, "workerinput"):
        return
    import shutil
    import tempfile
    import time

    root = Path(tempfile.gettempdir()) / "ptb"
    root.mkdir(exist_ok=True)
    cutoff = time.time() - BASETEMP_KEEP_SECONDS
    for old in root.iterdir():
        try:
            if old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    config.option.basetemp = str(root / str(os.getpid()))


@pytest.hookimpl(tryfirst=True)  # before pytest's tmp_path factory reads the option
def pytest_configure(config: pytest.Config) -> None:
    short_basetemp(config)
    config.addinivalue_line(
        "markers", "tectonic: needs the real pinned Tectonic engine and its bundle cache"
    )
    config.addinivalue_line(
        "markers", "serial: runs alone after the parallel suite (process timing; decision 0037)"
    )


# Modules that must not share the machine with a parallel run. They carry the ``serial`` marker:
# the full suite runs everything else in parallel (``-n 12 -m "not serial"``) and then these
# modules serially and alone (``-m serial``); decision 0037. They kill or race real worker
# processes, or a byte-range lock holder, against short delays: under 12 workers the step a worker
# has reached, or when Windows releases a killed holder's lock, changes (seen in all five).
#
# Git's local-transport modules run in parallel: their ~30 s stalls and MSYS start-up aborts were a
# host fault (a domain account lookup on every cold MSYS start; decision 0037 section 4), and
# tools/msys_retry.py covers fixture git if an abort recurs.
SERIAL_MODULES = frozenset(
    {
        "test_cp10_decode_cache",
        "test_cp10_pending",
        "test_cp10_purge_running",
        "test_cp10_recovery_r2",
        "test_cp10_runs_kill",
        # CP17 walks through one adoption per module (scan, plan, decide, baseline, rescan) on a
        # module-scoped store, so its steps run in order; xdist would split them across workers.
        "test_cp17_assist",
        "test_cp17_incremental",
        "test_cp17_plan",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.module is not None and item.module.__name__ in SERIAL_MODULES:
            item.add_marker(pytest.mark.serial)
    marked = [i for i in items if i.get_closest_marker("tectonic")]
    if not marked or _tectonic_required(config):
        return
    from cp9_support import engine_problem

    problem = engine_problem()
    if problem is None:
        return
    _DESELECTED.append(f"{len(marked)} real-engine tests deselected: {problem}")
    config.hook.pytest_deselected(items=marked)
    items[:] = [i for i in items if not i.get_closest_marker("tectonic")]


# Fixture git retries (tools/msys_retry.py) from xdist workers, collected on the controller.
_WORKER_RETRIES: list[str] = []


def pytest_sessionfinish(session: pytest.Session) -> None:
    """On an xdist worker: hand this worker's msys_retry reports to the controller."""
    output = getattr(session.config, "workeroutput", None)
    if output is not None:
        import msys_retry

        output["msys_retries"] = list(msys_retry.RETRIES)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: object, error: object) -> None:
    _WORKER_RETRIES.extend(getattr(node, "workeroutput", {}).get("msys_retries", []))


def msys_retry_lines() -> list[str]:
    """The terminal-summary lines for every fixture git retry of this run (D38-F2)."""
    import msys_retry

    retries = [*msys_retry.RETRIES, *_WORKER_RETRIES]
    if not retries:
        return []
    head = f"MSYS_RETRY: {len(retries)} fixture git command(s) retried after an MSYS runtime abort"
    return [head, *(f"  {line}" for line in retries)]


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    for line in _DESELECTED:
        terminalreporter.write_line(f"TECTONIC: {line} (run with -m tectonic to require them)")
    for line in msys_retry_lines():
        terminalreporter.write_line(line)


@pytest.fixture
def tectonic_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The real engine folder; fails loudly when it is not installed and verified."""
    from cp9_support import default_engine_dir, engine_problem

    problem = engine_problem()
    if problem is not None:
        pytest.fail(f"real Tectonic engine required but unavailable: {problem}")
    directory = default_engine_dir()
    assert directory is not None
    monkeypatch.setenv("PROMETHEOS_TEX_ENGINE_DIR", str(directory))
    yield directory
