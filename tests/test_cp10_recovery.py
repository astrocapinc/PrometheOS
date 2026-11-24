"""CP10.5 recovery (C10-RECOVERY, S27; spec/04 "Maintenance service"; decision 0021).

Every test uses a disposable home with a disposable store that is damaged on purpose: a
truncated or garbage ``record.sqlite``, a missing one, a newer schema, a database that is not
a PrometheOS store, a locked store, a corrupt backup archive and a tampered backup digest.

What is asserted, on the stored state:

- opening a damaged project fails with a recovery pointer on every transport, never a crash;
- the maintenance service (CLI, MCP, HTTP) inspects, verifies and restores into a NEW store
  without opening the damaged store's database and without writing a byte of the store;
- an agent's registry switch answers ``approval_required`` held in the per-user registry
  (``maintenance_approvals``) and binding the project, the damaged store and its database
  digest, the verified backup's manifest digest, the target, the caller rank and the plan
  digest; only an owner act grants it; the requesting rank uses it once, in the registry
  transaction that switches; a changed store, backup or target needs a new request;
- the registry switches only after verification, and the damaged store gets a quarantine
  record and stays listed for inspection.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, FULL, OWNER, OWNER_ACT, Env, create_entry
from hub_support import Hub, running_hub
from mcp import Client
from test_cp1_cli import Cli

from prometheos.app import approvals, maintenance
from prometheos.app.authority import OwnerAct
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_maintenance_server, build_server
from prometheos.store.lock import maintenance_lock
from prometheos.store.schema import MIGRATIONS

MODES = ("garbage", "truncated", "missing", "newer_schema", "not_a_store")
HEALTH = {
    "garbage": "corrupt",
    "truncated": "corrupt",
    "missing": "missing",
    "newer_schema": "newer_schema",
    "not_a_store": "not_a_store",
}


# ----- a disposable damaged project ----------------------------------------------------------


class World:
    """A disposable project with a retained source (media), a restricted entry and a verified
    backup in the store's exports folder; ``damage`` then breaks the store on purpose."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.env = Env(tmp)
        self.svc, self.pid = self.env.svc, self.env.project_id
        self.store = self.env.store_dir
        from prometheos.app import sources

        limit = sources.INLINE_LIMIT
        sources.INLINE_LIMIT = 1024  # artifact.bin (16 KiB) becomes a retained media file
        try:
            self.env.import_files(["baseline.md", "artifact.bin"])
        finally:
            sources.INLINE_LIMIT = limit
        assert any(p.is_file() for p in (self.store / "media").rglob("*"))
        self.env.apply([create_entry(title="Kept in the backup")])
        made = self.svc.backup(self.pid, None, FULL)
        self.backup_path = Path(made["backup"])
        self.backup_name = self.backup_path.name
        self.backup_sha = made["sha256"]

    def damage(self, mode: str) -> None:
        db = self.store / "record.sqlite"
        data = db.read_bytes()
        if mode == "garbage":
            db.write_bytes(b"\x00" * 100 + data[100:])
        elif mode == "truncated":
            db.write_bytes(data[:1024])
        elif mode == "missing":
            db.unlink()
        elif mode == "newer_schema":
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA user_version = 99")
            conn.close()
        elif mode == "not_a_store":
            db.unlink()
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE unrelated (x)")
            conn.close()
        else:  # pragma: no cover
            raise AssertionError(mode)

    def snapshot(self) -> dict[str, tuple[int, str, int]]:
        """Every file of the damaged store: size, digest and modification time."""
        found = {}
        for path in sorted(self.store.rglob("*")):
            if path.is_file():
                rel = path.relative_to(self.store).as_posix()
                stat = path.stat()
                found[rel] = (stat.st_size, _sha(path), stat.st_mtime_ns)
        return found

    def registry_row(self) -> dict[str, Any]:
        return dict(self.svc.registry.entry(self.pid))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def damaged(world: World) -> World:
    world.damage("garbage")
    return world


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every path ``sqlite3.connect`` opens, process-wide (services, MCP and the hub thread)."""
    seen: list[str] = []
    real = sqlite3.connect

    def spy(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        seen.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    yield seen


def _inside(paths: list[str], folder: Path) -> list[str]:
    """Opened databases inside ``folder`` (plain paths and ``file:`` URIs)."""
    root = str(folder).replace("\\", "/").casefold()
    hits = []
    for raw in paths:
        text = raw.replace("\\", "/").casefold()
        if text.startswith("file:"):
            text = text.split("?", 1)[0].removeprefix("file:").lstrip("/")
            root_cmp = root.lstrip("/")
        else:
            root_cmp = root
        if text.startswith(root_cmp + "/") or text == root_cmp:
            hits.append(raw)
    return hits


def _refused(fn: Callable[[], Any]) -> PrometheosError:
    with pytest.raises(PrometheosError) as err:
        fn()
    return err.value


def _request_switch(world: World, target: str, caller: Any = AGENT) -> str:
    err = _refused(
        lambda: world.svc.maintenance_switch(world.pid, caller, world.backup_name, target)
    )
    assert err.code == "approval_required", err.message
    return str(err.details["approval_request_id"])


def _restore(world: World) -> str:
    return str(world.svc.maintenance_restore(world.pid, world.backup_name)["target_store_path"])


def _grant(world: World, approval_id: str) -> dict[str, Any]:
    view = world.svc.maintenance_approval_get(world.pid, OWNER, approval_id)
    return world.svc.maintenance_decide(
        world.pid, approval_id, grant=True, plan_sha256=view["plan_sha256"], owner=OWNER_ACT
    )


# ----- opening a damaged project: a recovery pointer, never a crash ---------------------------


@pytest.mark.parametrize("mode", MODES)
def test_opening_a_damaged_store_answers_a_recovery_pointer(world: World, mode: str) -> None:
    world.damage(mode)
    err = _refused(lambda: world.svc.open(world.pid))
    assert err.code == ("unsupported" if mode == "newer_schema" else "io_error")
    assert err.details["recovery"]["hub"] == "/recovery"
    assert err.details["recovery"]["cli"].startswith("prometheos maintenance inspect")
    # Reads through the service carry the same pointer.
    info = _refused(lambda: world.svc.project_info(world.pid, AGENT))
    assert info.details["recovery"] == err.details["recovery"]
    # doctor reports the store with its pointer instead of failing.
    doctor = world.svc.doctor(world.pid)
    assert doctor["project"]["opens"] is False and doctor["project"]["recovery"]


def test_cli_mcp_and_http_answer_the_same_recovery_pointer(damaged: World) -> None:
    code, envelope, _ = Cli(damaged.tmp)("project", "info", "--project", damaged.pid)
    assert code == 5 and envelope["error"]["code"] == "io_error"
    pointer = envelope["error"]["details"]["recovery"]
    # MCP: the full server refuses to build; the maintenance mode serves the project instead.
    err = _refused(lambda: build_server(damaged.svc, damaged.pid))
    assert err.details["recovery"] == pointer
    build_maintenance_server(damaged.svc, damaged.pid)
    # HTTP: the hub starts in maintenance mode; project routes answer the pointer.
    with running_hub(damaged.svc, damaged.pid) as hub:
        assert hub.request("GET", "/api/v1/session").json()["mode"] == "maintenance"
        hub.pair()
        reply = hub.request("GET", "/api/v1/project")
        assert reply.status == 503
        body = reply.json()["error"]
        assert body["code"] == "io_error" and body["details"]["recovery"] == pointer
        assert hub.request("GET", "/api/v1/maintenance/inspect").status == 200


# ----- inspect: health without opening the store -------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_inspect_reports_health_without_opening_or_writing_the_store(
    world: World, mode: str, opened: list[str]
) -> None:
    world.damage(mode)
    # A store that never ran a maintenance operation has no lock file; inspecting it must not
    # create one (the lock is probed read-only).
    (world.store / "maintenance.lock").unlink(missing_ok=True)
    opened.clear()  # the test's own damage step is not the service
    before = world.snapshot()
    report = world.svc.maintenance_inspect(world.pid, AGENT)
    assert report["health"] == HEALTH[mode] and report["opens"] is False
    assert report["database"]["integrity"] == (
        "missing" if mode == "missing" else "corrupt" if HEALTH[mode] == "corrupt" else "ok"
    )
    if mode == "newer_schema":
        assert report["database"]["schema_version"] == 99
    assert report["database"]["supported_schema_version"] == len(MIGRATIONS)
    assert report["lock"] == {"maintenance": "free"}
    assert [b["name"] for b in report["backups"]] == [world.backup_name]
    assert report["environment"]["supported_schema_version"] == len(MIGRATIONS)
    assert _inside(opened, world.store) == []
    assert world.snapshot() == before


def test_a_healthy_store_inspects_ok_and_a_held_lock_is_reported(
    world: World, opened: list[str]
) -> None:
    report = world.svc.maintenance_inspect(world.pid, AGENT)
    assert report["health"] == "ok" and report["opens"] is True
    assert report["database"]["project_seq"] == world.env.seq()
    opened.clear()
    with maintenance_lock(world.store):
        assert world.svc.maintenance_inspect(world.pid, AGENT)["lock"] == {"maintenance": "held"}
    assert _inside(opened, world.store) == []


# ----- verify and restore -----------------------------------------------------------------------


def test_verify_and_restore_never_touch_the_damaged_store(
    damaged: World, opened: list[str]
) -> None:
    before = damaged.snapshot()
    verified = damaged.svc.maintenance_verify(damaged.pid, damaged.backup_name)
    assert verified["verified"] is True and verified["backup"]["sha256"] == damaged.backup_sha
    restored = damaged.svc.maintenance_restore(damaged.pid, damaged.backup_name)
    target = Path(restored["target_store_path"])
    assert restored["registry_switched"] is False and target.is_dir()
    assert restored["backup_manifest_sha256"] == verified["backup_manifest_sha256"]
    assert restored["target_store_label"].startswith("<PrometheOS home>/stores/")
    assert restored["target_managed"] is True
    assert damaged.registry_row()["store_path"] == str(damaged.store)  # unchanged
    assert _inside(opened, damaged.store) == []
    assert damaged.snapshot() == before
    # A restore never overwrites: the same folder again, or a folder inside a store, is refused.
    again = _refused(
        lambda: damaged.svc.maintenance_restore(
            damaged.pid, damaged.backup_name, target, owner=OWNER_ACT
        )
    )
    assert again.code == "validation_error"
    inside = _refused(
        lambda: damaged.svc.maintenance_restore(
            damaged.pid, damaged.backup_name, damaged.store / "nested", owner=OWNER_ACT
        )
    )
    assert inside.code == "validation_error" and not (damaged.store / "nested").exists()


def test_a_corrupt_archive_and_a_tampered_digest_are_refused(damaged: World) -> None:
    exports = damaged.store / "exports"
    garbage = exports / "garbage.zip"
    garbage.write_bytes(b"not a zip at all")
    tampered = exports / "tampered.zip"
    with zipfile.ZipFile(damaged.backup_path) as source, zipfile.ZipFile(tampered, "w") as out:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "record.sqlite":
                data = data[:-1] + bytes([data[-1] ^ 0xFF])  # same size, different digest
            out.writestr(info, data)
    (damaged.tmp / "other").mkdir()
    other = Env(damaged.tmp / "other")
    foreign = Path(other.svc.backup(other.project_id, None, FULL)["backup"])
    shutil.copyfile(foreign, exports / "foreign.zip")
    for name in ("garbage.zip", "tampered.zip", "foreign.zip"):
        err = _refused(lambda n=name: damaged.svc.maintenance_verify(damaged.pid, n))
        assert err.code == "validation_error", (name, err.message)
        target = damaged.tmp / f"restore-{name}"
        refused = _refused(
            lambda n=name, t=target: damaged.svc.maintenance_restore(
                damaged.pid, n, t, owner=OWNER_ACT
            )
        )
        assert refused.code == "validation_error"
        assert not target.exists() and not list(target.parent.glob(f".{target.name}*"))
    assert damaged.registry_row()["store_path"] == str(damaged.store)


# ----- the protected switch -----------------------------------------------------------------


def test_an_agent_switch_is_held_in_the_registry_and_bound(damaged: World) -> None:
    target = _restore(damaged)
    damaged_digest = _sha(damaged.store / "record.sqlite")
    approval_id = _request_switch(damaged, target)
    assert approval_id.startswith("mapreq_")
    assert damaged.registry_row()["store_path"] == str(damaged.store)  # nothing switched
    row = damaged.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "pending"
    verified = damaged.svc.maintenance_verify(damaged.pid, damaged.backup_name)
    assert (row.project_id, row.damaged_store_path, row.damaged_db_sha256) == (
        damaged.pid,
        str(damaged.store),
        damaged_digest,
    )
    assert row.backup_manifest_sha256 == verified["backup_manifest_sha256"]
    assert row.target_store_path == target and row.caller_rank == 0
    assert row.plan_sha256 == canonical_sha256(row.plan) and row.expires_at > row.created_at
    # An identical retry answers the same request; a second request ID for it is another row.
    assert _request_switch(damaged, target) == approval_id
    # No agent route grants: the service refuses anything but an owner act.
    plan = row.plan_sha256
    for forged in (None, "owner", AGENT, {"channel": "hub-owner"}):
        err = _refused(
            lambda f=forged: damaged.svc.maintenance_decide(
                damaged.pid, approval_id, grant=True, plan_sha256=plan, owner=f
            )
        )
        assert err.code == "policy_denied"
    pending = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id))
    assert pending.code == "approval_required"


def test_the_owner_grant_is_used_once_and_the_damaged_store_stays_listed(
    damaged: World, opened: list[str]
) -> None:
    before = damaged.snapshot()
    target = _restore(damaged)
    approval_id = _request_switch(damaged, target)
    granted = _grant(damaged, approval_id)
    assert granted["state"] == "granted" and granted["grant_expires_at"] == granted["expires_at"]
    # Only the rank that asked resumes it.
    other_rank = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, OWNER, approval_id))
    assert other_rank.code == "request_key_conflict"
    done = damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id)
    assert done["state"] == "used" and done["replayed"] is False
    result = done["result"]
    assert result["switched"] is True and result["store_path"] == target
    row = damaged.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "used" and row.used_at is not None
    assert damaged.registry_row()["store_path"] == target
    assert damaged.registry_row()["previous_store_path"] == str(damaged.store)
    [record] = damaged.svc.registry.quarantine_records(damaged.pid)
    assert record.maintenance_approval_id == approval_id
    assert record.damaged_store_path == str(damaged.store)
    assert record.damaged_integrity == "corrupt"
    assert record.replacement_store_path == target
    # Used once: a second resume replays, nothing switches again.
    again = damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id)
    assert again["replayed"] is True and again["result"]["quarantine"]["id"] == record.id
    assert len(damaged.svc.registry.quarantine_records(damaged.pid)) == 1
    # The project opens at the restored store; the damaged store is untouched and listed.
    assert damaged.svc.open(damaged.pid).directory == Path(target)
    assert damaged.env.seq() >= 1
    listed = damaged.svc.list_projects()[0]["quarantined_stores"]
    assert [q["quarantine_id"] for q in listed] == [record.id]
    inspected = damaged.svc.maintenance_inspect(damaged.pid, AGENT, record.id)
    assert inspected["store"]["kind"] == "quarantined" and inspected["health"] == "corrupt"
    assert _inside(opened, damaged.store) == []
    assert damaged.snapshot() == before


@pytest.mark.parametrize("change", ["store", "backup", "target", "registry"])
def test_a_changed_store_backup_or_target_needs_a_new_request(world: World, change: str) -> None:
    # A second, later backup of the same project (it verifies, but it is another backup).
    world.env.apply([create_entry(title="Only in the later backup")])
    later = Path(world.svc.backup(world.pid, world.tmp / "later.zip", FULL)["backup"])
    world.damage("garbage")
    damaged = world
    target = _restore(damaged)
    approval_id = _request_switch(damaged, target)
    _grant(damaged, approval_id)
    if change == "store":
        with (damaged.store / "record.sqlite").open("ab") as handle:
            handle.write(b"\x00")
    elif change == "backup":
        # Another valid backup of the same project now sits at the approved location.
        shutil.copyfile(later, damaged.backup_path)
    elif change == "target":
        restored = Path(target) / "record.sqlite"
        data = bytearray(restored.read_bytes())
        data[-1] ^= 0xFF
        restored.write_bytes(bytes(data))
    else:  # the registry was pointed elsewhere meanwhile (another switch)
        damaged.svc.registry.register(
            damaged.pid, damaged.tmp / "elsewhere", "x", "test", replace=True
        )
    err = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id))
    assert err.code in ("revision_conflict", "validation_error"), err.message
    row = damaged.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "granted"  # the grant was not spent
    assert damaged.svc.registry.quarantine_records(damaged.pid) == []
    if change != "registry":
        assert damaged.registry_row()["store_path"] == str(damaged.store)


def test_a_backup_with_another_manifest_needs_a_new_request(damaged: World) -> None:
    """The grant binds the verified manifest's digest itself: the same database under a
    different (still valid) manifest is another backup, even though the restored store still
    matches it byte for byte."""
    target = _restore(damaged)
    approval_id = _request_switch(damaged, target)
    _grant(damaged, approval_id)
    rewritten = damaged.backup_path.with_name("rewritten.zip")
    with zipfile.ZipFile(damaged.backup_path) as source, zipfile.ZipFile(rewritten, "w") as out:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["created_at"] = "2001-01-01T00:00:00.000000Z"
                data = json.dumps(manifest, indent=2).encode("utf-8")
            out.writestr(info, data)
    shutil.copyfile(rewritten, damaged.backup_path)
    assert damaged.svc.maintenance_verify(damaged.pid, damaged.backup_name)["verified"] is True
    err = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id))
    assert err.code == "revision_conflict" and "backup" in err.message
    row = damaged.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "granted"
    assert damaged.registry_row()["store_path"] == str(damaged.store)


def test_a_store_changed_just_before_the_registry_transaction_is_refused(
    damaged: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry transaction rechecks the damaged store's digest itself: a write landing
    after the plan was verified (here: during the lock probe that precedes the transaction)
    refuses the switch and leaves the grant unspent."""
    target = _restore(damaged)
    approval_id = _request_switch(damaged, target)
    _grant(damaged, approval_id)
    real = maintenance.lock_state

    def late_write(store_dir: Path) -> str:
        if store_dir == damaged.store:
            with (store_dir / "record.sqlite").open("ab") as handle:
                handle.write(b"\x00")
        return real(store_dir)

    monkeypatch.setattr(maintenance, "lock_state", late_write)
    err = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id))
    assert err.code == "revision_conflict"
    row = damaged.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "granted"
    assert damaged.registry_row()["store_path"] == str(damaged.store)
    assert damaged.svc.registry.quarantine_records(damaged.pid) == []


