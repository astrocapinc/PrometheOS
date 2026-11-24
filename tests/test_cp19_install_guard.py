"""The installer check refuses to run where PrometheOS is installed (integration run int11).

The per-user installer upgrades an existing installation in place, so running the check on the
owner's computer replaced the owner's app. The check must refuse before starting the installer.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows installer check")

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "install_check_under_test", ROOT / "desktop" / "app" / "test" / "install_check.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_existing_installation_is_refused_before_the_installer_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _load()
    app = tmp_path / "app"
    (app / "dist").mkdir(parents=True)
    (app / "dist" / "PrometheOS Setup 0.1.0.exe").write_bytes(b"not an installer")
    monkeypatch.setattr(check, "APP", app)
    existing = {"ec60f2c1": {"DisplayName": "PrometheOS 0.1.0", "UninstallString": "x"}}
    monkeypatch.setattr(
        check,
        "uninstall_keys",
        lambda hive: existing if hive == check.winreg.HKEY_CURRENT_USER else {},
    )
    started: list[object] = []

    def no_installer(args, *a, **k):  # the guard must refuse before any installer starts
        if isinstance(args, list) and str(args[0]).endswith(".exe"):
            started.append(args)
            raise AssertionError("the installer was started")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(check.subprocess, "run", no_installer)
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["install_check.py", str(out)])
    assert check.main() == 2
    assert started == []
    record = json.loads((out / "install.json").read_text("utf-8"))
    assert "already installed" in record["refused"]
    assert "PrometheOS 0.1.0" in record["refused"]
