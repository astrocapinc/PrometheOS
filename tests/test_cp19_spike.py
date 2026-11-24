"""C19-SPIKE (CP19.1, decisions 0047, 0049 and 0050): the spike's record holds together.

The spike itself (three prototypes measured three times each and installed per user) ran once;
its evidence is retained in development/receipts/CP19/s3b-spike-20261016. This test checks the
record a reviewer reads: every candidate has a recorded working prototype for the tray, the
notification and the hotkey, a measured idle memory and installer size, and a per-user install
without elevation; the decision records cover every stated criterion; and the choice is
registered in spec/09 and development/dependencies.json and used by the shipped shell.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import requires_private_records

# The spike's retained record is private evidence (decision 0072).
pytestmark = requires_private_records

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / "development" / "receipts" / "CP19" / "s3b-spike-20261016"
CANDIDATES = ("electron", "pywebview", "tauri")


def test_every_candidate_has_a_measured_working_prototype() -> None:
    summary = json.loads((EVIDENCE / "summary.json").read_text(encoding="utf-8"))
    for name in CANDIDATES:
        row = summary[name]
        assert row["runs"] >= 3 and row["all_steps_ok"] is True, name
        assert row["versions"], name
        assert row["shell_private_mib"] > 0 and row["sidecar_private_mib"] > 0, name
        for n in range(1, row["runs"] + 1):
            result = json.loads(
                (EVIDENCE / f"measure-{name}-{n}.result.json").read_text(encoding="utf-8")
            )
            assert_working(result["report"], (name, n))


def assert_working(report: dict, where: object) -> None:
    """The prototype's own report of one run: each feature worked (review s3d-a F3)."""
    assert report["ready"] is True, where
    assert report["tray"] is True, where
    assert report["notification"]["shown"] is True, where
    hotkey = report["hotkey"]
    assert hotkey["registered"] is True and hotkey["fired"] >= 1, where
    assert hotkey["capture_window"] is True, where
    assert report["errors"] == [], where


@pytest.mark.parametrize(
    "broken",
    [
        {"tray": False},
        {"notification": {"shown": False}},
        {"hotkey": {"registered": True, "fired": 0, "capture_window": True}},
        {"hotkey": {"registered": True, "fired": 1, "capture_window": False}},
        {"errors": ["the tray did not appear"]},
        {"ready": False},
    ],
)
def test_a_run_record_with_a_feature_that_failed_fails_the_check(broken: dict) -> None:
    good = json.loads((EVIDENCE / "measure-electron-1.result.json").read_text(encoding="utf-8"))[
        "report"
    ]
    assert_working(good, "good")
    with pytest.raises(AssertionError):
        assert_working({**good, **broken}, "broken")


def test_every_candidate_installs_per_user_without_elevation() -> None:
    for name in CANDIDATES:
        install = json.loads((EVIDENCE / f"install-{name}.json").read_text(encoding="utf-8"))
        assert install["all_ok"] is True, name
        assert install["elevated"] is False and install["hklm_changed"] is False, name
        assert install["installer_mib"] > 0, name


def test_the_decisions_cover_every_stated_criterion() -> None:
    decisions = REPO / "development" / "decisions"
    text = "\n".join(
        (decisions / name).read_text(encoding="utf-8").lower()
        for name in ("0047-cp19-1-desktop-spike.md", "0050-cp19-1-spike-completion.md")
    )
    for criterion in (
        "without admin",
        "tray",
        "notification",
        "hotkey",
        "installer size",
        "idle memory",
        "licence",
        "python 3.14",
        "update path",
        "signing",
    ):
        assert criterion in text, criterion
    assert "electron" in text and "tauri" in text and "pywebview" in text


def test_the_choice_is_registered_and_shipped() -> None:
    spec09 = next((REPO / "spec").glob("09*.md")).read_text(encoding="utf-8")
    row = next(line for line in spec09.splitlines() if "Desktop shell framework" in line)
    assert "Electron 44.5.1" in row and "0050" in row
    package = json.loads((REPO / "desktop" / "app" / "package.json").read_text(encoding="utf-8"))
    version = package["devDependencies"]["electron"]
    register = json.loads((REPO / "development" / "dependencies.json").read_text("utf-8"))
    entries = register["packages"] if isinstance(register, dict) else register
    electron = [e for e in entries if e["name"] == "electron" and e["ecosystem"] == "npm"]
    assert [(e["version"], e["scope"]) for e in electron] == [(version, "desktop_runtime")]
    assert version == "44.5.1"
    main = (REPO / "desktop" / "app" / "main.js").read_text(encoding="utf-8")
    assert 'require("electron")' in main