@pytest.mark.parametrize("ending", ["denied", "withdrawn", "expired", "revoked_by_expiry"])
def test_ended_requests_never_switch(
    damaged: World, ending: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    target = _restore(damaged)
    approval_id = _request_switch(damaged, target)
    view = damaged.svc.maintenance_approval_get(damaged.pid, OWNER, approval_id)
    if ending == "denied":
        damaged.svc.maintenance_decide(
            damaged.pid, approval_id, grant=False, plan_sha256=view["plan_sha256"], owner=OWNER_ACT
        )
    elif ending == "withdrawn":
        damaged.svc.maintenance_withdraw(damaged.pid, AGENT, approval_id)
    else:
        if ending == "revoked_by_expiry":
            _grant(damaged, approval_id)
        later = datetime.now(UTC) + timedelta(days=2)
        monkeypatch.setattr(approvals, "_now", lambda: later)
    err = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id))
    assert err.code == "policy_denied"
    expected = "expired" if ending == "revoked_by_expiry" else ending
    assert err.details["state"] == expected
    assert damaged.registry_row()["store_path"] == str(damaged.store)


def test_the_switch_happens_only_after_verification(damaged: World) -> None:
    target = Path(_restore(damaged))
    media = sorted((target / "media").rglob("*"))
    victim = next(p for p in media if p.is_file())
    victim.unlink()  # the restored store lost a media file
    err = _refused(
        lambda: damaged.svc.maintenance_switch(damaged.pid, AGENT, damaged.backup_name, str(target))
    )
    assert err.code == "validation_error" and "media" in err.message
    owner = _refused(
        lambda: damaged.svc.maintenance_switch(
            damaged.pid, AGENT, damaged.backup_name, str(target), owner=OWNER_ACT
        )
    )
    assert owner.code == "validation_error"
    empty = damaged.tmp / "empty-target"
    empty.mkdir()
    err = _refused(
        lambda: damaged.svc.maintenance_switch(
            damaged.pid, AGENT, damaged.backup_name, str(empty), owner=OWNER_ACT
        )
    )
    assert err.code == "validation_error"
    assert damaged.registry_row()["store_path"] == str(damaged.store)
    assert damaged.svc.registry.quarantine_records(damaged.pid) == []


