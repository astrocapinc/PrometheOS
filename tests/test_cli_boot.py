from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from prometheos import __version__


def run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "prometheos", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


def clean_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if "KEY" not in k and "TOKEN" not in k}
    env["PROMETHEOS_HOME"] = str(tmp_path / "home")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_help_and_version_need_no_project_or_credentials(tmp_path: Path) -> None:
    env = clean_env(tmp_path)
    help_out = run(["--help"], tmp_path, env)
    assert help_out.returncode == 0, help_out.stderr
    assert "PrometheOS" in help_out.stdout
    version_out = run(["--version"], tmp_path, env)
    assert version_out.returncode == 0
    assert version_out.stdout.strip() == f"prometheos {__version__}"
    # Neither command creates a store, registry or files in the working directory.
    assert list(tmp_path.iterdir()) == []


def test_schema_listing_and_unknown_schema(tmp_path: Path) -> None:
    env = clean_env(tmp_path)
    listing = run(["schema", "list"], tmp_path, env)
    assert listing.returncode == 0
    assert "request" in listing.stdout.split()
    missing = run(["schema", "show", "nope"], tmp_path, env)
    assert missing.returncode == 2
    assert missing.stdout == ""
