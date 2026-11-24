"""Backup and restore keep CP18's state (decision 0048; review int5c CP1-F4).

- A store with research events, detector state, an agent's approval request and operation runs
  restores with every one of those rows unchanged.
- A backup made before schema 9 (no event tables): the owner's ``restore`` restores it and then
  upgrades the staged copy before registering it (decision 0052, which replaces the refusal
  pinned by decision 0048 section 6); a failed upgrade changes nothing. The recovery path
  (``maintenance restore``) still restores it unregistered at its old schema.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, FULL, OWNER, OWNER_ACT, Env, create_entry
from cp7_support import create
from test_cp18_events import iso
from test_cp18_work_items import add_work_item, work_body

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError
from prometheos.store.backup import semantic_snapshot
from prometheos.store.schema import MIGRATIONS

TABLES = ("events", "detector_state", "approval_requests", "operation_runs")


def rows(store_dir: Path, table: str) -> list[tuple[Any, ...]]:
    path = (store_dir / "record.sqlite").as_posix()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608


def seeded(env: Env) -> None:
    add_work_item(env, due_at=iso(datetime.now(UTC) + timedelta(days=3)))
    env.apply([create("action", work_body(due_at=None), new_id("action"))], caller=AGENT)
    env.import_files(["baseline.md"])  # an operation run
    assert env.svc.events_detect_at_start(env.project_id) is not None  # detector state
    (event, *_) = env.svc.event_list(env.project_id, AGENT)["items"]
    with pytest.raises(PrometheosError) as asked:  # an agent's pending approval request
        env.svc.approval_request(
            env.project_id, AGENT, "event_acknowledge", {"event_ids": [event["event_id"]]}
        )
    assert asked.value.code == "approval_required"


def test_events_detector_state_approvals_and_runs_survive_backup_and_restore(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    seeded(env)
    before = {table: rows(env.store_dir, table) for table in TABLES}
    assert all(before[table] for table in TABLES), {t: len(r) for t, r in before.items()}
    backup = Path(env.svc.backup(env.project_id, None, caller=FULL)["backup"])
    target = tmp_path / "restored"
    result = env.svc.restore(backup, target, caller=FULL)
    assert result["store"] == str(target)
    after = {table: rows(target, table) for table in TABLES}
    assert after == before
    # And the restored store answers: the queue, the pending request.
    queue = env.svc.work_queue(env.project_id, AGENT)
    unacknowledged = [r for r in env.svc.event_list(env.project_id, OWNER)["items"]]
    assert queue["counts"]["events"] == len(unacknowledged) > 0
    pending = env.svc.approval_list(env.project_id, OWNER, state="pending")["approvals"]
    assert [p["operation"] for p in pending] == ["event_acknowledge"]


def older_backup(env_dir: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Env, Path]:
    """A project whose backup was made under schema 8 (before CP18's event tables); the install
    is then upgraded, so its live store migrates to the running schema."""
    from prometheos.store import project

    eight = [m for m in MIGRATIONS if m.version <= 8]
    with monkeypatch.context() as patch:
        patch.setattr(project, "MIGRATIONS", eight)
        env = Env(env_dir)
        env.apply([create_entry(title="Written under schema eight", text="okapi note")])
        backup = Path(env.svc.backup(env.project_id, None, caller=FULL)["backup"])
    assert env.svc.migrate(env.project_id, authority=OWNER_ACT)["to_schema"] == len(MIGRATIONS)
    return env, backup


def test_an_older_backup_is_restored_then_upgraded_and_keeps_its_pre_migration_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 0052 (owner answer, decision 0049 section 1.4): the owner's restore upgrades
    the staged copy of an older backup along the migration chain before it registers it. The
    archive is kept in the new store's exports/ as its verified pre-migration backup and listed
    in the inventory; the restored records equal the live store's after its own migration."""
    env, backup = older_backup(tmp_path, monkeypatch)
    live = semantic_snapshot(env.store_dir)
    backup_sha = hashlib.sha256(backup.read_bytes()).hexdigest()
    target = tmp_path / "restored"
    result = env.svc.restore(backup, target, caller=FULL)
    migrated = result["migrated"]
    assert migrated["from_schema"] == 8 and migrated["to_schema"] == len(MIGRATIONS)
    assert migrated["applied"] == list(range(9, len(MIGRATIONS) + 1))
    assert result["store"] == str(target) and result["quarantine"] is not None
    with closing(
        sqlite3.connect(f"file:{(target / 'record.sqlite').as_posix()}?mode=ro", uri=True)
    ) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        inventory = conn.execute(
            "SELECT backup_sha256, location, purpose, schema_version FROM backup_inventory "
            "WHERE purpose = 'pre_migration'"
        ).fetchall()
    kept = Path(migrated["pre_migration_backup"]["path"])
    assert kept.parent == target / "exports" and kept.name.startswith("pre-migrate-v8-to-v")
    assert hashlib.sha256(kept.read_bytes()).hexdigest() == backup_sha
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_sha  # source untouched
    assert inventory == [(backup_sha, str(kept), "pre_migration", 8)]
    restored = semantic_snapshot(target)
    for part in ("project", "records", "revisions", "relations", "payloads", "media"):
        assert restored[part] == live[part], part
    # The restored, upgraded store is the registered one and answers at the running schema.
    hits = env.svc.search(env.project_id, AGENT, "okapi")["hits"]
    assert [h["title"] for h in hits] == ["Written under schema eight"]
    assert env.svc.work_queue(env.project_id, AGENT)["counts"]["events"] >= 0


def test_a_backup_at_the_running_schema_is_restored_without_an_upgrade(tmp_path: Path) -> None:
    env = Env(tmp_path)
    backup = Path(env.svc.backup(env.project_id, None, caller=FULL)["backup"])
    result = env.svc.restore(backup, tmp_path / "restored", caller=FULL)
    assert result["migrated"] is None
    assert not list((tmp_path / "restored" / "exports").glob("pre-migrate-*"))


@pytest.mark.parametrize("where", ["a migration in the middle of the chain", "after the upgrade"])
def test_a_failed_upgrade_leaves_no_target_no_registry_change_and_the_live_store_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    from prometheos.app import service
    from prometheos.faults import inject

    env, backup = older_backup(tmp_path, monkeypatch)
    registered = dict(env.svc.registry.entry(env.project_id))
    backup_sha = hashlib.sha256(backup.read_bytes()).hexdigest()
    target = tmp_path / "restored"

    def boom(*_: object) -> None:
        raise sqlite3.OperationalError("injected failure")

    if where.startswith("a migration"):
        middle = (9 + len(MIGRATIONS)) // 2
        chain = [replace(m, apply=boom) if m.version == middle else m for m in MIGRATIONS]
        monkeypatch.setattr(service, "MIGRATIONS", chain)
        with pytest.raises(PrometheosError) as failed:
            env.svc.restore(backup, target, caller=FULL)
    else:
        with inject("restore_after_upgrade", boom), pytest.raises(PrometheosError) as failed:
            env.svc.restore(backup, target, caller=FULL)
    assert "could not be upgraded from schema 8" in failed.value.message
    assert "nothing was registered" in failed.value.message
    assert not target.exists() and not list(tmp_path.glob(".restored.restore-*"))
    assert dict(env.svc.registry.entry(env.project_id)) == registered
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_sha
    hits = env.svc.search(env.project_id, AGENT, "okapi")["hits"]
    assert [h["title"] for h in hits] == ["Written under schema eight"]


def test_the_owners_restore_prompt_names_the_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos import cli
    from prometheos.store.backup import read_manifest

    env, backup = older_backup(tmp_path, monkeypatch)
    summary, _ = cli._restore_summary(env.svc, backup, tmp_path / "r", read_manifest(backup))
    assert f"at schema 8; the restored copy is upgraded to schema {len(MIGRATIONS)}" in summary
    current = Path(env.svc.backup(env.project_id, None, caller=FULL)["backup"])
    summary, _ = cli._restore_summary(env.svc, current, tmp_path / "r", read_manifest(current))
    assert "upgraded" not in summary


def test_the_recovery_path_restores_an_older_backup_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Today's recovery path (review S10F-F1): ``maintenance restore`` verifies and restores an
    older backup into a new folder under the home at its old schema, and changes nothing else.
    What a switch to it and its migration do is not pinned here (decision 0048 section 6)."""
    from prometheos.store import project

    eight = [m for m in MIGRATIONS if m.version <= 8]
    with monkeypatch.context() as patch:
        patch.setattr(project, "MIGRATIONS", eight)
        env = Env(tmp_path)
        backup = Path(env.svc.backup(env.project_id, None, caller=FULL)["backup"])
    assert env.svc.migrate(env.project_id, authority=OWNER_ACT)["to_schema"] == len(MIGRATIONS)
    registered = dict(env.svc.registry.entry(env.project_id))
    result = env.svc.maintenance_restore(env.project_id, str(backup))
    assert result["restored"] is True and result["registry_switched"] is False
    target = Path(result["target_store_path"])
    path = (target / "record.sqlite").as_posix()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8  # still the old schema
    assert dict(env.svc.registry.entry(env.project_id)) == registered
    assert env.svc.project_info(env.project_id, OWNER)["project_id"] == env.project_id


def test_an_archive_swapped_after_verification_is_not_kept_as_the_pre_migration_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review s12-restore-a F1: the kept copy must be the archive that was verified. Here the
    copy step reads another (valid) archive; its manifest's database digest differs from the
    staged database, so the restore stops and changes nothing."""
    import shutil

    from prometheos.app import service

    env, backup = older_backup(tmp_path, monkeypatch)
    other = Path(env.svc.backup(env.project_id, None, caller=FULL)["backup"])
    registered = dict(env.svc.registry.entry(env.project_id))
    real_copy = shutil.copyfile
    monkeypatch.setattr(service.shutil, "copyfile", lambda src, dst: real_copy(other, dst))
    target = tmp_path / "restored"
    with pytest.raises(PrometheosError) as failed:
        env.svc.restore(backup, target, caller=FULL)
    assert "the backup changed while it was restored" in failed.value.message
    assert "nothing was registered" in failed.value.message
    assert not target.exists() and not list(tmp_path.glob(".restored.restore-*"))
    assert dict(env.svc.registry.entry(env.project_id)) == registered