def test_a_held_maintenance_lock_makes_the_switch_wait(damaged: World) -> None:
    target = _restore(damaged)
    # The lock file is created by the holder (here the test), never by the maintenance service.
    with maintenance_lock(damaged.store):
        assert damaged.svc.maintenance_inspect(damaged.pid, AGENT)["lock"] == {
            "maintenance": "held"
        }
        busy = _refused(
            lambda: damaged.svc.maintenance_switch(
                damaged.pid, AGENT, damaged.backup_name, target, owner=OWNER_ACT
            )
        )
        assert busy.code == "busy"
    done = damaged.svc.maintenance_switch(
        damaged.pid, AGENT, damaged.backup_name, target, owner=OWNER_ACT
    )
    assert done["switched"] is True and done["quarantine"]["maintenance_approval_id"] is None


def test_owner_restore_over_a_registered_project_quarantines_the_previous_store(
    world: World,
) -> None:
    into = world.tmp / "restored-by-owner"
    owner = type(FULL)(actor="cli-owner-restore", scope="owner", full_data=True)
    result = world.svc.restore(world.backup_path, into, caller=owner)
    assert result["previous_store"] == str(world.store)
    assert result["quarantine"]["damaged_integrity"] == "ok"
    [record] = world.svc.registry.quarantine_records(world.pid)
    assert record.replacement_store_path == str(into) and record.maintenance_approval_id is None
    assert world.svc.open(world.pid).directory == into


