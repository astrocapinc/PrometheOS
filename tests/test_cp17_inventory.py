"""C17-INVENTORY (A05, CP17.1; decision 0039 section 4): the adoption inventory on the seeded
synthetic corpus and small sample folders. Every file gets a row with stat, content digest,
normalised-text digest and container type; cloud placeholders are skipped without being
opened; long paths are read; archives are listed with size, count and nesting limits that stop
bombs; nothing is written into the adopted tree (root digest before and after, plus a full
content digest of the copy); what could not be read is reported with a reason."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import anyio
import pytest
from adoption_corpus import CORPUS_LIMITS, generate
from cp1_support import AGENT, OWNER, Env
from mcp import Client

from prometheos.adoption import inventory as inv
from prometheos.adoption import scan as scan_mod
from prometheos.adoption import tables
from prometheos.adoption.contracts import AdoptionFile, ArchiveLimits, ScanConfig, disk_path
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server
from prometheos.store.schema import MIGRATIONS

SEED = 17


# ----- helpers -------------------------------------------------------------------------------


def tree_digest(root: Path) -> str:
    """Every entry's path and kind, every file's modification time and content: a write
    anywhere under the root (bytes, names or file times) changes it. Folder times are left out
    (NTFS updates them lazily in the parent's listing; see ``inventory.root_digest``)."""
    h = hashlib.sha256()
    for walked in inv.walk(root):
        when = 0 if walked.stat.kind == "dir" else walked.stat.mtime_ns
        h.update(f"{walked.rel}\0{walked.stat.kind}\0{when}\n".encode())
        if walked.stat.kind == "file":
            with open(inv.os_path(root / walked.rel), "rb") as handle:
                h.update(hashlib.sha256(handle.read()).digest())
    return h.hexdigest()


def placeholder_provider(root: Path, placeholders: set[str]) -> inv.StatProvider:
    """Mark ``placeholders`` as recall-on-data-access files, as a cloud client would (user-mode
    code cannot set the bit, so the test sets it in the stat the inventory sees)."""
    base = os.path.abspath(root)

    def stat_of(entry: os.DirEntry[str]) -> inv.Stat:
        st = inv.entry_stat(entry)
        rel = Path(entry.path.removeprefix("\\\\?\\")).as_posix()
        rel = os.path.relpath(rel, base).replace("\\", "/")
        if rel in placeholders:
            bits = st.attributes | inv.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
            return inv.Stat(st.kind, st.size, st.mtime_ns, bits, st.reparse_tag)
        return st

    return stat_of


def placeholders_of(golden: dict[str, Any]) -> set[str]:
    return set(golden["placeholders"]) | set(golden["placeholder_folders"])


class OpenGuard:
    """Records every path the inventory opens: through its hashing reader and through any
    ``open`` call in the inventory module (archives are opened that way)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened: list[str] = []
        original = inv._read_and_hash

        def spy(path: Path, size_hint: int) -> Any:
            self.opened.append(Path(path).as_posix())
            return original(path, size_hint)

        def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            self.opened.append(str(file).replace("\\", "/"))
            return open(file, *args, **kwargs)

        monkeypatch.setattr(inv, "_read_and_hash", spy)
        monkeypatch.setattr(inv, "open", spy_open, raising=False)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    root = tmp_path_factory.mktemp("corpus") / "c17"
    return root, generate(root, SEED)


def corpus_config() -> ScanConfig:
    return ScanConfig(limits=ArchiveLimits.model_validate(CORPUS_LIMITS))


def by_path(rows: list[AdoptionFile]) -> dict[str, AdoptionFile]:
    return {r.path: r for r in rows}


# ----- the generator -------------------------------------------------------------------------


def test_the_generator_is_deterministic_and_seeded(tmp_path: Path) -> None:
    a = generate(tmp_path / "a", SEED)
    b = generate(tmp_path / "b", SEED)
    c = generate(tmp_path / "c", SEED + 1)
    assert a == b, "the same seed gives the same golden labels"
    assert tree_digest(tmp_path / "a") == tree_digest(tmp_path / "b"), "and the same bytes"
    assert a["files"] != c["files"], "another seed gives other content"
    ids = {t["id"] for t in a["traps"]}
    # every trap the criterion names (C17-INVENTORY, C17-LINEAGE, S49) is planted
    assert ids >= {
        "mtime_rewrite", "line_endings", "final_name", "contradiction", "placeholder",
        "nested_archive", "zip_bomb", "deep_archive", "objective_c", "long_path", "non_ascii",
        "autosave", "upgrade_backup", "cache", "injection",
    }  # fmt: skip
    long_rel = next(t["paths"][0] for t in a["traps"] if t["id"] == "long_path")
    assert len(str(tmp_path / "a" / long_rel)) > 300
    # the answers never sit inside the folder an analyzer reads
    assert not any("golden" in p for p in a["files"])


# ----- the inventory on the corpus -----------------------------------------------------------


@pytest.fixture
def inventoried(
    corpus: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, AdoptionFile], dict[str, Any], OpenGuard, inv.Result]:
    root, golden = corpus
    guard = OpenGuard(monkeypatch)
    before = tree_digest(root)
    digest_before = inv.root_digest(root)
    stat_of = placeholder_provider(root, placeholders_of(golden))
    result = inv.inventory(root, 1, corpus_config(), stat_of=stat_of)
    assert inv.root_digest(root) == digest_before
    assert tree_digest(root) == before, "nothing under the root changed"
    return by_path(result.rows), golden, guard, result


def test_every_file_has_a_row_with_stat_digest_and_container(inventoried: Any) -> None:
    rows, golden, _guard, result = inventoried
    on_disk = {disk_path(p) for p, r in rows.items() if r.entry == "file"}
    listed = set(golden["files"]) - set(golden["unlisted"])
    assert on_disk == listed, "one row per file on disk, nothing more or less"
    corrupt = next(t["paths"] for t in golden["traps"] if t["id"] == "corrupt_package")
    for rel in sorted(listed):
        label = golden["files"][rel]
        row = rows[inv.escaped(rel)[0]]
        assert row.size == label["size"], rel
        assert row.mtime_us == label["mtime"] * 1_000_000, (rel, "the planted time is recorded")
        if rel in golden["placeholders"] or rel in golden["noise"]:
            continue
        if rel in corrupt:
            assert row.status == "unreadable" and row.reason and row.sha256 == label["sha256"]
            continue
        assert row.status in ("read", "limited"), (rel, row.status, row.reason)
        assert row.sha256 == label["sha256"], rel
        assert row.container == label["container"], (rel, row.container)
    assert result.summary.files_on_disk == len(listed)
    fallback = [
        r.path for r in rows.values() if (r.reason or "").startswith("could not be recorded")
    ]
    assert not fallback, "the catch-all fallback must never fire on the corpus (review R2-4)"
    assert rows["Done!!/checklist.txt"].entry == "file", "the '!' in a folder name is escaped"


def test_text_rows_carry_a_normalised_digest_and_line_ending_copies_match(inventoried: Any) -> None:
    rows, golden, _guard, _ = inventoried
    group = golden["copy_groups"][0]
    a, b = (rows[p] for p in group["members"])
    assert a.sha256 != b.sha256, "the bytes differ (CRLF against LF)"
    assert a.text_sha256 == b.text_sha256 is not None, "the normalised text is the same"
    for row in rows.values():
        if row.container == "text":
            assert row.text_sha256 is not None, row.path
        else:
            assert row.text_sha256 is None, row.path


def test_cloud_placeholders_are_recorded_and_never_opened(inventoried: Any) -> None:
    rows, golden, guard, _ = inventoried
    for rel in golden["placeholders"]:
        row = rows[rel]
        assert row.status == "placeholder" and row.sha256 is None and row.container is None
        assert row.size is not None and row.attributes is not None
        assert row.attributes & inv.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        assert not any(o.endswith(rel) for o in guard.opened), f"{rel} was opened"
    for rel in golden["placeholder_folders"]:
        assert rows[rel].entry == "folder" and rows[rel].status == "placeholder"
    for rel in golden["unlisted"]:
        assert rel not in rows and not any(o.endswith(rel) for o in guard.opened)
    assert any(o.endswith("archives/bomb.zip") for o in guard.opened), "the spy sees archives"
    for rel in golden["noise"]:
        assert rows[rel].status == "noise" and not any(o.endswith(rel) for o in guard.opened)


def test_archives_are_listed_within_limits_and_bombs_stop(inventoried: Any) -> None:
    rows, _golden, _guard, result = inventoried
    bomb = rows["archives/bomb.zip"]
    assert bomb.status == "limited" and bomb.reason.startswith("max_ratio")
    assert not any(p.startswith("archives/bomb.zip!/") for p in rows), "no member of a bomb"
    many = rows["archives/many.zip"]
    assert many.status == "limited" and many.reason.startswith("max_members")
    deep = rows["archives/deep.zip!/level2.zip!/level3.zip"]
    assert deep.status == "limited" and deep.reason.startswith("max_depth") and deep.depth == 2
    inner = rows["backup/notes_backup.zip!/nested/inner.zip"]
    assert inner.status == "read" and inner.container == "zip" and inner.depth == 1
    leaf = rows["backup/notes_backup.zip!/nested/inner.zip!/inner/values.csv"]
    assert leaf.status == "listed" and leaf.depth == 2 and leaf.parent == inner.path
    assert leaf.detail is not None and "crc32" in leaf.detail
    assert result.summary.limited == 3


def test_containers_are_sniffed_from_bytes_not_names(inventoried: Any) -> None:
    rows, _golden, _guard, _ = inventoried
    assert rows["models/plant.slx"].container == "opc_slx"
    assert rows["models/plant.slx.r2024b"].container == "opc_slx", "the upgrade backup too"
    assert rows["models/plant.slxc"].container == "opc_slxc"
    assert rows["models/plant.slxc"].zone == "env_cache_noise"
    assert rows["analysis/explore_sweep.mlx"].container == "opc_mlx"
    assert rows["results/sweep_FINAL.mat"].container == "mat_v5"
    assert rows["models/plant.slx"].detail == {"parts": 8}
    assert rows["analysis/run_sweep.asv"].zone == "backup_copy"


def test_long_and_non_ascii_paths_are_read(inventoried: Any) -> None:
    rows, golden, _guard, _ = inventoried
    for trap in ("long_path", "non_ascii"):
        rel = next(t["paths"][0] for t in golden["traps"] if t["id"] == trap)
        assert rows[rel].status == "read" and rows[rel].sha256 == golden["files"][rel]["sha256"]


# ----- small sample folders: what cannot be read, links, version control, exclusions ---------


def test_unreadable_files_are_reported_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ok.txt").write_text("fine\n", "utf-8")
    (tmp_path / "locked.bin").write_bytes(b"\x00\x01")
    (tmp_path / "broken.zip").write_bytes(b"PK\x03\x04 not really a zip")
    original = inv._read_and_hash

    def refuse(path: Path, size_hint: int) -> Any:
        if Path(path).name == "locked.bin":
            raise PermissionError(13, "Permission denied")
        return original(path, size_hint)

    monkeypatch.setattr(inv, "_read_and_hash", refuse)
    rows = by_path(inv.inventory(tmp_path, 1).rows)
    assert rows["ok.txt"].status == "read"
    locked = rows["locked.bin"]
    assert locked.status == "unreadable" and locked.reason.startswith("PermissionError (errno 13)")
    broken = rows["broken.zip"]
    assert broken.status == "unreadable" and broken.reason.startswith("zip listing failed")
    assert broken.sha256 is not None, "the bytes were still digested"


def test_links_are_recorded_and_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside the root\n", "utf-8")
    root = tmp_path / "root"
    root.mkdir()
    try:
        os.symlink(outside, root / "link-dir", target_is_directory=True)
        os.symlink(outside / "secret.txt", root / "link-file")
    except OSError:
        pytest.skip("symbolic links need Developer Mode or administrator rights here")
    rows = by_path(inv.inventory(root, 1).rows)
    assert rows["link-dir"].status == "link" and rows["link-file"].status == "link"
    assert not any("secret" in p for p in rows), "nothing behind a link is listed"


def test_version_control_metadata_is_summarised_never_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = tmp_path / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "config").write_text("[remote]\n\turl = https://user:token@example.invalid/x\n", "utf-8")
    (git / "objects" / "ab").write_bytes(b"x" * 10)
    (tmp_path / "code.m").write_text("% a script\nx = 1;\n", "utf-8")
    guard = OpenGuard(monkeypatch)
    rows = by_path(inv.inventory(tmp_path, 1).rows)
    folder = rows[".git"]
    assert folder.entry == "folder" and folder.status == "noise" and folder.zone == "tool_output"
    assert (
        folder.detail == {"vcs": ".git", "files": 2}
        and folder.size == len((git / "config").read_bytes()) + 10
    )
    assert not any(p.startswith(".git/") for p in rows)
    assert not any(".git" in o for o in guard.opened)


def test_exclusions_and_noise_are_stat_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "diary.md").write_text("mine\n", "utf-8")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "index.js").write_text("//\n", "utf-8")
    (tmp_path / "keep.md").write_text("# keep\n", "utf-8")
    guard = OpenGuard(monkeypatch)
    config = ScanConfig(exclusions=["private/**"])
    rows = by_path(inv.inventory(tmp_path, 1, config).rows)
    assert rows["private"].status == "excluded" and rows["private"].entry == "folder"
    assert "private/diary.md" not in rows, "an excluded folder is summarised, not listed"
    assert rows["node_modules/x/index.js"].status == "noise"
    assert {Path(o).name for o in guard.opened} == {"keep.md"}


def test_globs_span_folders_and_ignore_case() -> None:
    globs = inv.Globs.of(["**/slprj/**", "*.asv", "data/raw/*"])
    assert globs.match("gearbox/slprj/_jitprj/a.l")
    assert globs.match("SLPRJ/x", folder=False)
    assert globs.match("gearbox/slprj", folder=True)
    assert globs.match("run.ASV") and not globs.match("old/run.asv")
    assert globs.match("data/raw/x.csv") and not globs.match("data/raw/sub/x.csv")


def test_normalised_text_digest_rules() -> None:
    lf = inv.normalised_text_digest("a = 1;  \nb = 2;\n\n")
    assert lf == inv.normalised_text_digest("a = 1;\r\nb = 2;\r\n")
    assert lf == inv.normalised_text_digest("a = 1;\rb = 2;")
    assert lf != inv.normalised_text_digest("a = 1;\nb = 3;\n")
    assert inv.decode_text(b"\xef\xbb\xbfabc") == "abc"
    assert inv.decode_text("é".encode("utf-16")) == "é"
    assert inv.decode_text(b"\x00\x01binary") is None


# ----- the scan through the service: storage, generations, read-only proof ------------------


def test_scan_is_stored_as_a_generation_and_reads_page(tmp_path: Path) -> None:
    env = Env(tmp_path)
    before = tree_digest(env.research)
    first = env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    assert first["state"] == "complete" and first["scan_gen"] == 1
    assert first["root_digest_before"] == first["root_digest_after"]
    assert tree_digest(env.research) == before, "the scan wrote nothing into the root"
    second = env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    assert second["scan_gen"] == 2 and second["config"]["previous_gen"] == 1
    scans = env.svc.adoption_scans(env.project_id, AGENT)
    assert [s["scan_gen"] for s in scans["scans"]] == [2, 1]
    page = env.svc.adoption_files(env.project_id, OWNER, 2, offset=0, limit=3)
    assert page["total"] == 6 and len(page["files"]) == 3 and page["next_offset"] == 3
    rest = env.svc.adoption_files(env.project_id, OWNER, 2, offset=3, limit=3)
    assert rest["next_offset"] is None
    names = [f["path"] for f in page["files"] + rest["files"]]
    assert names == sorted(names) and "sub/notes.txt" in names
    text = env.svc.adoption_files(env.project_id, AGENT, 2, container="text")
    assert {f["path"] for f in text["files"]} >= {"meeting.txt", "sub/notes.txt"}
    with pytest.raises(PrometheosError) as err:
        env.svc.adoption_files(env.project_id, AGENT, 99)
    assert err.value.code == "not_found"
    with pytest.raises(PrometheosError) as err:
        env.svc.adoption_files(env.project_id, AGENT, 2, status="hydrated")
    assert err.value.code == "validation_error"
    # the scan advanced no project sequence: adoption state is operational
    assert env.seq() == 1


def test_an_unknown_root_is_refused_before_any_run(tmp_path: Path) -> None:
    env = Env(tmp_path)
    with pytest.raises(PrometheosError) as err:
        env.svc.adoption_scan(env.project_id, AGENT, root_alias="elsewhere", mode="wait")
    assert err.value.code == "policy_denied"
    assert env.svc.adoption_scans(env.project_id, AGENT)["scans"] == []


def test_a_tree_that_changes_during_the_scan_fails_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    original = inv.Inventory.run

    def run_and_touch(self: inv.Inventory) -> inv.Result:
        result = original(self)
        (self.root / "appeared-meanwhile.txt").write_text("someone else wrote this\n", "utf-8")
        return result

    monkeypatch.setattr(inv.Inventory, "run", run_and_touch)
    with pytest.raises(PrometheosError) as err:
        env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    assert err.value.code == "io_error"
    assert err.value.details["reason"] == "adoption_tree_changed"
    scan = env.svc.adoption_scans(env.project_id, AGENT)["scans"][0]
    assert scan["state"] == "failed" and scan["root_digest_before"] != scan["root_digest_after"]


def test_the_corpus_scan_through_the_service(
    corpus: tuple[Path, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, golden = corpus
    env = Env(tmp_path)
    monkeypatch.setattr(
        scan_mod, "STAT_PROVIDER", placeholder_provider(root, placeholders_of(golden))
    )
    _add_root(env, "corpus", root)
    before = tree_digest(root)
    done = env.svc.adoption_scan(
        env.project_id, AGENT, root_alias="corpus", limits=CORPUS_LIMITS, mode="wait"
    )
    assert done["state"] == "complete"
    assert tree_digest(root) == before
    summary = done["summary"]
    assert summary["files_on_disk"] == len(golden["files"]) - len(golden["unlisted"])
    assert summary["by_status"]["placeholder"] == len(placeholders_of(golden))
    unreadable = env.svc.adoption_files(env.project_id, AGENT, done["scan_gen"], status="limited")
    assert {f["path"] for f in unreadable["files"]} == {
        "archives/bomb.zip", "archives/many.zip", "archives/deep.zip!/level2.zip!/level3.zip",
    }  # fmt: skip


def _add_root(env: Env, alias: str, path: Path) -> None:
    """Approve another research root the way the owner does (a protected project update)."""
    info = env.svc.project_info(env.project_id, OWNER)
    settings = dict(info["settings"])
    settings["allowed_roots"] = [*settings["allowed_roots"], {"alias": alias, "path": str(path)}]
    env.apply_protected(
        [{"op": "project_update", "base_rev": info["settings_rev"], "settings": settings}]
    )


# ----- storage contracts ---------------------------------------------------------------------


def test_row_models_are_the_table_columns(tmp_path: Path) -> None:
    env = Env(tmp_path)
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        for table, model in tables.TABLES.items():
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            assert columns == list(model.model_fields), table
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == len(MIGRATIONS) and MIGRATIONS[9].version == 10


def test_the_golden_labels_are_json_serialisable(corpus: tuple[Path, dict[str, Any]]) -> None:
    _root, golden = corpus
    json.dumps(golden)
    assert golden["generator_version"] == "1.6.0" and golden["seed"] == SEED


def test_the_agent_scans_and_reads_through_mcp(tmp_path: Path) -> None:
    """The MCP tool answers with a run at once; the agent reads the generation and its rows
    through the family tool, by root-relative paths only."""
    env = Env(tmp_path)

    async def main() -> tuple[Any, Any, Any]:
        async with Client(build_server(env.svc, env.project_id)) as client:
            started = await client.call_tool(
                "adoption_scan", {"root_alias": "research", "wait_seconds": 10}
            )
            scans = await client.call_tool("adoption_read", {"request": {"op": "scans"}})
            files = await client.call_tool(
                "adoption_read", {"request": {"op": "files", "scan_gen": 1, "status": "read"}}
            )
            return started, scans, files

    started, scans, files = anyio.run(main)
    assert not started.is_error and started.structured_content["operation"] == "adoption_scan"
    assert started.structured_content["state"] == "completed"
    scan = scans.structured_content["result"]["scans"][0]
    assert scan["state"] == "complete" and scan["root_digest_before"] == scan["root_digest_after"]
    rows = files.structured_content["result"]["files"]
    assert {r["path"] for r in rows} >= {"baseline.md", "sub/notes.txt"}
    assert all(not os.path.isabs(r["path"]) and "\\" not in r["path"] for r in rows)
    assert str(env.research) not in json.dumps(files.structured_content)
