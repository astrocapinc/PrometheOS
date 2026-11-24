"""Decision 0025: actual source sharing, lease handoff and copy-only orphan recovery."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, FULL, OWNER, OWNER_ACT, Env
from test_cp10_migrations import _schema6_env

from prometheos.app import maintenance, sources
from prometheos.domain.models import EXIT_CODES, PrometheosError
from prometheos.hub.app import create_app
from prometheos.hub.security import Gate
from prometheos.store import admission, backup, db
from prometheos.store.lock import maintenance_lock
from prometheos.store.project import ProjectStore
from prometheos.store.schema import MIGRATIONS

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows source-sharing repair")
REPO = Path(__file__).resolve().parents[1]


def busy(call: Callable[[], Any]) -> None:
    with pytest.raises(PrometheosError) as err:
        call()
    assert err.value.code == "busy"


def files(directory: Path) -> dict[str, tuple[str, int]]:
    return {
        file.name: (hashlib.sha256(file.read_bytes()).hexdigest(), file.stat().st_mtime_ns)
        for file in directory.iterdir()
        if file.is_file()
    }


def orphan(env: Env) -> None:
    database = env.store_dir / "record.sqlite"
    conn = sqlite3.connect(database)
    try:
        conn.execute("CREATE TABLE crash_probe(x)")
        conn.commit()
    finally:
        conn.close()
    script = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute('PRAGMA cache_size=1')
c.execute('BEGIN IMMEDIATE')
c.executemany('INSERT INTO crash_probe VALUES (?)', [('z'*800,)]*400)
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-I", "-c", script, str(database)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    assert (env.store_dir / "record.sqlite-journal").read_bytes()[:8] == maintenance.JOURNAL_MAGIC


@pytest.mark.parametrize("finish", ["COMMIT", "ROLLBACK"])
def test_completion_before_strong_admission_is_fresh_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    env = Env(tmp_path)
    database = env.store_dir / "record.sqlite"
    writer = sqlite3.connect(database, isolation_level=None)
    writer.execute("PRAGMA cache_size=1")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("CREATE TABLE live_probe(x)")
    writer.executemany("INSERT INTO live_probe VALUES (?)", [("x" * 800,)] * 400)
    assert (env.store_dir / "record.sqlite-journal").read_bytes()[:8] == maintenance.JOURNAL_MAGIC
    initial = admission.RawFile.__init__
    completions: list[str] = []

    def finish_before_guard(self: admission.RawFile, path: Path, *, strong: bool) -> None:
        if path == database and strong and not completions:
            writer.execute(finish)
            writer.close()
            completions.append(finish)
        initial(self, path, strong=strong)

    monkeypatch.setattr(admission.RawFile, "__init__", finish_before_guard)
    try:
        assert maintenance.journal_state(env.store_dir) == "none"
        assert completions == [finish]
        assert env.svc.open(env.project_id).directory == env.store_dir
    finally:
        if not completions:
            writer.close()


def test_live_and_idle_writable_handles_are_busy_and_never_hot(tmp_path: Path) -> None:
    env = Env(tmp_path)
    writer = sqlite3.connect(env.store_dir / "record.sqlite", isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("CREATE TABLE live_probe(x)")
        busy(lambda: maintenance.journal_state(env.store_dir))
        busy(lambda: env.svc.open(env.project_id))
        writer.execute("ROLLBACK")
        busy(lambda: maintenance.journal_state(env.store_dir))
        assert env.svc.registry.maintenance_approvals(env.project_id) == []
    finally:
        writer.close()
    assert maintenance.journal_state(env.store_dir) == "none"


def test_raw_guard_does_not_disguise_hot_to_ordinary_readers(tmp_path: Path) -> None:
    env = Env(tmp_path)
    orphan(env)
    before = files(env.store_dir)
    copy = tmp_path / "ordinary-reader-copy"
    shutil.copytree(env.store_dir, copy)
    with admission.source_snapshot(copy) as snapshot:
        assert snapshot.journal_state() == "hot"
        with admission.source_snapshot(copy) as second:
            assert second is snapshot and second.journal_state() == "hot"
        for readonly in (False, True):
            target = (
                (copy / "record.sqlite").as_uri() + "?mode=ro"
                if readonly
                else str(copy / "record.sqlite")
            )
            conn = None
            try:
                with pytest.raises(sqlite3.OperationalError):
                    conn = sqlite3.connect(target, uri=readonly, isolation_level=None, timeout=0)
                    conn.execute("SELECT count(*) FROM crash_probe").fetchone()
            finally:
                if conn is not None:
                    conn.close()
    conn = sqlite3.connect(copy / "record.sqlite")
    try:
        assert conn.execute("SELECT count(*) FROM crash_probe").fetchone()[0] == 0
    finally:
        conn.close()
    assert files(env.store_dir) == before


def test_true_orphan_never_sqlite_opens_original_and_recovers_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    orphan(env)
    before = files(env.store_dir)
    opened: list[str] = []
    original = sqlite3.connect

    def spy(path: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    with pytest.raises(PrometheosError) as err:
        env.svc.open(env.project_id)
    assert err.value.details["store_health"] == "needs_journal_recovery"
    recovered = env.svc.maintenance_journal_recover(env.project_id)
    assert recovered["journal_recovered"] and recovered["target_managed"]
    assert not any(str(env.store_dir / "record.sqlite") in path for path in opened)
    assert files(env.store_dir) == before
    assert env.svc.registry.lookup(env.project_id) == env.store_dir


def test_healthy_backup_preserves_real_two_connection_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    monkeypatch.setattr(sources, "INLINE_LIMIT", 1024)
    imported = env.import_files(["artifact.bin"])
    assert imported["plan"]["items"][0]["storage"] == "media"
    retained = [path for path in (env.store_dir / "media").rglob("*") if path.is_file()]
    assert len(retained) == 1
    assert retained[0].read_bytes() == (env.research / "artifact.bin").read_bytes()
    expected = env.seq()
    made = env.svc.backup(env.project_id, tmp_path / "healthy.zip", FULL)
    verified = backup.verify_backup(Path(made["backup"]), tmp_path / "verify")
    assert verified["manifest"]["project_seq"] == expected
    assert verified["manifest"]["media"] and env.seq() == expected


def test_pre_migration_backup_borrows_live_migration_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _schema6_env(tmp_path, monkeypatch)
    result = env.svc.migrate(env.project_id, authority=OWNER_ACT)
    assert result["from_schema"] == 6 and result["to_schema"] == len(MIGRATIONS)
    verified = backup.verify_backup(Path(result["backup"]["path"]), tmp_path / "verify")
    assert verified["manifest"]["schema_version"] == 6
    assert result["backup"]["verified"]


def test_nested_close_and_cross_thread_ownership(tmp_path: Path) -> None:
    env = Env(tmp_path)
    store = env.svc.open(env.project_id)
    first = store.connect()
    second = store.connect(readonly=True)
    assert isinstance(first, admission.Connection) and isinstance(second, admission.Connection)
    lease = first.lease
    assert lease is second.lease and lease is not None and len(lease.live) == 2
    second.close()
    assert len(lease.live) == 1
    with ThreadPoolExecutor(max_workers=1) as executor:

        def other_thread() -> str:
            try:
                conn = ProjectStore(env.store_dir).connect(readonly=True)
            except PrometheosError as exc:
                return exc.code
            conn.close()
            return "unexpected admission"

        assert executor.submit(other_thread).result(timeout=10) == "busy"
    first.close()
    assert not lease.live
    conn = store.connect(readonly=True)
    conn.close()


def test_zero_live_scope_rechecks_orphan_and_maintenance_order(tmp_path: Path) -> None:
    env = Env(tmp_path)
    store = env.svc.open(env.project_id)
    with admission.operation(store.db_path):
        conn = store.connect()
        conn.close()
        busy(lambda: _take_maintenance(store.directory))
        orphan(env)
        with pytest.raises(PrometheosError) as err:
            store.connect()
        assert err.value.details["store_health"] == "needs_journal_recovery"


def _take_maintenance(path: Path) -> None:
    with maintenance_lock(path):
        pass


def test_alias_and_two_process_gate_share_physical_identity(tmp_path: Path) -> None:
    env = Env(tmp_path)
    alias = tmp_path / "alias"
    alias.mkdir()
    os.link(env.store_dir / "record.sqlite", alias / "record.sqlite")
    store = env.svc.open(env.project_id)
    conn = store.connect()
    nested = ProjectStore(env.store_dir).connect(readonly=True)
    try:
        assert isinstance(conn, admission.Connection) and isinstance(nested, admission.Connection)
        assert conn.lease is nested.lease
        # Hard links share the gate, but their different journal location cannot borrow.
        busy(lambda: ProjectStore(alias).connect(readonly=True))
        script = """