# ----- migrate requests and backup requests (the agent request paths) ----------------------------


def test_an_agent_migrate_request_is_a_maintenance_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_cp10_migrations import PREVIOUS, _schema6_env

    env = _schema6_env(tmp_path, monkeypatch)
    svc, pid = env.svc, env.project_id
    err = _refused(lambda: svc.open(pid))
    assert err.code == "unsupported" and "migrate" in err.details["recovery"]
    assert svc.maintenance_inspect(pid, AGENT)["health"] == "needs_migration"
    asked = _refused(lambda: svc.maintenance_migrate_request(pid, AGENT))
    assert asked.code == "approval_required"
    approval_id = asked.details["approval_request_id"]
    assert asked.details["owner_route"]["hub"] == "/recovery"
    row = svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.plan["action"] == "migrate"
    assert row.plan["from_schema"] == PREVIOUS and row.target_store_path is None
    view = svc.maintenance_approval_get(pid, OWNER, approval_id)
    svc.maintenance_decide(
        pid, approval_id, grant=True, plan_sha256=view["plan_sha256"], owner=OWNER_ACT
    )
    done = svc.maintenance_resume(pid, AGENT, approval_id)
    assert done["operation"] == "maintenance_migrate"
    assert done["result"]["to_schema"] == len(MIGRATIONS) and done["result"]["backup"]["verified"]
    assert svc.open(pid)  # opens at the current schema
    assert svc.maintenance_resume(pid, AGENT, approval_id)["replayed"] is True
    # A healthy store does not take a migrate request.
    assert _refused(lambda: svc.maintenance_migrate_request(pid, AGENT)).code == "validation_error"


