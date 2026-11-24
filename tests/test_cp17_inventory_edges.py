"""C17-INVENTORY robustness (CP17.1 round 2; review-precheck-cp17-1-a F1-F11): what a real
folder contains never aborts a scan. Odd names, old times, invalid Unicode, very long paths,
corrupt or strange archives, refused folders, vanishing entries and cloud reparse points each
give a row (or a summarised folder row) and the scan completes; the root digest detects every
kind of change it claims to; placeholder folders are not entered; excluded folders are not
listed."""

from __future__ import annotations

import io
import os
import stat
import struct
import sys
import warnings
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, Env

from prometheos.adoption import inventory as inv
from prometheos.adoption.contracts import (
    MEMBER_SEPARATOR,
    AdoptionFile,
    ArchiveLimits,
    ScanConfig,
    disk_path,
    escape_segment,
)
from prometheos.domain.models import MAX_INT


def rows_of(root: Path, config: ScanConfig | None = None, **kw: Any) -> dict[str, AdoptionFile]:
    return {r.path: r for r in inv.inventory(root, 1, config, **kw).rows}


def zip_of(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 5, 9, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()


# ----- F1: names and stats that real folders contain ----------------------------------------


def test_a_folder_name_ending_in_bang_is_not_an_archive_member(tmp_path: Path) -> None:
    (tmp_path / "Done!").mkdir()
    (tmp_path / "Done!" / "x.txt").write_text("done\n", "utf-8")
    (tmp_path / "Done").write_bytes(zip_of([("x.txt", b"member\n")]))
    rows = rows_of(tmp_path)
    disk = rows[escape_segment("Done!") + "/x.txt"]
    member = rows["Done" + MEMBER_SEPARATOR + "x.txt"]
    assert disk.entry == "file" and disk.status == "read" and disk.parent is None
    assert member.entry == "member" and member.parent == "Done"
    assert disk.path != member.path, "the escape keeps the two apart"
    assert disk_path(disk.path) == "Done!/x.txt"


def test_old_times_and_long_paths_are_recorded(tmp_path: Path) -> None:
    old = tmp_path / "old.txt"
    old.write_text("from 1965\n", "utf-8")
    when = -150_000_000 * 10**9  # 1965
    os.utime(inv.os_path(old), ns=(when, when))
    deep = tmp_path
    for i in range(45):
        deep = deep / (
            f"a_very_long_folder_name_number_{i:02d}_for_paths_over_4096_chars_" + "x" * 45
        )
    os.makedirs(inv.os_path(deep))
    with open(inv.os_path(deep / "leaf.txt"), "w", encoding="utf-8") as handle:
        handle.write("deep\n")
    rows = rows_of(tmp_path)
    assert rows["old.txt"].mtime_us == when // 1000
    leaf = next(r for p, r in rows.items() if p.endswith("leaf.txt"))
    assert len(leaf.path) > 4096 and leaf.status == "read"


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS names may hold unpaired surrogates")
def test_a_name_that_is_not_valid_unicode_is_reported(tmp_path: Path) -> None:
    try:
        (tmp_path / "bad\udc80name.txt").write_bytes(b"x")
    except (OSError, UnicodeError):
        pytest.skip("the file system refused the name")
    (tmp_path / "fine.txt").write_bytes(b"y")
    rows = rows_of(tmp_path)
    assert rows["fine.txt"].status == "read"
    bad = [r for r in rows.values() if r.path != "fine.txt"]
    assert len(bad) == 1 and bad[0].status == "unreadable"
    assert "not valid Unicode" in (bad[0].reason or "") and bad[0].sha256 is None


# ----- F2: archives that are corrupt, odd or duplicated -------------------------------------


def corrupt_deflate(data: bytes, member: str) -> bytes:
    """``data`` (a ZIP) with ``member``'s deflate stream replaced by an invalid block type, so
    reading it raises ``zlib.error`` while the central directory stays valid."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo(member)
    raw = bytearray(data)
    header = info.header_offset
    name_len, extra_len = struct.unpack("<HH", raw[header + 26 : header + 30])
    start = header + 30 + name_len + extra_len
    raw[start : start + 4] = b"\xff\xff\xff\xff"  # BTYPE 11: reserved, "invalid block type"
    return bytes(raw)


def test_corrupt_packages_and_members_are_unreadable_rows(tmp_path: Path) -> None:
    package = zip_of(
        [
            ("[Content_Types].xml", b"<Types/>"),
            ("metadata/mwcoreProperties.xml", b"<contentType>x</contentType>" * 50),
        ]
    )
    (tmp_path / "model.slx").write_bytes(corrupt_deflate(package, "metadata/mwcoreProperties.xml"))
    inner = corrupt_deflate(zip_of([("data.txt", b"payload " * 200)]), "data.txt")
    (tmp_path / "outer.zip").write_bytes(
        zip_of(
            [
                (
                    "inner.zip",
                    zip_of([("x.zip", corrupt_deflate(zip_of([("y", b"y" * 500)]), "y"))]),
                ),
                ("broken.zip", b"PK\x03\x04" + b"\x00" * 40),
                ("ok.txt", b"ok"),
            ]
        )
    )
    (tmp_path / "nested.zip").write_bytes(
        corrupt_deflate(
            zip_of([("deflated.zip", zip_of([("a", b"a" * 900)])), ("ok.txt", b"ok")]),
            "deflated.zip",
        )
    )
    rows = rows_of(tmp_path)
    model = rows["model.slx"]
    assert model.status == "unreadable"
    assert model.reason == "package metadata unreadable: error", "zlib.error, named"
    member = rows["nested.zip!/deflated.zip"]
    assert member.status == "unreadable" and member.reason == "member read failed: error"
    assert rows["nested.zip!/ok.txt"].status == "listed", "the rest of the archive is kept"
    assert rows["outer.zip"].status == "read" and rows["outer.zip!/ok.txt"].status == "listed"
    assert inner  # built to show the stream is really invalid
    with pytest.raises(zlib.error):
        zipfile.ZipFile(io.BytesIO(inner)).read("data.txt")


def test_duplicate_and_odd_member_names_get_unique_rows(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w") as archive:
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate on purpose here
        for name in ("a.txt", "a.txt", "x/../a.txt", "a\\b.txt", "/abs.txt", "", "c!/d.txt"):
            info = zipfile.ZipInfo(name or "noname", date_time=(2026, 1, 5, 9, 0, 0))
            info.filename = name  # keep the stored name exactly as a hostile archive would
            archive.writestr(info, b"x")
    (tmp_path / "odd.zip").write_bytes(buffer.getvalue())
    rows = rows_of(tmp_path)
    members = [r for r in rows.values() if r.entry == "member"]
    assert len(members) == 7 and len({m.path for m in members}) == 7
    for member in members:
        assert member.parent == "odd.zip" and member.depth == 1
    stored = {(m.detail or {}).get("name_as_stored"): m.path for m in members}
    assert stored["x/../a.txt"].startswith("odd.zip!/x/a.txt")
    assert stored["/abs.txt"] == "odd.zip!/abs.txt"
    assert stored[""] == "odd.zip!/unnamed"
    assert sum(1 for m in members if (m.detail or {}).get("duplicate_of")) >= 1
    assert "odd.zip!/c!!/d.txt" in rows, "a '!' inside a member name is escaped"


def test_a_listing_that_fails_half_way_leaves_no_orphan_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "half.zip").write_bytes(
        zip_of([("a.txt", b"a"), ("b.txt", b"b"), ("c.zip", zip_of([("d", b"d")]))])
    )
    original = inv.Inventory._member
    calls = {"n": 0}

    def failing(self: inv.Inventory, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("simulated failure in the middle of a listing")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(inv.Inventory, "_member", failing)
    rows = rows_of(tmp_path)
    assert rows["half.zip"].status == "unreadable"
    assert not [p for p in rows if p.startswith("half.zip" + MEMBER_SEPARATOR)]


def test_member_count_is_limited_before_the_directory_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "many.zip").write_bytes(zip_of([(f"{i}.txt", b"") for i in range(300)]))

    def no_parsing(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the central directory was parsed before the member limit")

    monkeypatch.setattr(inv.zipfile, "ZipFile", no_parsing)
    rows = rows_of(tmp_path, ScanConfig(limits=ArchiveLimits(max_members=100)))
    assert rows["many.zip"].status == "limited", rows["many.zip"].reason
    assert rows["many.zip"].reason == "max_members: 300 > 100"


def test_opc_packages_obey_the_limits_too(tmp_path: Path) -> None:
    (tmp_path / "bomb.slx").write_bytes(
        zip_of([("[Content_Types].xml", b"<Types/>"), ("zeros.bin", b"\x00" * (4 * 1024 * 1024))])
    )
    rows = rows_of(tmp_path)
    assert rows["bomb.slx"].status == "limited" and rows["bomb.slx"].reason.startswith("max_ratio")


# ----- F3: refused folders and vanishing entries --------------------------------------------


def test_a_refused_folder_is_a_row_and_the_scan_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "secret.txt").write_text("x", "utf-8")
    (tmp_path / "open.txt").write_text("y", "utf-8")
    real = os.scandir

    def scandir(path: Any) -> Any:
        if str(path).rstrip("\\/").endswith("locked"):
            raise PermissionError(13, "Access is denied")
        return real(path)

    monkeypatch.setattr(inv.os, "scandir", scandir)
    rows = rows_of(tmp_path)
    locked = rows["locked"]
    assert locked.entry == "folder" and locked.status == "unreadable"
    assert locked.reason.startswith("PermissionError")
    assert rows["open.txt"].status == "read"
    first, second = inv.root_digest(tmp_path), inv.root_digest(tmp_path)
    assert first == second, "a folder that stays unreadable keeps the digest stable"


def test_an_entry_that_vanishes_during_the_walk_is_a_row(tmp_path: Path) -> None:
    (tmp_path / "gone.txt").write_text("x", "utf-8")
    (tmp_path / "here.txt").write_text("y", "utf-8")

    def stat_of(entry: os.DirEntry[str]) -> inv.Stat:
        if entry.name == "gone.txt":
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return inv.entry_stat(entry)

    rows = rows_of(tmp_path, stat_of=stat_of)
    assert rows["gone.txt"].status == "unreadable" and rows["here.txt"].status == "read"


# ----- F4, F5: cloud reparse points and placeholder folders ---------------------------------


def test_cloud_reparse_points_are_placeholders_and_junctions_are_links(tmp_path: Path) -> None:
    (tmp_path / "cloud.zip").write_bytes(zip_of([("x", b"x")]))
    (tmp_path / "junction").mkdir()
    (tmp_path / "junction" / "inside.txt").write_text("x", "utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder" / "child.txt").write_text("x", "utf-8")
    entered: list[str] = []

    def stat_of(entry: os.DirEntry[str]) -> inv.Stat:
        st = inv.entry_stat(entry)
        cloud = inv.FILE_ATTRIBUTE_REPARSE_POINT | inv.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        if entry.name == "cloud.zip":
            bits = st.attributes | cloud
            return inv.Stat(
                inv.kind_of(stat.S_IFREG, bits, 0x9000001A), st.size, st.mtime_ns, bits, 0x9000001A
            )
        if entry.name == "junction":
            bits = st.attributes | inv.FILE_ATTRIBUTE_REPARSE_POINT
            return inv.Stat(
                inv.kind_of(stat.S_IFDIR, bits, 0xA0000003), 0, st.mtime_ns, bits, 0xA0000003
            )
        if entry.name == "folder":
            bits = st.attributes | cloud
            return inv.Stat(
                inv.kind_of(stat.S_IFDIR, bits, 0x9000601A), 0, st.mtime_ns, bits, 0x9000601A
            )
        entered.append(entry.path)
        return st

    rows = rows_of(tmp_path, stat_of=stat_of)
    assert rows["cloud.zip"].status == "placeholder" and rows["cloud.zip"].sha256 is None
    assert rows["junction"].status == "link" and "junction/inside.txt" not in rows
    assert rows["folder"].entry == "folder" and rows["folder"].status == "placeholder"
    assert not any("child.txt" in p for p in entered), "a placeholder folder is never entered"
    digest = inv.root_digest(tmp_path, stat_of)
    assert digest == inv.root_digest(tmp_path, stat_of)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_a_real_junction_is_a_link(tmp_path: Path) -> None:
    import _winapi

    target = tmp_path / "target"
    target.mkdir()
    (target / "x.txt").write_text("x", "utf-8")
    root = tmp_path / "root"
    root.mkdir()
    _winapi.CreateJunction(str(target), str(root / "j"))
    rows = rows_of(root)
    assert rows["j"].status == "link" and "j/x.txt" not in rows


# ----- F6: what the root digest detects -----------------------------------------------------


@pytest.mark.parametrize("change", ["create", "delete", "rename", "resize", "retime", "attribute"])
def test_the_root_digest_detects_each_change(tmp_path: Path, change: str) -> None:
    target = tmp_path / "a.txt"
    target.write_text("12345", "utf-8")
    (tmp_path / "sub").mkdir()
    before = inv.root_digest(tmp_path)
    if change == "create":
        (tmp_path / "sub" / "new.txt").write_text("n", "utf-8")
    elif change == "delete":
        target.unlink()
    elif change == "rename":
        target.rename(tmp_path / "b.txt")
    elif change == "resize":
        stat = target.stat()
        target.write_text("123456", "utf-8")
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif change == "retime":
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    elif change == "attribute":
        if sys.platform != "win32":
            pytest.skip("Windows attributes")
        os.chmod(target, 0o444)  # sets FILE_ATTRIBUTE_READONLY
    after = inv.root_digest(tmp_path)
    if change == "attribute":
        os.chmod(target, 0o600)
    assert before != after, change


def test_a_file_that_grows_while_read_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "log.txt"
    target.write_text("x" * 100, "utf-8")

    def stat_of(entry: os.DirEntry[str]) -> inv.Stat:
        st = inv.entry_stat(entry)
        return inv.Stat(st.kind, 50, st.mtime_ns, st.attributes, st.reparse_tag)  # stale size

    rows = rows_of(tmp_path, stat_of=stat_of)
    row = rows["log.txt"]
    assert row.status == "unreadable" and "changed while it was read" in (row.reason or "")


# ----- F8: bounded integers and cross-field rules -------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"detail": {"compressed": 2**60}},
        {"path": "a\nb.txt"},
        {"status": "link"},
        {"entry": "link"},
        {"status": "read", "attributes": 0x400000},
    ],
)
def test_file_rows_refuse_inconsistent_values(change: dict[str, Any]) -> None:
    row = {
        "scan_gen": 1,
        "path": "a.txt",
        "parent": None,
        "depth": 0,
        "entry": "file",
        "size": 1,
        "mtime_us": 0,
        "attributes": 32,
        "sha256": "a" * 64,
        "text_sha256": None,
        "container": "binary",
        "status": "read",
        "reason": None,
        "zone": "unknown",
        "change": None,
        "detail": None,
    }
    AdoptionFile.model_validate(row)
    with pytest.raises(ValueError):
        AdoptionFile.model_validate({**row, **change})


def test_member_rows_must_sit_under_their_archive() -> None:
    base = {
        "scan_gen": 1,
        "path": "x.zip!/m.txt",
        "parent": "x.zip",
        "depth": 1,
        "entry": "member",
        "size": 1,
        "mtime_us": None,
        "attributes": None,
        "sha256": None,
        "text_sha256": None,
        "container": None,
        "status": "listed",
        "reason": None,
        "zone": "unknown",
        "change": None,
        "detail": {"crc32": "00000000", "compressed": 1},
    }
    AdoptionFile.model_validate(base)
    with pytest.raises(ValueError):
        AdoptionFile.model_validate({**base, "parent": "y.zip"})
    with pytest.raises(ValueError):
        AdoptionFile.model_validate({**base, "depth": 2})
    assert MAX_INT > 0


# ----- F10, F11, F13: exclusions, limits, roots ----------------------------------------------


def test_excluded_folders_are_summarised_not_listed(tmp_path: Path) -> None:
    (tmp_path / "private" / "inner").mkdir(parents=True)
    (tmp_path / "private" / "diary.md").write_text("mine\n", "utf-8")
    (tmp_path / "private" / "inner" / "x.md").write_text("mine too\n", "utf-8")
    rows = rows_of(tmp_path, ScanConfig(exclusions=["private/**"]))
    assert not [p for p in rows if p.startswith("private/")]
    folder = rows["private"]
    assert folder.entry == "folder" and folder.status == "excluded"
    assert folder.detail == {"files": 2} and folder.sha256 is None


def test_limits_cannot_ask_for_unbounded_memory() -> None:
    with pytest.raises(ValueError):
        ArchiveLimits(max_member_bytes=2**40)
    with pytest.raises(ValueError):
        ArchiveLimits(max_total_uncompressed=2**50)


def test_a_root_that_holds_the_store_is_refused(tmp_path: Path) -> None:
    from test_cp17_inventory import _add_root

    from prometheos.domain.models import PrometheosError

    env = Env(tmp_path)
    _add_root(env, "everything", tmp_path)  # contains the home and the store
    with pytest.raises(PrometheosError) as err:
        env.svc.adoption_scan(env.project_id, AGENT, root_alias="everything", mode="wait")
    assert err.value.code == "policy_denied"


# ----- F9: transports, cancellation and paths in replies ------------------------------------


def test_a_cancelled_scan_leaves_a_failed_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app import runs

    env = Env(tmp_path)

    def cancelled(self: inv.Inventory) -> inv.Result:
        raise runs.Cancelled("simulated cancellation at the inventory stage")

    monkeypatch.setattr(inv.Inventory, "run", cancelled)
    try:
        view = env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
        assert view.get("state") in (None, "cancelled")
    except Exception as exc:  # the synchronous path may report the stop as an error
        assert "cancel" in str(exc).lower() or getattr(exc, "code", "") in ("busy", "io_error")
    scan = env.svc.adoption_scans(env.project_id, AGENT)["scans"][0]
    assert scan["state"] == "failed" and scan["finished_at"] is not None


def test_the_scan_through_the_cli_and_http_names_no_local_path(tmp_path: Path) -> None:
    import json as _json

    from hub_support import running_hub
    from test_cp1_cli import Cli

    env = Env(tmp_path)
    cli = Cli(tmp_path)
    code, done, err = cli("adopt", "scan", "--root", "research", "--project", env.project_id)
    assert code == 0, err
    assert done["state"] == "complete" and done["scan_gen"] == 1
    code, files, _ = cli("adopt", "files", "1", "--project", env.project_id, "--limit", "3")
    assert code == 0 and files["total"] >= 6 and len(files["files"]) == 3
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        started = hub.request(
            "POST", "/api/v1/adoption/scans", {"root_alias": "research", "wait_seconds": 10}
        )
        assert started.status == 200, started.body
        listed = hub.request("GET", "/api/v1/adoption/scans")
        evidence = hub.request("GET", "/api/v1/adoption/scans/1/evidence?limit=50")
    bodies = [started.body, listed.body, evidence.body]
    texts = [_json.dumps(done), _json.dumps(files), *(b.decode("utf-8") for b in bodies)]
    assert _json.loads(listed.body)["scans"][0]["state"] == "complete"
    for text in texts:
        assert str(env.research) not in text and str(env.home) not in text
        assert str(env.research).replace("\\", "/") not in text