import json, sys
sys.path.insert(0, sys.argv[2])
from prometheos.store import admission
from prometheos.store.project import ProjectStore
from prometheos.domain.models import PrometheosError
from pathlib import Path
admission.TIMEOUT_MS = 100
try:
    c = ProjectStore(Path(sys.argv[1])).connect(readonly=True)
except PrometheosError as e:
    print(json.dumps({'code': e.code}))
else:
    c.close()
    raise SystemExit('gate bypass')
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(alias), str(REPO / "src")],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert json.loads(child.stdout) == {"code": "busy"}
    finally:
        nested.close()
        conn.close()


def test_uncertain_read_is_not_none_or_cached_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    initial = admission.RawFile.__init__

    def unreadable(self: admission.RawFile, path: Path, *, strong: bool) -> None:
        if path.name == "record.sqlite-journal":
            raise PermissionError("synthetic denied read")
        initial(self, path, strong=strong)

    monkeypatch.setattr(admission.RawFile, "__init__", unreadable)
    with pytest.raises(PrometheosError) as err:
        maintenance.journal_state(env.store_dir)
    assert err.value.code == "io_error"
    assert not admission._owners()


def test_rollback_uncertainty_poison_prevents_nested_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    store = env.svc.open(env.project_id)
    initial = db.connect

    class FailedRollback(admission.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("synthetic rollback uncertainty")
            return super().execute(sql, *args, **kwargs)

    def failing_connect(path: Path, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = FailedRollback
        return initial(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(db, "connect", failing_connect)
        conn = store.connect()
        try:
            with pytest.raises(ValueError, match="original failure") as err:
                with db.write_transaction(conn):
                    raise ValueError("original failure")
            assert isinstance(err.value.__cause__, sqlite3.OperationalError)
            busy(lambda: store.connect(readonly=True))
        finally:
            conn.close()
    fresh = store.connect(readonly=True)
    fresh.close()


@pytest.mark.parametrize("content", [b"x", b"\0" * 7, b"abcdefgh", b"malformed" * 20])
def test_uncertain_journal_never_opens_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    env = Env(tmp_path)
    (env.store_dir / "record.sqlite-journal").write_bytes(content)
    before = files(env.store_dir)
    original = sqlite3.connect
    opened: list[str] = []

    def spy(path: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    with pytest.raises(PrometheosError) as err:
        env.svc.open(env.project_id)
    assert err.value.code == "io_error"
    assert err.value.details["recovery"]["hub"] == "/recovery"
    assert "store_health" not in err.value.details
    assert create_app(env.svc, env.project_id, Gate(port=1)).state.maintenance is True
    assert env.svc.doctor(env.project_id)["project"]["opens"] is False
    assert not any(str(env.store_dir / "record.sqlite") in path for path in opened)
    assert files(env.store_dir) == before


@pytest.mark.parametrize("mode", ["PERSIST", "TRUNCATE"])
def test_real_completed_journal_modes_remain_safe(tmp_path: Path, mode: str) -> None:
    env = Env(tmp_path)
    conn = sqlite3.connect(env.store_dir / "record.sqlite", isolation_level=None)
    try:
        assert conn.execute("PRAGMA journal_mode=" + mode).fetchone()[0] == mode.lower()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE completed_probe(x)")
        conn.execute("COMMIT")
    finally:
        conn.close()
    before = files(env.store_dir)
    assert maintenance.journal_state(env.store_dir) == "none"
    assert env.svc.open(env.project_id).directory == env.store_dir
    assert files(env.store_dir) == before


@pytest.mark.parametrize("fault", ["api-failure", "zero-identity"])
def test_native_identity_uncertainty_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    (tmp_path / "record.sqlite").write_bytes(b"synthetic raw identity target")
    initial = admission._win

    class Kernel:
        def __init__(self) -> None:
            self.kernel = initial()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.kernel, name)

        def GetFileInformationByHandle(self, handle: Any, info: Any) -> Any:
            if fault == "api-failure":
                ctypes.set_last_error(5)
                return 0
            result = self.kernel.GetFileInformationByHandle(handle, info)
            values = ctypes.cast(info, ctypes.POINTER(ctypes.c_ulong))
            values[11] = values[12] = 0
            return result

    monkeypatch.setattr(admission, "_win", Kernel)
    with pytest.raises(PrometheosError) as err:
        maintenance.journal_state(tmp_path)
    assert err.value.code == ("io_error" if fault == "api-failure" else "unsupported")
    assert not admission._owners()


def test_physical_replacement_requires_reopen_and_pin_blocks_delete(tmp_path: Path) -> None:
    env = Env(tmp_path)
    store = env.svc.open(env.project_id)
    database = env.store_dir / "record.sqlite"
    replacement = tmp_path / "replacement.sqlite"
    shutil.copyfile(database, replacement)
    conn = store.connect()
    try:
        with pytest.raises(PermissionError):
            os.replace(replacement, database)
    finally:
        conn.close()
    os.replace(replacement, database)
    busy(lambda: store.connect())
    reopened = env.svc.open(env.project_id)
    fresh = reopened.connect()
    fresh.close()


def test_final_missing_side_file_observation_refuses_metadata_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    initial = Path.stat
    journal = env.store_dir / "record.sqlite-journal"
    before = files(env.store_dir)
    sqlite_connect = sqlite3.connect
    opened: list[str] = []

    def spy(path: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(path))
        return sqlite_connect(path, *args, **kwargs)

    def uncertain(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == journal:
            raise PermissionError("synthetic final missing-file uncertainty")
        return initial(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", uncertain)
        patch.setattr(sqlite3, "connect", spy)
        with pytest.raises(PrometheosError) as err:
            maintenance.journal_state(env.store_dir)
        assert err.value.code == "io_error"
        assert not admission._owners()
        with pytest.raises(PrometheosError) as opened_error:
            env.svc.open(env.project_id)
        assert opened_error.value.code == "io_error"
        assert opened_error.value.details["recovery"]["hub"] == "/recovery"
        assert "store_health" not in opened_error.value.details
        assert create_app(env.svc, env.project_id, Gate(port=1)).state.maintenance is True
        assert env.svc.doctor(env.project_id)["project"]["opens"] is False
    assert not any(str(env.store_dir / "record.sqlite") in path for path in opened)
    assert files(env.store_dir) == before
    assert maintenance.journal_state(env.store_dir) == "none"


@pytest.mark.parametrize("state", ["HOT", "WAL"])
def test_late_identity_refusal_keeps_selected_project_recovery_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    env = Env(tmp_path)
    initial = ProjectStore.identity
    sqlite_connect = sqlite3.connect
    before: dict[str, tuple[str, int]] = {}
    opened: list[str] = []

    def spy(path: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(path))
        return sqlite_connect(path, *args, **kwargs)

    def change_before_identity(store: ProjectStore) -> str:
        if store.directory == env.store_dir and not before:
            if state == "HOT":
                orphan(env)
            else:
                database = store.db_path
                content = bytearray(database.read_bytes())
                content[18:20] = b"\x02\x02"
                database.write_bytes(content)
            before.update(files(env.store_dir))
            monkeypatch.setattr(sqlite3, "connect", spy)
        return initial(store)

    monkeypatch.setattr(ProjectStore, "identity", change_before_identity)
    with pytest.raises(PrometheosError) as err:
        env.svc.open(env.project_id)
    assert err.value.code == ("io_error" if state == "HOT" else "unsupported")
    assert err.value.details["store_health"] == (
        "needs_journal_recovery" if state == "HOT" else "unsupported_journal_mode"
    )
    assert err.value.details["project_id"] == env.project_id
    assert env.project_id in err.value.details["recovery"]["cli"]
    assert "unknown" not in err.value.details["recovery"]["cli"]
    assert files(env.store_dir) == before
    assert not any(str(env.store_dir / "record.sqlite") in path for path in opened)


def test_migration_grant_rechecks_at_actual_admission_before_spending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _schema6_env(tmp_path, monkeypatch)
    with pytest.raises(PrometheosError) as asked:
        env.svc.maintenance_migrate_request(env.project_id, AGENT)
    approval_id = asked.value.details["approval_request_id"]
    view = env.svc.maintenance_approval_get(env.project_id, OWNER, approval_id)
    env.svc.maintenance_decide(
        env.project_id, approval_id, grant=True, plan_sha256=view["plan_sha256"], owner=OWNER_ACT
    )
    initial = env.svc.migrate
    mutations: list[str] = []

    def mutate_before_admission(project_id: str, **kwargs: Any) -> dict[str, Any]:
        conn = ProjectStore(env.store_dir).connect(allow_pending=True)
        try:
            conn.execute("CREATE TABLE supported_write_in_old_handoff_gap(x)")
            mutations.append("committed")
        finally:
            conn.close()
        return initial(project_id, **kwargs)

    monkeypatch.setattr(env.svc, "migrate", mutate_before_admission)
    with pytest.raises(PrometheosError) as err:
        env.svc.maintenance_resume(env.project_id, AGENT, approval_id)
    assert mutations == ["committed"] and err.value.code == "revision_conflict"
    row = env.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "granted" and row.used_at is None
    conn = ProjectStore(env.store_dir).connect(readonly=True, allow_pending=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        conn.close()
    assert not list((env.store_dir / "exports").glob("pre-migrate-*.zip"))


def test_granted_migration_keeps_gate_from_authorization_through_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _schema6_env(tmp_path, monkeypatch)
    with pytest.raises(PrometheosError) as asked:
        env.svc.maintenance_migrate_request(env.project_id, AGENT)
    approval_id = asked.value.details["approval_request_id"]
    view = env.svc.maintenance_approval_get(env.project_id, OWNER, approval_id)
    env.svc.maintenance_decide(
        env.project_id, approval_id, grant=True, plan_sha256=view["plan_sha256"], owner=OWNER_ACT
    )
    initial = ProjectStore.upgrade
    crossings: list[str] = []

    def guarded_upgrade(
        store: ProjectStore, callback: Callable[[int, int], None]
    ) -> dict[str, Any]:
        row = env.svc.registry.maintenance_approval(approval_id)
        assert row is not None and row.state == "used"

        def competitor() -> str:
            try:
                conn = ProjectStore(store.directory).connect(allow_pending=True)
            except PrometheosError as exc:
                return exc.code
            conn.close()
            return "gate bypass"

        with ThreadPoolExecutor(max_workers=1) as pool:
            crossings.append(pool.submit(competitor).result(timeout=10))
        return initial(store, callback)

    monkeypatch.setattr(ProjectStore, "upgrade", guarded_upgrade)
    result = env.svc.maintenance_resume(env.project_id, AGENT, approval_id)
    assert crossings == ["busy"] and result["state"] == "used"
    assert result["result"]["to_schema"] == len(MIGRATIONS)


@pytest.mark.parametrize("content", [b"", b"\0" * 8, b"\0" * 8 + b"retained payload"])
def test_stable_completed_journal_forms_are_none(tmp_path: Path, content: bytes) -> None:
    env = Env(tmp_path)
    (env.store_dir / "record.sqlite-journal").write_bytes(content)
    before = files(env.store_dir)
    assert maintenance.journal_state(env.store_dir) == "none"
    assert env.svc.open(env.project_id).directory == env.store_dir
    assert files(env.store_dir) == before


@pytest.mark.parametrize("side", ["-wal", "-shm", "wal-header"])
def test_unsupported_wal_package_never_opens_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    env = Env(tmp_path)
    database = env.store_dir / "record.sqlite"
    if side == "wal-header":
        content = bytearray(database.read_bytes())
        content[18:20] = b"\x02\x02"
        database.write_bytes(content)
    else:
        (env.store_dir / ("record.sqlite" + side)).write_bytes(b"orphan WAL package")
    before = files(env.store_dir)
    original = sqlite3.connect
    opened: list[str] = []

    def spy(path: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    with pytest.raises(PrometheosError) as err:
        env.svc.open(env.project_id)
    assert err.value.code == "unsupported"
    assert err.value.details["store_health"] == "unsupported_journal_mode"
    report = env.svc.maintenance_inspect(env.project_id, AGENT)
    assert report["health"] == "unsupported_journal_mode" and report["opens"] is False
    assert report["database"]["journal"]["mode_supported"] is False
    assert report["database"]["schema_version"] is None
    assert report["database"]["integrity_checked"] is False
    assert not any(str(database) in path for path in opened)
    assert files(env.store_dir) == before


def test_lower_wal_header_hint_never_authorizes_migration(tmp_path: Path) -> None:
    env = Env(tmp_path)
    database = env.store_dir / "record.sqlite"
    content = bytearray(database.read_bytes())
    content[18:20] = b"\x02\x02"
    content[60:64] = (6).to_bytes(4, "big")
    database.write_bytes(content)
    before = files(env.store_dir)
    report = env.svc.maintenance_inspect(env.project_id, AGENT)
    assert report["health"] == "unsupported_journal_mode" and not report["opens"]
    assert report["database"]["header_schema_version"] == 6
    assert report["database"]["schema_version"] is None
    with pytest.raises(PrometheosError) as err:
        env.svc.maintenance_migrate_request(env.project_id, AGENT)
    assert err.value.code == "unsupported"
    assert env.svc.registry.maintenance_approvals(env.project_id) == []
    assert files(env.store_dir) == before


def test_mixed_hot_wal_is_unsupported_mode_without_mutation_authority(tmp_path: Path) -> None:
    # A04 round 2 (decision 0026, R1-M1): the unsupported mode outranks the hot journal, so no
    # route offers journal recovery or a switch that would then refuse; the hot state stays
    # visible in the journal fields.
    env = Env(tmp_path)
    orphan(env)
    (env.store_dir / "record.sqlite-wal").write_bytes(b"unsupported orphan WAL")
    before = files(env.store_dir)
    with pytest.raises(PrometheosError) as err:
        env.svc.open(env.project_id)
    assert err.value.code == "unsupported"
    assert err.value.details["store_health"] == "unsupported_journal_mode"
    report = env.svc.maintenance_inspect(env.project_id, AGENT)
    assert report["health"] == "unsupported_journal_mode" and report["opens"] is False
    assert report["health"] not in maintenance.DAMAGED
    assert report["database"]["journal"]["state"] == "hot"
    assert report["database"]["journal"]["mode_supported"] is False
    assert report["database"]["journal_recovery"]["health"] == "unsupported_journal_mode"
    managed = env.svc._maintenance().managed_dir(env.project_id)
    assert not managed.exists()
    calls = [
        lambda: env.svc.maintenance_journal_recover(env.project_id),
        lambda: env.svc.migrate(env.project_id, authority=OWNER_ACT),
        lambda: env.svc.maintenance_switch(
            env.project_id, AGENT, "unavailable.zip", str(managed / "restored-synthetic")
        ),
    ]
    for call in calls:
        with pytest.raises(PrometheosError) as err:
            call()
        assert err.value.code == "unsupported"
    assert not managed.exists()
    assert env.svc.registry.maintenance_approvals(env.project_id) == []
    assert env.svc.registry.quarantine_records(env.project_id) == []
    assert files(env.store_dir) == before


def test_recovery_rechecks_mode_before_any_managed_copy_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    orphan(env)
    initial = admission.Snapshot.__init__
    admissions: list[Path] = []

    def introduce_wal(
        snapshot: admission.Snapshot, directory: Path, lease: admission.Lease
    ) -> None:
        if directory == env.store_dir:
            admissions.append(directory)
            if len(admissions) == 2:
                (directory / "record.sqlite-wal").write_bytes(b"new unsupported side file")
        initial(snapshot, directory, lease)

    monkeypatch.setattr(admission.Snapshot, "__init__", introduce_wal)
    with pytest.raises(PrometheosError) as err:
        env.svc.maintenance_journal_recover(env.project_id)
    assert len(admissions) == 2 and err.value.code == "unsupported"
    assert not env.svc._maintenance().managed_dir(env.project_id).exists()
    assert env.svc.registry.quarantine_records(env.project_id) == []


def test_doctor_rechecks_admission_after_identity_before_source_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    initial = ProjectStore.connect
    sqlite_connect = sqlite3.connect
    before: dict[str, tuple[str, int]] = {}
    opened: list[str] = []

    def spy(path: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(path))
        return sqlite_connect(path, *args, **kwargs)

    def crash_before_read(store: ProjectStore, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if store.directory == env.store_dir and not before:
            orphan(env)
            before.update(files(env.store_dir))
            monkeypatch.setattr(sqlite3, "connect", spy)
        return initial(store, *args, **kwargs)

    monkeypatch.setattr(ProjectStore, "connect", crash_before_read)
    report = env.svc.doctor(env.project_id)
    assert report["project"]["opens"] is False
    assert report["project"]["recovery"]["hub"] == "/recovery"
    assert before and files(env.store_dir) == before
    assert not any(str(env.store_dir / "record.sqlite") in path for path in opened)


def test_final_switch_rechecks_unsupported_mode_before_spending_grant(tmp_path: Path) -> None:
    from test_cp10_recovery import World, _grant, _request_switch, _restore
    from test_cp10_recovery_r2 import _hot_journal

    world = World(tmp_path)
    target = _restore(world)
    _hot_journal(world)
    approval_id = _request_switch(world, target)
    _grant(world, approval_id)
    row = world.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "granted"
    (world.store / "record.sqlite-wal").write_bytes(b"unsupported final side file")
    before = files(world.store)
    with pytest.raises(PrometheosError) as err:
        world.svc._maintenance()._commit_switch(row.plan, row, "synthetic-owner", "test")
    assert err.value.code == "unsupported"
    current = world.svc.registry.maintenance_approval(approval_id)
    assert current is not None and current.state == "granted" and current.used_at is None
    assert world.svc.registry.lookup(world.pid) == world.store
    assert world.svc.registry.quarantine_records(world.pid) == []
    assert files(world.store) == before


def test_process_death_releases_gate_but_preserves_hot_refusal(tmp_path: Path) -> None:
    env = Env(tmp_path)
    script = """
import os, sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from prometheos.store.project import ProjectStore
c = ProjectStore(Path(sys.argv[1])).connect()
c.execute('PRAGMA cache_size=1')
c.execute('BEGIN IMMEDIATE')
c.execute('CREATE TABLE dead_writer(x)')
c.executemany('INSERT INTO dead_writer VALUES (?)', [('d'*800,)]*400)
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-I", "-c", script, str(env.store_dir), str(REPO / "src")],
        check=True,
        capture_output=True,
        timeout=20,
    )
    before = files(env.store_dir)
    assert maintenance.journal_state(env.store_dir) == "hot"
    with pytest.raises(PrometheosError) as err:
        env.svc.open(env.project_id)
    assert err.value.details["store_health"] == "needs_journal_recovery"
    assert files(env.store_dir) == before


# ----- A04 round 2 (decision 0026, review R1-M1): one shared source precedence ---------------

# case -> (open code, store health for open and inspect alike, agent switch request outcome)
PRECEDENCE = {
    "garbage": ("io_error", "corrupt", "approval_required"),
    "empty": ("io_error", "corrupt", "approval_required"),
    "garbage+hot": ("io_error", "needs_journal_recovery", "approval_required"),
    "garbage+wal": ("unsupported", "unsupported_journal_mode", "unsupported"),
    "garbage+shm": ("unsupported", "unsupported_journal_mode", "unsupported"),
    "missing+wal": ("unsupported", "unsupported_journal_mode", "unsupported"),
    "hot+wal": ("unsupported", "unsupported_journal_mode", "unsupported"),
    # R2-L1: the format-byte test needs a complete 100-byte header; a shorter magic-prefixed
    # main file is corrupt and switch-eligible, whatever its bytes 18-19 hold.
    "magic16": ("io_error", "corrupt", "approval_required"),
    "magic50-wal-bytes": ("io_error", "corrupt", "approval_required"),
    "magic50-rollback-bytes": ("io_error", "corrupt", "approval_required"),
}


def _damage_source(world: Any, case: str) -> None:
    from test_cp10_recovery_r2 import _hot_journal

    db = world.store / "record.sqlite"
    parts = case.split("+")
    if "hot" in parts:
        _hot_journal(world)
    if "garbage" in parts:
        data = bytearray(db.read_bytes())
        data[:16] = b"NOT-A-SQLITE-HDR"
        db.write_bytes(bytes(data))
    if "empty" in parts:
        db.write_bytes(b"")
    if "missing" in parts:
        db.unlink()
    if "magic16" in parts:
        db.write_bytes(b"SQLite format 3\x00")
    if "magic50-wal-bytes" in parts or "magic50-rollback-bytes" in parts:
        data = bytearray(db.read_bytes()[:50])
        assert data[:16] == b"SQLite format 3\x00" and data[18:20] == b"\x01\x01"
        if "magic50-wal-bytes" in parts:
            data[18:20] = b"\x02\x02"
        db.write_bytes(bytes(data))
    for side in ("wal", "shm"):
        if side in parts:
            (world.store / f"record.sqlite-{side}").write_bytes(b"")


@pytest.mark.parametrize("case", list(PRECEDENCE))
def test_open_inspect_and_switch_share_one_source_precedence(tmp_path: Path, case: str) -> None:
    from test_cp1_cli import Cli
    from test_cp10_recovery import World, _restore

    world = World(tmp_path)
    target = _restore(world)
    _damage_source(world, case)
    code, health, switch = PRECEDENCE[case]
    before = files(world.store)
    with pytest.raises(PrometheosError) as err:
        world.svc.open(world.pid)
    assert (err.value.code, err.value.details["store_health"]) == (code, health)
    assert err.value.details["recovery"]["hub"] == "/recovery"
    report = world.svc.maintenance_inspect(world.pid, AGENT, refresh=True)
    assert report["health"] == health and report["opens"] is False
    # Eligibility is derived from health on every route (CLI, hub): it must agree with switch.
    assert (health in maintenance.DAMAGED) == (switch == "approval_required")
    with pytest.raises(PrometheosError) as refused:
        world.svc.maintenance_switch(world.pid, AGENT, world.backup_name, target)
    assert refused.value.code == switch
    if switch == "unsupported":
        assert refused.value.details["store_health"] == "unsupported_journal_mode"
        assert world.svc.registry.maintenance_approvals(world.pid) == []
    # The same answers through a second transport (the CLI).
    cli = Cli(world.tmp)
    exit_code, inspected, _ = cli("maintenance", "inspect", "--project", world.pid)
    assert exit_code == 0 and inspected["health"] == health
    exit_code, opened, _ = cli("project", "info", "--project", world.pid)
    assert exit_code == EXIT_CODES[code] and opened["error"]["code"] == code
    assert opened["error"]["details"]["store_health"] == health
    assert world.svc.registry.lookup(world.pid) == world.store
    assert world.svc.registry.quarantine_records(world.pid) == []
    assert files(world.store) == before


def test_missing_database_with_side_files_reports_no_database_bytes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    (env.store_dir / "record.sqlite").unlink()
    (env.store_dir / "record.sqlite-wal").write_bytes(b"orphan")
    report = env.svc.maintenance_inspect(env.project_id, AGENT)
    database = report["database"]
    assert report["health"] == "unsupported_journal_mode"
    assert database["present"] is False and database["sha256"] == "missing"
    assert database["integrity"] == "missing" and database["integrity_checked"] is False
    assert database["schema_version"] is None and database["header_schema_version"] is None


def test_a_raw_sqlite_busy_during_open_keeps_the_recovery_context(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mapped = maintenance.open_failure(
        env.project_id, sqlite3.OperationalError("database is locked")
    )
    assert mapped.code == "busy"
    assert mapped.details["project_id"] == env.project_id
    assert mapped.details["recovery"] == maintenance.recovery_pointer(env.project_id)


def test_a_file_reference_above_the_json_integer_range_still_binds_a_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An NTFS file reference carries the MFT sequence number in its high 16 bits, so a reused
    # record often exceeds 2**53; the stored plan must still validate (A04 round 2).
    from test_cp10_recovery import World, _request_switch, _restore

    world = World(tmp_path)
    target = _restore(world)
    world.damage("garbage")
    initial = admission.RawFile.__init__

    def high_reference(file: admission.RawFile, path: Path, *, strong: bool) -> None:
        initial(file, path, strong=strong)
        file.key = (file.key[0], file.key[1] | (1 << 60))

    monkeypatch.setattr(admission.RawFile, "__init__", high_reference)
    approval_id = _request_switch(world, target)
    row = world.svc.registry.maintenance_approval(approval_id)
    assert row is not None
    identity = row.plan["source_binding"]["record.sqlite"]["identity"]
    assert all(isinstance(part, str) for part in identity) and int(identity[1]) >= 1 << 60


# ----- R2-L2: the owner's backup restore checks the registered store before writing -----------


def _owner_restore_case(tmp_path: Path, case: str) -> tuple[Env, Path, Path]:
    env = Env(tmp_path)
    backup_path = Path(env.svc.backup(env.project_id, None, FULL)["backup"])
    if case == "uncertain-journal":
        (env.store_dir / "record.sqlite-journal").write_bytes(b"XXXXXXXX" + b"\x00" * 504)
    elif case == "orphan-wal":
        (env.store_dir / "record.sqlite-wal").write_bytes(b"")
    return env, backup_path, tmp_path / "restored"


def test_owner_restore_over_an_inadmissible_store_refuses_before_writing(tmp_path: Path) -> None:
    env, backup_path, target = _owner_restore_case(tmp_path, "uncertain-journal")
    before_store, before_registry = (
        files(env.store_dir),
        dict(env.svc.registry.entry(env.project_id)),
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.restore(backup_path, target, caller=FULL)
    assert err.value.code == "io_error"
    # Nothing was written: no target, no staging folder beside it, no registry change.
    assert not target.exists()
    assert not list(target.parent.glob(f".{target.name}.restore-*"))
    assert dict(env.svc.registry.entry(env.project_id)) == before_registry
    assert env.svc.registry.quarantine_records(env.project_id) == []
    assert files(env.store_dir) == before_store
    assert err.value.details["project_id"] == env.project_id
    assert err.value.details["recovery"] == maintenance.recovery_pointer(env.project_id)


def test_owner_restore_over_an_unsupported_mode_store_still_quarantines_it(tmp_path: Path) -> None:
    env, backup_path, target = _owner_restore_case(tmp_path, "orphan-wal")
    previous, before_store = env.store_dir, files(env.store_dir)
    result = env.svc.restore(backup_path, target, caller=FULL)
    assert result["store"] == str(target) and target.is_dir()
    assert env.svc.registry.lookup(env.project_id) == target
    (record,) = env.svc.registry.quarantine_records(env.project_id)
    assert record.damaged_store_path == str(previous)
    assert files(previous) == before_store


def test_owner_restore_names_the_kept_target_when_the_previous_store_changes_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, backup_path, target = _owner_restore_case(tmp_path, "admissible")
    before_registry = dict(env.svc.registry.entry(env.project_id))
    original = backup.restore_backup

    def then_damage(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        (env.store_dir / "record.sqlite-journal").write_bytes(b"XXXXXXXX" + b"\x00" * 504)
        return result

    monkeypatch.setattr(backup, "restore_backup", then_damage)
    with pytest.raises(PrometheosError) as err:
        env.svc.restore(backup_path, target, caller=FULL)
    assert err.value.code == "io_error"
    assert err.value.details["restored_store"] == str(target) and target.is_dir()
    assert dict(env.svc.registry.entry(env.project_id)) == before_registry
    assert env.svc.registry.quarantine_records(env.project_id) == []