def test_an_agent_backup_request_runs_once_after_the_owner_grant(world: World) -> None:
    err = _refused(lambda: world.svc.approval_request(world.pid, AGENT, "backup_create", {}))
    assert err.code == "approval_required"
    approval_id = err.details["approval_request_id"]
    shown = world.svc.approval_owner_view(world.pid, approval_id)
    assert "restricted and private" in shown["summary"][0]
    world.svc.approval_decide(
        world.pid,
        approval_id,
        grant=True,
        plan_sha256=shown["plan_sha256"],
        owner=OWNER_ACT,
    )
    done = world.svc.approval_resume(world.pid, AGENT, approval_id)
    made = done["result"]
    assert made["created"] is True and made["verified"] is True
    assert (world.store / "exports" / made["backup"]["name"]).is_file()
    again = world.svc.approval_resume(world.pid, AGENT, approval_id)
    assert again["replayed"] is True
    assert len(list((world.store / "exports").glob("*.zip"))) == 2  # the first one plus this


# ----- S27 through the service, MCP and HTTP ------------------------------------------------------


def _mcp(svc: Any, pid: str, steps: Callable[[Client], Any], *, maintenance_mode: bool) -> Any:
    async def run() -> Any:
        built = build_maintenance_server(svc, pid) if maintenance_mode else build_server(svc, pid)
        async with Client(built) as client:
            return await steps(client)

    return anyio.run(run)


