"""CP10.1 forward migrations (A02 items (a), (b), (c), (e); decision 0015).

Project store schema 6 -> 7 (operation runs, approval requests, backup inventory) after a
verified backup, and registry schema 0/1 -> 2 (maintenance approvals, quarantine records)
after a verified copy. Every operational table matches its exported row contract.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env, create_entry

from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.ids import new_id, new_operational_id
from prometheos.domain.models import PrometheosError
from prometheos.store import operational
from prometheos.store.backup import semantic_snapshot
from prometheos.store.home import REGISTRY_MIGRATIONS, Registry
from prometheos.store.schema import MIGRATIONS

PREVIOUS = 6  # the last schema before CP10 (CP6 audio and provider tables)


def user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _schema6_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    from prometheos.store import project

    with monkeypatch.context() as patch:
        patch.setattr(project, "MIGRATIONS", MIGRATIONS[:PREVIOUS])
        env = Env(tmp_path)
        # The low-level import (schema 6 had no operation runs; the application now records
        # one per import, decision 0017).
        from prometheos.app import sources

        store = env.svc.open(env.project_id)
        plan = sources.create_plan(
            store, root_alias="research", paths=["baseline.md"], retention="copy",
            sensitivity="normal", max_rank=0,
        )  # fmt: skip
        sources.commit_plan(store, plan["plan_id"], "test", max_rank=0)
        env.apply([create_entry(title="Written under schema six", text="zebra note")])
    return env


def test_the_cp10_migration_is_version_seven() -> None:
    assert MIGRATIONS[PREVIOUS].version == 7 and "operation runs" in MIGRATIONS[PREVIOUS].name
    # CP10.3 (decision 0017) adds version 8: run stages, transcription runs folded in. Later
    # checkpoints append (CP18.2, decision 0035: version 9, the event outbox).
    assert MIGRATIONS[PREVIOUS + 1].version == 8 and len(MIGRATIONS) >= 8


def test_schema_six_store_migrates_after_a_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _schema6_env(tmp_path, monkeypatch)
    db_path = env.store_dir / "record.sqlite"
    assert user_version(db_path) == PREVIOUS
    before = semantic_snapshot(env.store_dir)
    with pytest.raises(PrometheosError) as refused:
        env.svc.project_info(env.project_id, OWNER)
    assert refused.value.code == "unsupported" and "migrat" in refused.value.message

    result = env.svc.migrate(env.project_id, authority=OWNER_ACT)
    assert result["from_schema"] == PREVIOUS and result["to_schema"] == len(MIGRATIONS)
    assert result["applied"] == list(range(7, len(MIGRATIONS) + 1))  # 7, 8 and later
    backup = Path(result["backup"]["path"])
    assert result["backup"]["verified"] and backup.parent == env.store_dir / "exports"
    with zipfile.ZipFile(backup) as archive:
        (tmp_path / "old.sqlite").write_bytes(archive.read("record.sqlite"))
    assert user_version(tmp_path / "old.sqlite") == PREVIOUS  # the backup is the old schema

    # Research content is untouched; the new tables exist and are empty but for the backup.
    assert semantic_snapshot(env.store_dir) == before
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for table in operational.TABLES:
            assert columns(conn, table), table
        assert conn.execute("SELECT COUNT(*) FROM operation_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
        entries = operational.list_backups(conn)
    finally:
        conn.close()
    # The pre-migration backup is listed in the metadata-only inventory.
    assert [(e.purpose, e.backup_sha256, e.schema_version) for e in entries] == [
        ("pre_migration", result["backup"]["sha256"], PREVIOUS)
    ]
    assert entries[0].label == backup.name and entries[0].verification == "verified"
    hits = env.svc.search(env.project_id, AGENT, "zebra")["hits"]
    assert [h["title"] for h in hits] == ["Written under schema six"]
    assert env.svc.migrate(env.project_id, authority=OWNER_ACT)["applied"] == []


def test_failed_backup_leaves_schema_six(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prometheos.store import backup as backup_mod

    env = _schema6_env(tmp_path, monkeypatch)

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise PrometheosError("io_error", "simulated backup failure", {})

    monkeypatch.setattr(backup_mod, "create_backup", broken)
    with pytest.raises(PrometheosError):
        env.svc.migrate(env.project_id, authority=OWNER_ACT)
    assert user_version(env.store_dir / "record.sqlite") == PREVIOUS
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert "operation_runs" not in names


def test_new_store_tables_match_their_row_contracts(tmp_path: Path) -> None:
    env = Env(tmp_path)
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        for table, model in operational.TABLES.items():
            assert columns(conn, table) == list(model.model_fields), table
    finally:
        conn.close()
    registry = sqlite3.connect(env.home / "registry.sqlite")
    try:
        assert registry.execute("PRAGMA user_version").fetchone()[0] == len(REGISTRY_MIGRATIONS)
        for table, model in operational.REGISTRY_TABLES.items():
            assert columns(registry, table) == list(model.model_fields), table
    finally:
        registry.close()


def _example(name: str, file: str) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1] / "contracts" / "examples"
    return dict(json.loads((root / name / file).read_text(encoding="utf-8")))


def test_store_accessors_round_trip_and_constraints_hold(tmp_path: Path) -> None:
    env = Env(tmp_path)
    conn = sqlite3.connect(env.store_dir / "record.sqlite", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        run = operational.OperationRun.model_validate(
            _example("operation_run", "valid-running.json")
        )
        operational.insert_operation_run(conn, run)
        assert operational.get_operation_run(conn, run.id) == run
        assert operational.list_operation_runs(conn, ("running",)) == [run]
        assert operational.list_operation_runs(conn, ("completed",)) == []
        for i in range(operational.MAX_RUN_LOG_ENTRIES + 5):
            operational.append_run_log(conn, run.id, "2026-10-06T10:00:00Z", "info", f"line {i}")
        logs = operational.run_logs(conn, run.id)
        assert len(logs) == operational.MAX_RUN_LOG_ENTRIES and logs[-1].message.endswith("504")

        approval = operational.ApprovalRequest.model_validate(
            _example("approval_request", "valid-pending.json")
        )
        operational.insert_approval_request(conn, approval)
        assert operational.get_approval_request(conn, approval.id) == approval
        assert operational.list_approval_requests(conn, "pending") == [approval]

        # The database refuses rows the contracts forbid, even written around them.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE operation_runs SET state = 'completed' WHERE id = ?", (run.id,)
            )  # completed without finished_at
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE operation_runs SET state = 'done' WHERE id = ?", (run.id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE approval_requests SET state = 'granted' WHERE id = ?", (approval.id,)
            )
        record_id = new_id("entry")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operation_runs (id, operation, state, caller_rank, actor, "
                "input_digests, input_digest, config_digest, created_at, updated_at) "
                "VALUES (?, 'x', 'queued', 0, 'a', '[]', 'd', 'c', 't', 't')",
                (record_id,),
            )  # a record ID is not a run ID

        manifest = {
            "created_at": "2026-10-06T10:00:00Z",
            "software_version": "0.1.0",
            "project_seq": 3,
            "schema_version": 7,
            "database": {"path": "record.sqlite", "sha256": "8" * 64, "bytes": 10},
            "media": [{"path": "media/aa/" + "a" * 64, "sha256": "a" * 64, "bytes": 5}],
            "reference_only_not_included": [],
        }
        entry, media = operational.entry_from_manifest(
            manifest,
            backup_sha256="6" * 64,
            location=str(tmp_path / "b.zip"),
            label="b.zip",
            purpose="manual",
            verified_at="2026-10-06T10:00:05Z",
            recorded_at="2026-10-06T10:00:06Z",
        )
        first = operational.record_backup(conn, entry, media)
        assert operational.record_backup(conn, entry, media) == first  # not duplicated
        listed = operational.list_backups(conn)
        assert len(listed) == 1 and listed[0].manifest_sha256 == canonical_sha256(manifest)
        assert operational.backups_covering_media(conn, "a" * 64) == [first]
        assert operational.backups_covering_media(conn, "b" * 64) == []
    finally:
        conn.close()


# ----- registry ------------------------------------------------------------------------


def _legacy_registry(home: Path) -> Path:
    """A registry exactly as CP1-CP9 code wrote it: user_version 0, one projects table."""
    home.mkdir(parents=True)
    path = home / "registry.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS projects (project_id TEXT PRIMARY KEY, store_path TEXT "
        "NOT NULL, title TEXT NOT NULL, registered_at TEXT NOT NULL, previous_store_path TEXT, "
        "location_note TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO projects VALUES (?,?,?,?,?,?)",
        (f"project_{'0' * 8}-0000-4000-8000-000000000001", "C:/stores/a", "A", "t", None, "n"),
    )
    conn.commit()
    conn.close()
    return path


def test_legacy_registry_migrates_after_a_verified_copy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = _legacy_registry(home)
    registry = Registry(home)
    entries = registry.entries()
    assert [e["title"] for e in entries] == ["A"]
    assert user_version(path) == 2
    copies = sorted((home / "backups").glob("registry-v0-to-v2-*.sqlite"))
    assert len(copies) == 1
    old = sqlite3.connect(copies[0])
    try:
        assert old.execute("SELECT title FROM projects").fetchall() == [("A",)]
        assert old.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        old.close()
    # Idempotent: a second connection neither migrates nor copies again.
    registry.entries()
    assert len(list((home / "backups").glob("*.sqlite"))) == 1


def test_fresh_registry_needs_no_backup_and_newer_registry_is_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert not (env.home / "backups").exists()
    conn = sqlite3.connect(env.home / "registry.sqlite")
    conn.execute("PRAGMA user_version = 3")
    conn.close()
    with pytest.raises(PrometheosError) as refused:
        Registry(env.home).entries()
    assert refused.value.code == "unsupported"


def test_failed_registry_copy_leaves_the_registry_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    path = _legacy_registry(home)

    def broken(self: Registry, conn: sqlite3.Connection, old: int, new: int) -> Path:
        raise PrometheosError("io_error", "simulated copy failure", {})

    monkeypatch.setattr(Registry, "_verified_copy", broken)
    with pytest.raises(PrometheosError):
        Registry(home).entries()
    assert user_version(path) == 0
    conn = sqlite3.connect(path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert "maintenance_approvals" not in names


def test_registry_accessors_round_trip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _legacy_registry(home)
    registry = Registry(home)
    approval = operational.MaintenanceApproval.model_validate(
        _example("maintenance_approval", "valid-pending.json")
    )
    registry.insert_maintenance_approval(approval)
    assert registry.maintenance_approval(approval.id) == approval
    assert registry.maintenance_approvals(approval.project_id) == [approval]
    record = operational.StoreQuarantine.model_validate(
        _example("store_quarantine", "valid-missing-database.json")
    )
    registry.insert_quarantine(record)
    assert registry.quarantine_records(record.project_id) == [record]
    assert registry.quarantine_records(new_id("project")) == []
    # A switch the owner performed directly names no maintenance approval.
    orphan = record.model_copy(
        update={"id": new_operational_id("quarantine"), "maintenance_approval_id": None}
    )
    registry.insert_quarantine(orphan)
    assert len(registry.quarantine_records(record.project_id)) == 2


# ----- round 2 (decision 0015): lifecycle, resume fields, retries, foreign keys ----------


def _store_conn(env: Env) -> sqlite3.Connection:
    conn = sqlite3.connect(env.store_dir / "record.sqlite", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def _capability(conn: sqlite3.Connection, project_id: str) -> str:
    from prometheos.store import capabilities

    return capabilities.issue(
        conn,
        project_id=project_id,
        operation="exact_request",
        digest="f" * 64,
        ttl_seconds=600,
        max_uses=1,
        label="SYNTHETIC-TEST grant",
        synthetic=True,
    ).capability_id


def test_a_granted_request_that_ends_keeps_its_decision(tmp_path: Path) -> None:
    """R2: expiry, withdrawal or revocation after a grant keep who decided, when, and the
    grant; the state says why it ended and closed_at when. Erasing them is refused."""
    env = Env(tmp_path)
    conn = _store_conn(env)
    try:
        pending = operational.ApprovalRequest.model_validate(
            _example("approval_request", "valid-pending.json")
        ).model_copy(update={"project_id": env.project_id})
        operational.insert_approval_request(conn, pending)
        cap = _capability(conn, env.project_id)
        conn.execute(
            "UPDATE approval_requests SET state = 'granted', "
            "decided_at = '2026-10-06T10:10:00Z', decided_by = 'hub-owner', capability_id = ? "
            "WHERE id = ?",
            (cap, pending.id),
        )
        for state in ("expired", "withdrawn", "revoked"):
            conn.execute("SAVEPOINT s")
            conn.execute(
                "UPDATE approval_requests SET state = ?, closed_at = '2026-10-06T11:00:00Z' "
                "WHERE id = ?",
                (state, pending.id),
            )
            ended = operational.get_approval_request(conn, pending.id)
            assert ended is not None and ended.state == state
            assert ended.decided_by == "hub-owner" and ended.capability_id == cap
            conn.execute("ROLLBACK TO s")
            conn.execute("RELEASE s")
        with pytest.raises(sqlite3.IntegrityError):  # the grant cannot be erased
            conn.execute(
                "UPDATE approval_requests SET state = 'expired', closed_at = 't', "
                "capability_id = NULL WHERE id = ?",
                (pending.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):  # an ended request records when
            conn.execute(
                "UPDATE approval_requests SET state = 'revoked' WHERE id = ?", (pending.id,)
            )
        with pytest.raises(sqlite3.IntegrityError):  # a grant names a real capability row
            conn.execute(
                "UPDATE approval_requests SET capability_id = ? WHERE id = ?", (_uuid(), pending.id)
            )
    finally:
        conn.close()


def test_runs_store_what_resume_needs(tmp_path: Path) -> None:
    """R3: parameters bound by a digest, what a waiting run awaits, the uncertain call."""
    env = Env(tmp_path)
    conn = _store_conn(env)
    try:
        for name in ("valid-waiting-for-approval.json", "valid-unknown-external.json"):
            run = operational.OperationRun.model_validate(_example("operation_run", name))
            run = run.model_copy(
                update={"id": new_operational_id("oprun"), "start_request_id": _uuid()}
            )
            operational.insert_operation_run(conn, run)
            stored = operational.get_operation_run(conn, run.id)
            assert stored == run
            assert stored is not None and stored.params_digest == canonical_sha256(stored.params)
        waiting = operational.list_operation_runs(conn, ("waiting_for_input",))[0]
        assert waiting.waiting_on is not None and waiting.waiting_on.kind == "approval"
        unknown = operational.list_operation_runs(conn, ("unknown_external",))[0]
        assert unknown.external_call is not None
        assert unknown.external_call.external_ref == "req_abc123"
        with pytest.raises(sqlite3.IntegrityError):  # an uncertain effect names its call
            conn.execute(
                "UPDATE operation_runs SET external_call = NULL WHERE id = ?", (unknown.id,)
            )
        with pytest.raises(sqlite3.IntegrityError):  # a waiting run says what it awaits
            conn.execute("UPDATE operation_runs SET waiting_on = NULL WHERE id = ?", (waiting.id,))
    finally:
        conn.close()
    with pytest.raises(ValueError, match="credential"):
        operational.ExternalCall(
            provider="p",
            external_ref="https://api.example/x?token=abc",
            request_sha256=None,
            started_at="2026-10-06T10:00:00Z",
        )


def test_approval_request_ids_are_one_namespace_across_ranks(tmp_path: Path) -> None:
    """R10, decision 0010: an identical retry returns the stored request; the same request
    ID from another rank, or for another plan, conflicts."""
    env = Env(tmp_path)
    conn = _store_conn(env)
    try:
        first = operational.ApprovalRequest.model_validate(
            _example("approval_request", "valid-pending.json")
        )
        assert operational.insert_approval_request(conn, first) == first
        retry = first.model_copy(update={"id": new_operational_id("apreq")})
        assert operational.insert_approval_request(conn, retry) == first  # replay, no new row
        assert len(operational.list_approval_requests(conn)) == 1
        owner = retry.model_copy(update={"caller_rank": 2})
        with pytest.raises(PrometheosError) as conflict:
            operational.insert_approval_request(conn, owner)
        assert conflict.value.code == "request_key_conflict"
        assert first.id not in json.dumps(conflict.value.details)
        with pytest.raises(sqlite3.IntegrityError):  # the database enforces one namespace
            operational.insert(conn, "approval_requests", owner)
    finally:
        conn.close()


def test_maintenance_approvals_retry_safely_and_keep_decisions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _legacy_registry(home)
    registry = Registry(home)
    approval = operational.MaintenanceApproval.model_validate(
        _example("maintenance_approval", "valid-pending.json")
    )
    assert registry.insert_maintenance_approval(approval) == approval
    retry = approval.model_copy(update={"id": new_operational_id("mapreq")})
    assert registry.insert_maintenance_approval(retry) == approval
    assert len(registry.maintenance_approvals(approval.project_id)) == 1
    with pytest.raises(PrometheosError) as conflict:
        registry.insert_maintenance_approval(retry.model_copy(update={"caller_rank": 2}))
    assert conflict.value.code == "request_key_conflict"
    ended = operational.MaintenanceApproval.model_validate(
        _example("maintenance_approval", "valid-expired-after-grant.json")
    ).model_copy(update={"id": new_operational_id("mapreq"), "request_id": _uuid()})
    registry.insert_maintenance_approval(ended)
    stored = registry.maintenance_approval(ended.id)
    assert stored is not None and stored.decided_by == "hub-owner-recovery"
    assert stored.grant_expires_at is not None and stored.state == "expired"


def test_registry_enforces_foreign_keys(tmp_path: Path) -> None:
    """R9: a quarantine record cannot name an approval the registry does not hold."""
    home = tmp_path / "home"
    _legacy_registry(home)
    registry = Registry(home)
    record = operational.StoreQuarantine.model_validate(
        _example("store_quarantine", "valid-missing-database.json")
    )
    with pytest.raises(PrometheosError) as refused:
        registry.insert_quarantine(record)  # its approval was never inserted
    assert refused.value.code == "validation_error"
    assert registry.quarantine_records(record.project_id) == []
    conn = registry._connect(create=False)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_lifecycle_models_refuse_an_erased_decision() -> None:
    """R2 at the contract level, for both approval tables."""
    from pydantic import ValidationError

    granted = dict(
        _example("approval_request", "valid-pending.json"),
        state="granted",
        decided_at="2026-10-06T10:10:00Z",
        decided_by="hub-owner",
        capability_id=_uuid(),
    )
    operational.ApprovalRequest.model_validate(granted)
    for state in ("expired", "withdrawn", "revoked"):
        ended = dict(granted, state=state, closed_at="2026-10-06T11:00:00Z")
        operational.ApprovalRequest.model_validate(ended)
        with pytest.raises(ValidationError):
            operational.ApprovalRequest.model_validate(dict(ended, capability_id=None))
        with pytest.raises(ValidationError):
            operational.ApprovalRequest.model_validate(dict(ended, closed_at=None))
    maint = _example("maintenance_approval", "valid-expired-after-grant.json")
    operational.MaintenanceApproval.model_validate(maint)
    with pytest.raises(ValidationError):
        operational.MaintenanceApproval.model_validate(dict(maint, grant_expires_at=None))


def test_database_decision_checks_match_the_model(tmp_path: Path) -> None:
    """Round 3 (N3): a pending request has no decision; denied, granted, used and revoked
    requests have one, in the database as in the contract."""
    env = Env(tmp_path)
    conn = _store_conn(env)
    try:
        pending = operational.ApprovalRequest.model_validate(
            _example("approval_request", "valid-pending.json")
        ).model_copy(update={"project_id": env.project_id})
        operational.insert_approval_request(conn, pending)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE approval_requests SET decided_at = 't', decided_by = 'x' WHERE id = ?",
                (pending.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE approval_requests SET state = 'denied', closed_at = 't' WHERE id = ?",
                (pending.id,),
            )
        conn.execute(
            "UPDATE approval_requests SET state = 'denied', closed_at = 't', decided_at = 't', "
            "decided_by = 'hub-owner' WHERE id = ?",
            (pending.id,),
        )
    finally:
        conn.close()
    home = tmp_path / "home2"
    _legacy_registry(home)
    registry = Registry(home)
    approval = operational.MaintenanceApproval.model_validate(
        _example("maintenance_approval", "valid-pending.json")
    )
    registry.insert_maintenance_approval(approval)
    rconn = registry._connect(create=False)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            rconn.execute(
                "UPDATE maintenance_approvals SET decided_at = 't', decided_by = 'x' WHERE id = ?",
                (approval.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            rconn.execute(
                "UPDATE maintenance_approvals SET state = 'denied', closed_at = 't' WHERE id = ?",
                (approval.id,),
            )
    finally:
        rconn.close()
