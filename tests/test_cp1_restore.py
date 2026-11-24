"""C01-RESTORE: a verified snapshot restores into a second directory with equivalent
records and safe explicit root rebinding; corrupt copies are rejected (S04)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest
from cp1_support import FULL, OWNER, Env, create_entry, entry_body

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError
from prometheos.store.backup import semantic_snapshot


@pytest.fixture
def populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    from prometheos.app import sources

    monkeypatch.setattr(sources, "INLINE_LIMIT", 4096)
    env = Env(tmp_path)
    retained = env.import_files(["baseline.md", "artifact.bin"], retention="copy")
    reference = env.import_files(["plant-result.md"], retention="reference")
    base = retained["plan"]["items"][0]
    eid = new_id("entry")
    evidence = [
        {
            "source_id": base["source_id"],
            "version_id": base["version_id"],
            "locator": {"type": "text", "start_line": 1, "end_line": 1},
            "quote_sha256": None,
        }
    ]
    created_seq = env.apply([create_entry(eid, title="Baseline note", evidence=evidence)])[
        "project_seq"
    ]
    env.apply_protected(
        caller=OWNER,
        ops=[
            {
                "op": "revise",
                "id": eid,
                "base_rev": 1,
                "sensitivity": "restricted",
                "occurred_at": "2026-09-28T10:00:00Z",
                "body": entry_body(title="Baseline note (revised)", evidence=evidence),
            }
        ],
    )
    env.apply(
        caller=OWNER,
        ops=[
            {
                "op": "relation_add",
                "source_ref": {"id": eid, "rev": 2},
                "target_ref": {"id": base["version_id"], "rev": 1},
                "relation": "cites",
            }
        ],
    )
    env.svc.source_check(env.project_id, [reference["plan"]["items"][0]["version_id"]], save=True)
    return {
        "env": env,
        "entry": eid,
        "created_seq": created_seq,
        "base": base,
        "ref": reference["plan"]["items"][0],
        "art": retained["plan"]["items"][1],
    }


def test_backup_restores_equivalent_records_in_second_directory(
    populated: dict, tmp_path: Path
) -> None:
    env: Env = populated["env"]
    assert populated["art"]["storage"] == "media"
    result = env.svc.backup(env.project_id, tmp_path / "snap.zip", FULL)
    manifest = result["manifest"]
    assert [m["sha256"] for m in manifest["media"]] == [populated["art"]["sha256"]]
    assert [r["version_id"] for r in manifest["reference_only_not_included"]] == [
        populated["ref"]["version_id"]
    ]
    assert env.svc.verify_backup(Path(result["backup"]))["verified"]

    original = semantic_snapshot(env.store_dir)
    target = tmp_path / "second" / "store"
    restored = env.svc.restore(Path(result["backup"]), target, caller=FULL)
    assert restored["previous_store"] == str(env.store_dir)
    assert semantic_snapshot(target) == original
    assert (
        target / "media" / populated["art"]["sha256"][:2] / populated["art"]["sha256"]
    ).is_file()
    # The registry now points at the restored store; the previous store is kept intact.
    assert env.svc.registry.lookup(env.project_id) == target
    assert env.store_dir.is_dir() and semantic_snapshot(env.store_dir) == original
    entry = env.svc.get(env.project_id, OWNER, populated["entry"], None)
    assert entry["body"]["title"] == "Baseline note (revised)" and entry["rev"] == 2
    historical = env.svc.get(env.project_id, OWNER, populated["entry"], populated["created_seq"])
    assert historical["body"]["title"] == "Baseline note"
    # Reference-only bytes were never in the backup and remain honestly external/missing.
    ref = env.svc.get(env.project_id, OWNER, populated["ref"]["version_id"], None)
    assert ref["body"]["availability"] in ("external", "missing")
    assert ref["body"]["storage_key"] is None


def test_restore_reports_missing_roots_and_rebinds_only_explicitly(
    populated: dict, tmp_path: Path
) -> None:
    env: Env = populated["env"]
    backup = Path(env.svc.backup(env.project_id, tmp_path / "snap.zip", FULL)["backup"])
    moved = tmp_path / "moved-research"
    shutil.move(str(env.research), str(moved))

    first = env.svc.restore(backup, tmp_path / "r1", caller=FULL)
    assert first["roots"] == [{"alias": "research", "path": str(env.research), "available": False}]
    assert first["rebind_commit"] is None
    obs = env.svc.source_check(env.project_id, [populated["base"]["version_id"]], save=False)
    assert obs["observations"][0]["retained_digest_ok"] is True
    assert obs["observations"][0]["origin_reachable"] is False  # never auto-bound

    second = env.svc.restore(backup, tmp_path / "r2", rebind={"research": str(moved)}, caller=FULL)
    assert second["roots"][0] == {
        "alias": "research",
        "path": str(moved.resolve()),
        "available": True,
    }
    assert second["rebind_commit"]["project_seq"] == second["project_seq"] + 1
    obs = env.svc.source_check(env.project_id, [populated["base"]["version_id"]], save=False)
    assert obs["observations"][0]["origin_reachable"] is True
    assert obs["observations"][0]["origin_matches_version"] is True
    with pytest.raises(PrometheosError) as err:
        env.svc.restore(backup, tmp_path / "r3", rebind={"unknown": str(moved)}, caller=FULL)
    assert err.value.code == "validation_error"


def _rewrite(src: Path, dst: Path, mutate: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info.filename, mutate.get(info.filename, zin.read(info.filename)))
    return dst


def test_corrupt_backups_rejected_without_changing_registry(
    populated: dict, tmp_path: Path
) -> None:
    env: Env = populated["env"]
    good = Path(env.svc.backup(env.project_id, tmp_path / "snap.zip", FULL)["backup"])
    with zipfile.ZipFile(good) as z:
        db_bytes = bytearray(z.read("record.sqlite"))
        manifest = json.loads(z.read("manifest.json"))
        media_name = manifest["media"][0]["path"]
    flipped = bytearray(db_bytes)
    flipped[len(flipped) // 2] ^= 0xFF
    tampered_manifest = dict(manifest, project_seq=manifest["project_seq"] + 1)
    cases = {
        "db-bitflip": {"record.sqlite": bytes(flipped)},
        "media-altered": {media_name: b"not the retained bytes"},
        "manifest-seq": {"manifest.json": json.dumps(tampered_manifest).encode()},
        "manifest-dupkey": {"manifest.json": b'{"format":"x","format":"prometheos-backup-v1"}'},
    }
    for label, mutate in cases.items():
        bad = _rewrite(good, tmp_path / f"{label}.zip", mutate)
        with pytest.raises(PrometheosError):
            env.svc.verify_backup(bad)
        target = tmp_path / f"restore-{label}"
        with pytest.raises(PrometheosError):
            env.svc.restore(bad, target, caller=FULL)
        assert not target.exists(), label
        assert env.svc.registry.lookup(env.project_id) == env.store_dir, label
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    with pytest.raises(PrometheosError):
        env.svc.verify_backup(truncated)
    assert not list(tmp_path.glob(".restore-*.restore-*"))


def test_restore_never_overwrites_existing_directory(populated: dict, tmp_path: Path) -> None:
    env: Env = populated["env"]
    good = Path(env.svc.backup(env.project_id, tmp_path / "snap.zip", FULL)["backup"])
    with pytest.raises(PrometheosError) as err:
        env.svc.restore(good, env.store_dir, caller=FULL)
    assert err.value.code == "validation_error"
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(PrometheosError):
        env.svc.restore(good, occupied, caller=FULL)
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_backup_output_never_overwritten_and_restore_store_is_writable(
    populated: dict, tmp_path: Path
) -> None:
    env: Env = populated["env"]
    good = Path(env.svc.backup(env.project_id, tmp_path / "snap.zip", FULL)["backup"])
    with pytest.raises(PrometheosError):
        env.svc.backup(env.project_id, good, FULL)
    target = tmp_path / "restored"
    env.svc.restore(good, target, caller=FULL)
    seq = env.seq()
    env.apply([create_entry(title="Written after restore")])
    assert env.seq() == seq + 1
    conn = sqlite3.connect(target / "record.sqlite")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
