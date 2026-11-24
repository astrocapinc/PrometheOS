"""Files that hold a local secret (an agent token, the approval binding key).

Each is created exclusively and restricted to the current user *before* any secret is written
into it: mode 0600 on POSIX; on Windows an explicit ACL granting only the current user
(``icacls``, inheritance removed). A file that cannot be restricted is removed and refused,
never left with its default permissions (decision 0016 rounds 2 and 3, review F15 / R2-6).

As everywhere in PrometheOS this guards against accidental disclosure, not against software
running as the same OS user.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from prometheos.domain.models import PrometheosError


def restrict_to_owner(path: Path) -> bool:
    """Restrict ``path`` to the current user; ``False`` when that could not be done."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return (path.stat().st_mode & 0o077) == 0
    user = os.environ.get("USERNAME")
    if not user:
        return False
    try:
        done = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def write_private(path: Path, data: bytes) -> None:
    """Create ``path`` (never overwrite), restrict it to the owner while it is still empty,
    then write ``data``. Refuses (``io_error``) and removes the empty file if it cannot be
    restricted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass  # created empty, exclusively
    if not restrict_to_owner(path):
        path.unlink(missing_ok=True)
        raise PrometheosError(
            "io_error", "the secret file could not be restricted to the current user", {}
        )
    with path.open("r+b") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
