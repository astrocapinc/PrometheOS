"""tools/msys_retry.py (decision 0037; review int6 D37-F3): fixture git is retried only when the
MSYS runtime aborted at start-up, each retry is reported, and a final failure names git's stderr."""

from __future__ import annotations

import sys
from pathlib import Path

import msys_retry
import pytest


@pytest.fixture(autouse=True)
def own_retry_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests retry on purpose: keep their reports out of the run's real summary."""
    monkeypatch.setattr(msys_retry, "RETRIES", [])


@pytest.mark.parametrize(
    "stderr",
    [
        "      0 [main] sh (4944) C:\\Program Files\\Git\\usr\\bin\\sh.exe: *** fatal error - "
        'add_item ("\\??\\C:\\Program Files\\Git", "/", ...) failed, errno 1',
        "*** fatal error - cygheap base mismatch detected",
        b"  12 [main] git-upload-pack (77) something",
    ],
)
def test_msys_runtime_aborts_are_recognised(stderr: str | bytes) -> None:
    assert msys_retry.runtime_failure(stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: 'origin' does not appear to be a git repository",
        "error: failed to push some refs to 'C:/x/o.git'",
        "",
        None,
    ],
)
def test_ordinary_git_errors_are_not_retried(stderr: str | None) -> None:
    assert not msys_retry.runtime_failure(stderr)


def flaky(counter: Path, failures: int, message: str) -> list[str]:
    """A command that fails ``failures`` times with ``message`` on stderr, then succeeds."""
    code = (
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        f"if n < {failures}:\n"
        f"    sys.stderr.write({message!r}); sys.exit(128)\n"
        "print('ok')\n"
    )
    return [sys.executable, "-c", code]


def test_an_msys_abort_is_retried_and_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(msys_retry.time, "sleep", lambda _s: None)
    counter = tmp_path / "n"
    done = msys_retry.run(flaky(counter, 2, "  0 [main] sh (1) *** fatal error - x"), text=True)
    assert done.stdout.strip() == "ok" and counter.read_text() == "3"
    assert capsys.readouterr().err.count("aborted in the MSYS runtime; retrying") == 2


def test_an_ordinary_failure_is_not_retried_and_names_gits_stderr(tmp_path: Path) -> None:
    counter = tmp_path / "n"
    with pytest.raises(msys_retry.GitFailed) as failed:
        msys_retry.run(flaky(counter, 5, "fatal: bad object HEAD"), text=True)
    assert counter.read_text() == "1"  # one attempt only
    assert "fatal: bad object HEAD" in str(failed.value)


def test_three_aborts_then_a_final_failure_names_the_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(msys_retry.time, "sleep", lambda _s: None)
    counter = tmp_path / "n"
    with pytest.raises(msys_retry.GitFailed) as failed:
        msys_retry.run(flaky(counter, 9, "  0 [main] sh (1) *** fatal error - add_item"))
    assert counter.read_text() == "3"  # at most three attempts in all
    assert "add_item" in str(failed.value)


def test_retries_reach_the_terminal_summary_including_xdist_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review int7 D38-F2: a retry inside a passing test is captured by pytest, so the run's
    terminal summary lists every retry, also those an xdist worker made."""
    import conftest

    monkeypatch.setattr(msys_retry.time, "sleep", lambda _s: None)
    monkeypatch.setattr(msys_retry, "RETRIES", [])
    monkeypatch.setattr(conftest, "_WORKER_RETRIES", [])
    assert conftest.msys_retry_lines() == []
    msys_retry.run(flaky(tmp_path / "n", 1, "  0 [main] sh (1) *** fatal error - x"), text=True)
    assert len(msys_retry.RETRIES) == 1

    sent: dict[str, object] = {}
    worker = type("Session", (), {"config": type("Config", (), {"workeroutput": sent})()})()
    conftest.pytest_sessionfinish(worker)  # what an xdist worker sends at its end
    node = type("Node", (), {"workeroutput": sent})()
    conftest.pytest_testnodedown(node, None)  # what the controller receives
    lines = conftest.msys_retry_lines()
    assert lines[0].startswith("MSYS_RETRY: 2 fixture git command(s) retried")
    assert all("aborted in the MSYS runtime" in line for line in lines[1:])