def _tool_error(result: Any) -> dict[str, Any]:
    assert result.is_error
    text = result.content[0].text
    error: dict[str, Any] = json.loads(text[text.index("{") :])["error"]
    return error


def test_s27_through_mcp_maintenance_mode(damaged: World, opened: list[str]) -> None:
    before = damaged.snapshot()

    async def agent(client: Client) -> Any:
        names = {t.name for t in (await client.list_tools()).tools}
        inspected = await client.call_tool("maintenance_inspect", {})
        verified = await client.call_tool("maintenance_verify", {"backup": damaged.backup_name})
        restored = await client.call_tool("maintenance_restore", {"backup": damaged.backup_name})
        assert restored.structured_content.get("target_store_path") is None  # labels (F9)
        target = restored.structured_content["target_store_label"]
        asked = await client.call_tool(
            "maintenance_switch", {"backup": damaged.backup_name, "target": target}
        )
        return names, inspected, verified, restored, target, asked

    names, inspected, verified, restored, target, asked = _mcp(
        damaged.svc, damaged.pid, agent, maintenance_mode=True
    )
    assert "maintenance_inspect" in names and "record_get" not in names
    assert not any(n.endswith(("_grant", "_deny", "_decide", "_review")) for n in names)
    assert inspected.structured_content["health"] == "corrupt"
    home = str(damaged.env.home)
    for reply in (inspected, verified, restored):  # round 2, F9: no absolute path over MCP
        text = json.dumps(reply.structured_content)
        assert home.replace("\\", "\\\\") not in text and home not in text
    assert verified.structured_content["verified"] is True
    assert restored.structured_content["registry_switched"] is False
    error = _tool_error(asked)
    assert error["code"] == "approval_required"
    approval_id = error["details"]["approval_request_id"]
    assert damaged.registry_row()["store_path"] == str(damaged.store)
    _grant(damaged, approval_id)  # the owner (hub or interactive session; see the HTTP test)

    async def resume(client: Client) -> Any:
        first = await client.call_tool("maintenance_resume", {"approval_id": approval_id})
        second = await client.call_tool("maintenance_resume", {"approval_id": approval_id})
        return first, second

    first, second = _mcp(damaged.svc, damaged.pid, resume, maintenance_mode=True)
    assert first.structured_content["replayed"] is False
    assert second.structured_content["replayed"] is True
    assert maintenance.store_label(damaged.registry_row()["store_path"], damaged.env.home) == (
        target
    )
    assert _inside(opened, damaged.store) == []
    assert damaged.snapshot() == before


