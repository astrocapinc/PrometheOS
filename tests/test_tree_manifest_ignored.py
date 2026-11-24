"""The candidate tree manifest (tools/tree_manifest.py) holds no file Git ignores (decision 0055).

A build output that Git ignores but the manifest counts would change a checkpoint run's
candidate while it runs: the CP19 suite builds the desktop app inside the run, and the
desktop app's and the CP19.1 spike's outputs (about 11,000 files) were counted until 0055.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import tree_manifest  # noqa: E402


def test_the_manifest_of_this_checkout_holds_no_file_git_ignores() -> None:
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("needs this checkout's Git (not a pass)")
    paths = [f["path"] for f in tree_manifest.build_manifest(REPO)["files"]]
    out = subprocess.run(
        ["git", "-c", "core.quotepath=off", "check-ignore", "-z", "--stdin"],
        cwd=REPO, input="\0".join(paths).encode("utf-8"), capture_output=True, check=False,
    )  # fmt: skip
    assert out.returncode in (0, 1), out.stderr  # 1: nothing is ignored
    ignored = [p for p in out.stdout.decode("utf-8").split("\0") if p]
    assert ignored == [], ignored[:20]


@pytest.mark.parametrize(
    "path",
    [
        "desktop/app/build/core/python.exe",
        "desktop/app/dist/win-unpacked/PrometheOS.exe",
        "desktop/spike/tauri/src-tauri/target/release/app.exe",
        "desktop/spike/pywebview/PrometheOSSpikePywebview.spec",
    ],
)
def test_desktop_build_outputs_are_not_candidate_files(path: str) -> None:
    assert tree_manifest.excluded(path)


@pytest.mark.parametrize(
    "path", ["desktop/app/main.js", "desktop/app/build_core.py", "desktop/app/walk/walk19.spec.ts"]
)
def test_the_desktop_apps_own_files_stay_candidate_files(path: str) -> None:
    assert not tree_manifest.excluded(path)
