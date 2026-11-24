"""The dependency register's scope must match the lock (CP17 session finding at decision 0053:
a package moved from the core to an optional extra passed while still registered as runtime)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import dependency_register as register  # noqa: E402


def with_scope(tmp_path: Path, ecosystem: str, wanted: str) -> tuple[Path, str]:
    data = json.loads(register.REGISTER.read_text(encoding="utf-8"))
    for entry in data["packages"]:
        if entry.get("ecosystem", "pypi") == ecosystem and entry["scope"] == wanted:
            entry["scope"] = {"pypi": "development", "npm": "hub_development"}[ecosystem]
            if wanted.startswith("desktop_"):
                entry["scope"] = "desktop_development"
            path = tmp_path / "dependencies.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return path, entry["name"]
    raise AssertionError(f"no {ecosystem} {wanted} package registered")


def test_the_register_passes_as_committed() -> None:
    assert register.check() == []


@pytest.mark.parametrize(
    ("ecosystem", "scope"),
    [("pypi", "runtime"), ("npm", "hub_runtime"), ("npm", "desktop_runtime")],
)
def test_a_registered_scope_that_differs_from_the_lock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ecosystem: str, scope: str
) -> None:
    path, name = with_scope(tmp_path, ecosystem, scope)
    monkeypatch.setattr(register, "REGISTER", path)
    failures = register.check()
    assert any(name in f and f"the lock makes it {scope}" in f for f in failures), failures


def test_the_desktop_lock_is_registered_both_ways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 0054: desktop/app's npm lock is registered like the hub's. Electron, which the
    installer redistributes, is desktop_runtime; a package missing from the register, or one
    registered beyond the lock, fails."""
    data = json.loads(register.REGISTER.read_text(encoding="utf-8"))
    desktop = [p for p in data["packages"] if str(p.get("scope", "")).startswith("desktop_")]
    runtime = {(p["name"], p.get("ecosystem")) for p in desktop if p["scope"] == "desktop_runtime"}
    assert runtime == {
        ("electron", "npm"),
        ("cpython", "binary"),
        ("nsis", "binary"),
        ("elevate.exe", "binary"),
    }
    dropped = next(p for p in desktop if p["name"] == "electron-builder")
    data["packages"].remove(dropped)
    data["packages"].append({**dropped, "name": "not-in-the-lock"})
    path = tmp_path / "dependencies.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(register, "REGISTER", path)
    failures = register.check()
    assert f"unregistered desktop package electron-builder@{dropped['version']}" in failures
    assert (
        f"registered desktop package not in lock not-in-the-lock@{dropped['version']}" in failures
    )


def test_a_package_reached_through_an_extra_gets_the_scope_of_what_asks_for_it() -> None:
    """Review s3c2-a F3: mcp needs pyjwt[crypto], whose extra brings cryptography (and cffi,
    pycparser): they ship in the core, so they are runtime, not development."""
    locked = register.locked_packages()
    for name in ("cryptography", "cffi", "pycparser"):
        assert locked[name]["scope"] == "runtime", name


def test_electrons_register_entry_carries_chromiums_notices() -> None:
    """Review int8 CP19-F5 (decision 0056): the installer redistributes Electron's binaries, so
    its entry names Chromium's notice file (dist/LICENSES.chromium.html), pinned by digest, and
    the entry equals what the register reads from the installed package."""
    committed = json.loads((ROOT / "development" / "dependencies.json").read_text("utf-8"))
    entry = next(
        e for e in committed["packages"] if e["name"] == "electron" and e["ecosystem"] == "npm"
    )
    files = {f["file"]: f["sha256"] for f in entry["license_files"]}
    notices = "desktop/app/node_modules/electron/dist/LICENSES.chromium.html"
    assert notices in files and len(files[notices]) == 64
    installed = ROOT / notices
    if not installed.is_file():
        pytest.skip("desktop/app/node_modules is not installed here (not a pass)")
    computed = next(e for e in register.npm_entries(register.DESKTOP) if e["name"] == "electron")
    assert computed["license_files"] == entry["license_files"]


def test_the_build_closure_is_pinned_and_registered_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 0057: the offline wheel builds float no build dependency. The pinned closure
    equals the register's build entries, a drift either way fails, and both wheel builds pass
    the constraints file."""
    pins = register.build_constraints()
    assert ("hatchling", "1.32.4") in pins
    recorded = {
        (p["name"], p["version"])
        for p in json.loads(register.REGISTER.read_text(encoding="utf-8"))["packages"]
        if p.get("scope") == "build"
    }
    assert recorded == pins
    drifted = tmp_path / "build-constraints.txt"
    drifted.write_text(
        register.BUILD_CONSTRAINTS.read_text(encoding="utf-8").replace(
            "pluggy==1.6.0", "pluggy==1.7.0"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(register, "BUILD_CONSTRAINTS", drifted)
    failures = register.check()
    assert "build constraint pluggy==1.7.0 not registered" in failures
    assert "registered build package pluggy==1.6.0 not pinned" in failures
    for tool in ("desktop/app/build_core.py", "tools/clean_install_check.py"):
        text = (ROOT / tool).read_text(encoding="utf-8")
        assert '"--build-constraints"' in text and '"build-constraints.txt"' in text, tool


def test_uvs_build_constraints_equal_the_pinned_closure(tmp_path: Path) -> None:
    """Decision 0059: pyproject's [tool.uv] build-constraint-dependencies (used by uv sync's
    editable build) is the same closure as build-constraints.txt; a drift is seen."""
    assert register.uv_build_constraints() == register.build_constraints()
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tomlkit==0.15.1"' in text
    (tmp_path / "pyproject.toml").write_text(
        text.replace('"tomlkit==0.15.1"', '"tomlkit==0.15.2"'), encoding="utf-8"
    )
    drifted = register.uv_build_constraints(tmp_path)
    assert drifted ^ register.build_constraints() == {("tomlkit", "0.15.1"), ("tomlkit", "0.15.2")}
