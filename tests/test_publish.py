"""The public export's privacy scan, allow-list and notices (decision 0072)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import privacy_scan as ps  # noqa: E402
import public_export as pe  # noqa: E402
import third_party_notices as tpn  # noqa: E402

# Assembled at run time, so this file holds no literal home folder or address of its own.
NAME = "jsmith2"
MAIL = "jane.doe" + "@" + "uni-somewhere.edu"


def rules(text: str, rel: str = "src/x.py", terms: set[str] | None = None, names=None) -> list:
    return [f.rule for f in ps.scan_text(rel, text, terms or set(), names or set())]


def test_a_real_home_folder_is_found_and_a_generic_one_is_not() -> None:
    assert rules("open C:\\Users\\" + NAME + "\\notes.txt") == ["home-path"]
    assert rules(f"open /c/Users/{NAME}/notes.txt") == ["home-path"]
    assert rules(f"cd /home/{NAME}/x") == ["home-path"]
    assert rules(r"C:\Users\someone\x and C:/Users/researcher/y and /home/user/z") == []
    # A URL's path is not a home folder.
    assert rules('"origin_uri": "zotero://users/0/items/DRAG0001"') == []


def test_an_address_is_found_outside_example_domains_and_licence_files() -> None:
    assert rules(f"contact {MAIL}") == ["email"]
    assert rules("contact jane@example.org or a@b.test") == []
    assert rules(f"Copyright {MAIL}", rel="hub/LICENSE") == []
    assert rules('"git@github.com:someone/repo.git"') == []
    assert rules("https://user:token@git.example-host.com/x") == []


def test_tokens_and_keys_are_found_and_the_allow_mark_skips_a_line() -> None:
    token = "ghp_" + "a" * 36
    assert rules(f"token = {token}") == ["secret"]
    assert rules("-----BEGIN OPENSSH PRIVATE " + "KEY-----") == ["secret"]
    assert rules(f"token = {token}  # privacy-scan: allow") == []


def test_denylist_terms_and_harvested_research_names_match_case_insensitively(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research"
    (research / "Pump Rig" / "slprj").mkdir(parents=True)
    (research / "Pump Rig" / "ValveMap_Series.m").write_text("x")
    (research / "Pump Rig" / "slprj" / "cacheMap_x.mat").write_text("x")
    (research / "Pump Rig" / "README.md").write_text("x")
    names = ps.harvest_names(research)
    assert {"pump rig", "valvemap_series", "valvemap_series.m"} <= names
    assert "cachemap_x" not in names, "tool-generated caches are not research names"
    assert "readme.md" not in names, "a generic file name is not a research name"
    assert rules("see valvemap_SERIES.m", names=names) == ["research-name"]
    assert rules("ask MyInstitute", terms={"myinstitute"}) == ["denylist"]


def test_findings_are_masked_and_the_scan_fails_on_a_tree(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(f"mail {MAIL}\n", encoding="utf-8")
    (tmp_path / "Pump Rig.txt").write_text("clean\n", encoding="utf-8")
    findings = ps.scan(tmp_path, set(), {"pump rig"})
    assert {(f.path, f.rule) for f in findings} == {
        ("a.md", "email"),
        ("Pump Rig.txt", "research-name"),
    }
    assert all("doe" not in f.detail for f in findings)
    assert ps.main([str(tmp_path)]) == 1


def test_the_export_allow_list_keeps_private_evidence_out() -> None:
    chosen = pe.selected(
        [
            "src/prometheos/cli.py",
            "development/dependencies.json",
            "development/state.json",
            "development/receipts/CP0/r.json",
            "development/decisions/0001.md",
            "hub/src/App.tsx",
            "hub/screenshots/queue.png",
            "desktop/app/main.js",
            "desktop/spike/README.md",
            ".claude/launch.json",
            "README.md",
        ]
    )
    assert chosen == [
        "README.md",
        "desktop/app/main.js",
        "development/dependencies.json",
        "hub/src/App.tsx",
        "src/prometheos/cli.py",
    ]


def test_the_export_refuses_a_folder_inside_the_repository(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="outside the repository"):
        pe.main(["--out", str(ROOT / "scratch" / "export-here"), "--allow-dirty"])


def test_the_identity_must_be_a_name_and_an_address() -> None:
    assert pe.split_identity("A B <a@b.invalid>") == ("A B", "a@b.invalid")
    with pytest.raises(SystemExit):
        pe.split_identity("no address")


@pytest.mark.skipif(not (ROOT / "hub" / "node_modules").is_dir(), reason="needs npm ci in hub/")
def test_the_committed_notices_match_the_register() -> None:
    done = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "third_party_notices.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


def test_the_notices_list_what_ships_and_never_the_development_tools() -> None:
    packages = tpn.load_register()
    bundled = {p["name"] for p in tpn.hub_bundled(packages)}
    assert {"react", "pdfjs-dist", "@codemirror/view"} <= bundled
    assert not any(n.startswith("@napi-rs/canvas") for n in bundled), "never bundled"
    assert "vite" not in bundled and "typescript" not in bundled


def test_a_short_research_name_counts_only_as_a_word_of_its_own() -> None:
    names = {"rig4"}
    assert rules("see the rIG4 model", names=names) == ["research-name"]
    assert rules("const xrig4y = 1", names=names) == []


def test_binary_files_are_scanned_in_utf16_too(tmp_path: Path) -> None:
    path = ("C:\\Users\\" + NAME + "\\x").encode("utf-16-le")
    (tmp_path / "a.bin").write_bytes(b"\0\0" + path)
    assert [f.rule for f in ps.scan(tmp_path, set(), set())] == ["home-path"]


def test_the_export_refuses_without_the_owner_terms_or_research_names(tmp_path: Path) -> None:
    research = tmp_path / "research"
    research.mkdir()
    (research / "Pump_Rig.m").write_text("x")
    empty = tmp_path / "denylist.txt"
    empty.write_text("# nothing\n")
    terms = tmp_path / "terms.txt"
    terms.write_text("myinstitute\n")
    with pytest.raises(SystemExit, match="no owner denylist"):
        pe.owner_checks([empty], [research], unchecked=False)
    with pytest.raises(SystemExit, match="no research names"):
        pe.owner_checks([terms], [], unchecked=False)
    assert pe.owner_checks([terms], [research], unchecked=False) == (
        {"myinstitute"},
        {"pump_rig", "pump_rig.m"},
    )
    assert pe.owner_checks([empty], [], unchecked=True) == (set(), set())


def _build_app():  # type: ignore[no-untyped-def]
    import importlib.util

    path = ROOT / "desktop" / "app" / "build_app.py"
    spec = importlib.util.spec_from_file_location("build_app", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_release_scan_accepts_only_the_published_notice_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dogfood P18: after E08 put the owner's name in NOTICE, every release build failed its
    privacy scan on that line. A finding is accepted only in a shipped NOTICE file, on a line
    exactly as the repository's committed NOTICE has it; the name anywhere else, or on another
    NOTICE line, is still a finding (review kers-p18-a)."""
    denylist = tmp_path / "full.txt"
    denylist.write_text("Ada Example\nada@example.invalid\n", "utf-8")
    monkeypatch.setattr(ps, "DEFAULT_DENYLIST", denylist)
    repo = tmp_path / "repo"
    repo.mkdir()
    notice = "PrometheOS\nCopyright 2026 Ada Example\n"
    (repo / "NOTICE").write_text(notice, "utf-8")
    build_app = _build_app()
    monkeypatch.setattr(build_app, "REPO", repo)  # no dogfood/ names here
    resources = tmp_path / "resources"
    (resources / "core" / "licenses").mkdir(parents=True)
    (resources / "NOTICE").write_text(notice, "utf-8")
    (resources / "core" / "licenses" / "NOTICE").write_text(notice, "utf-8")
    assert build_app.privacy_problems(resources) == []
    (resources / "about.txt").write_text("built by Ada Example\n", "utf-8")
    assert [p for p in build_app.privacy_problems(resources) if "about.txt" in p]
    (resources / "about.txt").unlink()
    (resources / "NOTICE").write_text(notice + "Maintainer: Ada Example\n", "utf-8")
    assert [p for p in build_app.privacy_problems(resources) if "NOTICE:3" in p]
