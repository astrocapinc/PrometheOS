"""C01-PATH: traversal, archive escape, reparse paths and corrupt/missing media are
rejected safely."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cp1_support import FULL, OWNER, Env, create_entry

from prometheos.domain.models import PrometheosError
from prometheos.sources.paths import normalize_relative, resolve_in_root
from prometheos.store.home import check_store_location


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "sub/../../outside.txt",
        "sub/./notes.txt",
        "sub//notes.txt",
        "sub/",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "c:relative.txt",
        "\\\\server\\share\\file.txt",
        "//server/share/file.txt",
        "\\\\?\\C:\\file.txt",
        "\\\\.\\COM1",
        "CON",
        "nul.txt",
        "sub/aux.md",
        "com1",
        "file.txt:stream",
        "trailing.",
        "trailing ",
        "",
    ],
)
def test_unsafe_relative_paths_rejected(path: str) -> None:
    with pytest.raises(PrometheosError) as err:
        normalize_relative(path)
    assert err.value.code in ("policy_denied", "validation_error")


def test_safe_relative_paths_normalised() -> None:
    assert normalize_relative("sub\\notes.txt") == "sub/notes.txt"
    assert normalize_relative("sub/notes.txt") == "sub/notes.txt"


def test_import_refuses_files_outside_root_and_unknown_roots(env: Env, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    for paths, alias in (
        ([str(outside)], "research"),
        (["../outside.txt"], "research"),
        (["baseline.md"], "nope"),
    ):
        with pytest.raises(PrometheosError) as err:
            env.svc.import_plan(
                env.project_id,
                root_alias=alias,
                paths=paths,
                retention="copy",
                sensitivity="normal",
            )
        assert err.value.code == "policy_denied"


def test_absolute_path_inside_root_is_accepted(env: Env) -> None:
    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=[str(env.research / "sub" / "notes.txt")],
        retention="copy",
        sensitivity="normal",
    )
    assert plan["items"][0]["relative_path"] == "sub/notes.txt"


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
def test_junction_inside_root_cannot_escape(env: Env, tmp_path: Path) -> None:
    secret_dir = tmp_path / "private"
    secret_dir.mkdir()
    (secret_dir / "key.txt").write_text("do not import\n", encoding="utf-8")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(env.research / "escape"), str(secret_dir)],
        capture_output=True,
        check=False,
    )
    assert made.returncode == 0, made.stderr
    with pytest.raises(PrometheosError) as err:
        env.svc.import_plan(
            env.project_id,
            root_alias="research",
            paths=["escape/key.txt"],
            retention="copy",
            sensitivity="normal",
        )
    assert err.value.code == "policy_denied"
    with pytest.raises(PrometheosError):
        resolve_in_root(env.research, "escape/key.txt")


def test_symlinked_file_rejected(env: Env, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("x\n", encoding="utf-8")
    try:
        os.symlink(target, env.research / "link.txt")
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    with pytest.raises(PrometheosError) as err:
        env.svc.import_plan(
            env.project_id,
            root_alias="research",
            paths=["link.txt"],
            retention="copy",
            sensitivity="normal",
        )
    assert err.value.code == "policy_denied"


def test_directories_and_missing_files_are_not_imported(env: Env) -> None:
    for path, code in (("sub", "policy_denied"), ("missing.md", "source_unavailable")):
        with pytest.raises(PrometheosError) as err:
            env.svc.import_plan(
                env.project_id,
                root_alias="research",
                paths=[path],
                retention="copy",
                sensitivity="normal",
            )
        assert err.value.code == code


def _hostile_backup(tmp_path: Path, good: Path, member: str, payload: bytes = b"x") -> Path:
    hostile = tmp_path / f"hostile-{abs(hash(member))}.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(hostile, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info))
        dst.writestr(member, payload)
    return hostile


@pytest.mark.parametrize(
    "member",
    [
        "../evil.txt",
        "../../evil.txt",
        "/abs/evil.txt",
        "C:/evil.txt",
        "media/../../evil.txt",
        "media/aa/" + "a" * 64 + "/../../evil",
        "record.sqlite-journal",
        "extra/readme.txt",
    ],
)
def test_archive_escape_members_rejected(env: Env, tmp_path: Path, member: str) -> None:
    good = Path(env.svc.backup(env.project_id, tmp_path / "good.zip", FULL)["backup"])
    hostile = _hostile_backup(tmp_path, good, member)
    with pytest.raises(PrometheosError) as err:
        env.svc.verify_backup(hostile)
    assert err.value.code in ("policy_denied", "validation_error")
    target = tmp_path / "restore-here"
    with pytest.raises(PrometheosError):
        env.svc.restore(hostile, target, caller=FULL)
    assert not target.exists()
    assert not (tmp_path / "evil.txt").exists()
    assert not list(tmp_path.glob(".restore-here.restore-*"))


def test_duplicate_archive_member_rejected(env: Env, tmp_path: Path) -> None:
    good = Path(env.svc.backup(env.project_id, tmp_path / "good.zip", FULL)["backup"])
    with pytest.warns(UserWarning):  # zipfile warns about the duplicate name it writes
        hostile = _hostile_backup(tmp_path, good, "manifest.json", b"{}")
    with pytest.raises(PrometheosError) as err:
        env.svc.verify_backup(hostile)
    assert err.value.code == "validation_error"


def test_corrupt_and_missing_media_are_reported(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app import sources

    monkeypatch.setattr(sources, "INLINE_LIMIT", 1024)
    result = env.import_files(["artifact.bin"], retention="copy")
    item = result["plan"]["items"][0]
    blob = env.store_dir / "media" / item["sha256"][:2] / item["sha256"]
    blob.write_bytes(b"corrupted")
    obs = env.svc.source_check(env.project_id, [item["version_id"]], save=False)["observations"][0]
    assert obs["retained_digest_ok"] is False
    with pytest.raises(PrometheosError) as err:
        env.svc.backup(env.project_id, tmp_path / "refused.zip", FULL)
    assert err.value.code == "io_error"
    assert not (tmp_path / "refused.zip").exists()
    blob.unlink()
    version = env.svc.get(env.project_id, OWNER, item["version_id"], None)
    assert version["body"]["availability"] == "missing"
    evidence = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "json", "pointer": "/x"},
        "quote_sha256": None,
    }
    with pytest.raises(PrometheosError) as err:
        env.apply([create_entry(evidence=[evidence])])
    assert err.value.code == "source_unavailable"


@pytest.mark.parametrize(
    ("location", "accepted"),
    [
        ("\\\\server\\share\\store", False),
        ("//server/share/store", False),
        ("C:/Users/someone/OneDrive/research/store", False),
        ("C:/Users/someone/Dropbox/store", False),
    ],
)
def test_network_and_synchronised_store_locations_refused(location: str, accepted: bool) -> None:
    assert check_store_location(Path(location)).accepted is accepted


def test_local_store_location_accepted(tmp_path: Path) -> None:
    report = check_store_location(tmp_path / "store")
    assert report.accepted, report.reason


def test_init_refuses_synchronised_store(tmp_path: Path) -> None:
    from prometheos.app.service import Service

    svc = Service(home=tmp_path / "home")
    with pytest.raises(PrometheosError) as err:
        svc.init_project(title="t", objective="o", store=tmp_path / "OneDrive" / "store")
    assert err.value.code == "policy_denied"
    assert not (tmp_path / "OneDrive").exists()