def _agent(hub: Hub, method: str, path: str, body: Any = None) -> Any:
    token = hub.extra.setdefault("token", hub.gate.new_agent_token())
    return hub.request(
        method, path, body, cookie=None, csrf=None, origin=None,
        headers={"Authorization": f"Bearer {token}"},
    )  # fmt: skip


@pytest.mark.parametrize("mode", ["garbage", "truncated"])
def test_s27_through_the_hub_with_the_owner_grant_on_the_recovery_routes(
    world: World, opened: list[str], mode: str
) -> None:
    # "truncated" keeps a valid header, so only the maintenance mode keeps the hub from
    # reading the damaged database (round 2: the redaction and the routes open nothing).
    world.damage(mode)
    damaged = world
    before = damaged.snapshot()
    home = str(damaged.env.home)
    with running_hub(damaged.svc, damaged.pid) as hub:
        # Starting the hub tried to open the project read-only once and chose maintenance
        # mode; from here on nothing opens a database inside the damaged store.
        opened.clear()
        assert _agent(hub, "GET", "/api/v1/project").json()["error"]["details"]["recovery"]
        inspected = _agent(hub, "GET", "/api/v1/maintenance/inspect")
        assert inspected.status == 200 and inspected.json()["health"] == "corrupt"
        name = inspected.json()["backups"][0]["name"]
        assert _agent(hub, "POST", "/api/v1/maintenance/verify", {"backup": name}).status == 200
        restored = _agent(hub, "POST", "/api/v1/maintenance/restore", {"backup": name}).json()
        assert "target_store_path" not in restored  # the hub shows labels, never paths
        label = restored["target_store_label"]
        asked = _agent(hub, "POST", "/api/v1/maintenance/switch", {"backup": name, "target": label})
        assert asked.status == 403 and asked.json()["error"]["code"] == "approval_required"
        approval_id = asked.json()["error"]["details"]["approval_request_id"]
        # The agent token cannot grant; the paired owner session can.
        view = _agent(hub, "GET", f"/api/v1/maintenance/approvals/{approval_id}").json()
        refused = _agent(
            hub, "POST", f"/api/v1/maintenance/approvals/{approval_id}/grant",
            {"plan_sha256": view["plan_sha256"]},
        )  # fmt: skip
        assert refused.status == 403
        assert damaged.registry_row()["store_path"] == str(damaged.store)
        hub.pair()
        queue = hub.request("GET", "/api/v1/maintenance/inspect").json()["approvals"]
        assert [a["approval_request_id"] for a in queue] == [approval_id]
        granted = hub.request(
            "POST",
            f"/api/v1/maintenance/approvals/{approval_id}/grant",
            {"plan_sha256": queue[0]["plan_sha256"]},
        )
        assert granted.status == 200 and granted.json()["state"] == "granted"
        done = _agent(hub, "POST", f"/api/v1/maintenance/approvals/{approval_id}/resume")
        assert done.status == 200 and done.json()["state"] == "used"
        after = hub.request("GET", "/api/v1/maintenance/inspect").json()
        for reply in (inspected.json(), after, restored, view, done.json()):
            assert home.casefold() not in json.dumps(reply).casefold()
            assert home.replace("\\", "/").casefold() not in json.dumps(reply).casefold()
    assert after["health"] == "ok" and after["quarantine"][0]["damaged_integrity"] == "corrupt"
    assert damaged.registry_row()["store_path"].endswith(label.rsplit("/", 1)[1])
    assert _inside(opened, damaged.store) == []
    assert damaged.snapshot() == before


