"""Project maintenance lock released by the operating system on process death.

An exclusive byte-range lock on ``<store>/maintenance.lock`` (msvcrt on
Windows, fcntl elsewhere). There is no PID file and no force-unlock: a killed
holder's lock disappears with its handle. Acquisition is bounded and reports
``busy``. Backup, restore staging, orphan collection and purge hold it.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from prometheos.domain.models import PrometheosError
from prometheos.store import admission

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - exercised on Linux CI
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def maintenance_lock(store: Path, timeout: float = 5.0) -> Iterator[None]:
    admission.require_before_maintenance()
    path = store / "maintenance.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            # Give the lock byte a body. Another opener of the new file may have written and
            # locked it first: on Windows the write then fails (a locked range), and it is not
            # needed; wait for the lock below instead (decision 0038's guard; 0048, D38-F1).
            with contextlib.suppress(OSError):
                os.write(fd, b"\0")
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise PrometheosError(
                    "busy",
                    "another maintenance operation (backup, restore or purge) is running",
                    {"lock": "maintenance"},
                )
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
