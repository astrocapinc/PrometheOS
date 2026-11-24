"""tests/conftest.py tooling (decision 0048): the short default basetemp on Windows."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import conftest
import pytest


def fake_config(basetemp: str | None, worker: bool = False) -> SimpleNamespace:
    config = SimpleNamespace(option=SimpleNamespace(basetemp=basetemp))
    if worker:
        config.workerinput = {}  # an xdist worker
    return config


@pytest.mark.skipif(sys.platform != "win32", reason="the MAX_PATH default is Windows-only")
def test_a_run_without_basetemp_gets_a_short_one_under_temp() -> None:
    config = fake_config(None)
    conftest.short_basetemp(config)  # type: ignore[arg-type]
    chosen = Path(config.option.basetemp)
    assert chosen == Path(tempfile.gettempdir()) / "ptb" / str(os.getpid())
    # Shorter than pytest's default root, which adds pytest-of-<user>\pytest-NNN.
    default = Path(tempfile.gettempdir()) / f"pytest-of-{os.getlogin()}" / "pytest-100"
    assert len(str(chosen)) < len(str(default))


def test_a_given_basetemp_and_xdist_workers_are_left_alone() -> None:
    given = fake_config("C:/short")
    conftest.short_basetemp(given)  # type: ignore[arg-type]
    assert given.option.basetemp == "C:/short"
    worker = fake_config(None, worker=True)
    conftest.short_basetemp(worker)  # type: ignore[arg-type]
    assert worker.option.basetemp is None  # the controller's choice reaches it from xdist


@pytest.mark.skipif(sys.platform != "win32", reason="the MAX_PATH default is Windows-only")
def test_old_run_folders_are_removed_and_recent_ones_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review s10f-a F5: folders older than a day go; a recent one (possibly another run that
    is still going) stays."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    root = tmp_path / "ptb"
    old, recent = root / "111", root / "222"
    for folder in (old, recent):
        (folder / "x").mkdir(parents=True)
    stale = time.time() - conftest.BASETEMP_KEEP_SECONDS - 60
    os.utime(old, (stale, stale))
    config = fake_config(None)
    conftest.short_basetemp(config)  # type: ignore[arg-type]
    assert not old.exists() and recent.exists()
    assert Path(config.option.basetemp) == root / str(os.getpid())