def test_the_cli_maintenance_commands_and_owner_review(damaged: World) -> None:
    cli = Cli(damaged.tmp)
    code, report, _ = cli("maintenance", "inspect", "--project", damaged.pid)
    assert code == 0 and report["health"] == "corrupt"
    code, verified, _ = cli("maintenance", "verify", damaged.backup_name, "--project", damaged.pid)
    assert code == 0 and verified["verified"] is True
    into = damaged.tmp / "cli-restored"
    # An agent (non-interactive) cannot name the location (round 2, F1).
    code, refused, _ = cli(
        "maintenance", "restore", damaged.backup_name, "--into", str(into), "--project", damaged.pid
    )
    assert code == 4 and refused["error"]["code"] == "policy_denied" and not into.exists()
    code, restored, _ = cli("maintenance", "restore", damaged.backup_name, "--project", damaged.pid)
    assert code == 0 and restored["target_managed"] is True
    label = restored["target_store_label"]
    assert label.startswith("<PrometheOS home>/stores/")
    code, asked, _ = cli(
        "maintenance", "switch", damaged.backup_name, "--target", label, "--project", damaged.pid
    )  # the label is accepted on the CLI too (round 2, F8)
    assert code == 4 and asked["error"]["code"] == "approval_required"
    approval_id = asked["error"]["details"]["approval_request_id"]
    # The owner session is interactive only: a non-interactive review is refused.
    code, review, _ = cli("maintenance", "review", "--request", approval_id, "--grant",
                          "--project", damaged.pid)  # fmt: skip
    assert code == 4 and review["error"]["code"] == "policy_denied"
    code, migrate, _ = cli("maintenance", "migrate", "--project", damaged.pid)
    assert code == 2 and migrate["error"]["code"] == "validation_error"  # corrupt, not old
    _grant(damaged, approval_id)
    code, done, _ = cli("maintenance", "resume", approval_id, "--project", damaged.pid)
    assert code == 0 and done["result"]["store_path"] == restored["target_store_path"]
    code, listed, _ = cli("project", "list")
    [entry] = listed["projects"]
    assert code == 0 and entry["quarantined_stores"][0]["damaged_integrity"] == "corrupt"


def test_maintenance_tools_are_a_subset_of_the_full_server(world: World) -> None:
    async def names(client: Client) -> set[str]:
        return {t.name for t in (await client.list_tools()).tools}

    full = _mcp(world.svc, world.pid, names, maintenance_mode=False)
    only = _mcp(world.svc, world.pid, names, maintenance_mode=True)
    assert only <= full
    assert only == {n for n in full if n.startswith("maintenance_")}


def test_labels_hide_local_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert (
        maintenance.store_label(home / "projects" / "p1", home) == "<PrometheOS home>/projects/p1"
    )
    assert maintenance.store_label(tmp_path / "elsewhere" / "kers", home) == "<local path>/kers"
    m = maintenance.Maintenance(type("S", (), {"home": home, "registry": None})())
    assert m.resolve_location("<PrometheOS home>/projects/p1") == Path(
        os.path.abspath(home / "projects" / "p1")
    )
    for bad in ("<PrometheOS home>/../x", "<PrometheOS home>/a/./b", "relative/folder", ""):
        assert _refused(lambda b=bad: m.resolve_location(b)).code == "validation_error"


def test_no_owner_act_is_built_by_maintenance_agent_routes() -> None:
    """Static: the maintenance module builds an owner act only after spending a grant."""
    source = Path(maintenance.__file__).read_text(  # the module actually imported
        encoding="utf-8"
    )
    assert source.count("OwnerAct(") == 1
    spent = source.index("_use_grant(conn, current, approvals.now_ts())")
    assert spent < source.index("OwnerAct(")
    assert isinstance(OWNER_ACT, OwnerAct)
