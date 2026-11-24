"""Test-side retry for git commands that die in Git for Windows' MSYS runtime (decision 0037).

Git for Windows runs its local transport (clone, fetch, push to a path) through its MSYS shell.
On the owner's PC that runtime intermittently aborted at start-up ("0 [main] sh (1234) ...
*** fatal error - add_item (...) failed") or stalled, more often under load (INT4-CP10-L1; the
host cause was fixed in decision 0037 section 5). Such a failure happens before git did anything,
so a test fixture may simply run the command again.

Only test fixtures use this, for their own setup (seeding a bare remote, cloning a collaborator
copy). Product code never retries: a failed git command there stays the typed error it is.
Every retry is reported on stderr, so a clean run shows whether one happened, and a final
failure names git's own stderr (review int6 D37-F3, CP5-F1, CP9-F1).
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Any

# The MSYS runtime's own diagnostic prefix ("<n> [main] <program> (<pid>)") or its fatal-error
# line; git's ordinary errors ("fatal: ...") never match.
MSYS_FAILURE = re.compile(r"\[main\] \S+ \(\d+\)|\*\*\* fatal error - ")
ATTEMPTS = 3
# Every retry in this process; tests/conftest.py prints them in the terminal summary,
# including those of xdist workers (review int7 D38-F2: pytest hides a passing test's stderr).
RETRIES: list[str] = []


class GitFailed(subprocess.CalledProcessError):
    """A fixture git command that failed: its message carries the end of git's stderr."""

    def __str__(self) -> str:
        stderr = self.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        tail = (stderr or "").strip()[-600:]
        return f"{super().__str__()} stderr: {tail or '(empty)'}"


def runtime_failure(stderr: str | bytes | None) -> bool:
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return bool(stderr) and MSYS_FAILURE.search(stderr) is not None


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run(argv, capture_output=True, check=True, **kwargs)``, run again (up to
    three attempts in all) only when it failed inside the MSYS runtime."""
    for attempt in range(1, ATTEMPTS + 1):
        done = subprocess.run(argv, capture_output=True, check=False, **kwargs)
        if done.returncode == 0 or attempt == ATTEMPTS or not runtime_failure(done.stderr):
            break
        message = (
            f"msys_retry: attempt {attempt} of {argv[:3]} aborted in the MSYS runtime; retrying"
        )
        RETRIES.append(message)
        print(message, file=sys.stderr, flush=True)
        time.sleep(attempt)
    if done.returncode != 0:
        raise GitFailed(done.returncode, argv, done.stdout, done.stderr)
    return done
