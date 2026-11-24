"""CP10.5 pre-check round 2 (review-precheck-cp10-5-a; decision 0021 "Round 2").

One regression per finding and per surviving mutant of the review:

- F1: an agent restores only into a new folder PrometheOS creates under the home, and an
  agent's switch target must be such a folder; the owner may name a location.
- F2: only a store that does not open can be switched; the owner's card shows the current
  health and sequence and what becomes live; ``backup restore`` over a healthy store asks to
  confirm a rollback.
- F3: a hot rollback journal is ``needs_journal_recovery`` (open and inspect agree), recovered
  into a NEW store through a verified backup, never in place; an active write is not hot.
- F4: unreadable databases refuse a switch with a typed ``io_error``; scratch failures and
  file-system errors are typed on every transport.
- F5: opening a damaged WAL-mode store creates no side file.
- F6: in maintenance mode no route opens the damaged database (backups, owner migrate).
- F7: the owner's restore into the lost store's own location registers it without a crash.
- F8, F9: one location rule (home labels) everywhere; MCP replies carry labels, not paths.
- F10 (MM1-MM7, MM9): grant digest, in-transaction registry and target rechecks, migrate
  digest recheck, 30 s grant window, media digests, withdraw by another rank, owner routes.
- F11: inspect results are cached per store state, one probe at a time.
"""

from __future__ import annotations

import ast
import builtins
import errno
import json
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, FULL, OWNER, OWNER_ACT, create_entry
from hub_support import running_hub
from mcp import Client
from test_cp10_recovery import (
    World,
    _agent,
    _grant,
    _inside,
    _refused,
    _request_switch,
    _restore,
    _sha,
    _tool_error,
)

from prometheos.app import approvals, maintenance
from prometheos.mcp_adapter import build_maintenance_server
from prometheos.store.schema import MIGRATIONS

REPO = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def damaged(world: World) -> World:
    world.damage("garbage")
    return world


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every path ``sqlite3.connect`` opens, process-wide (as in test_cp10_recovery)."""
    seen: list[str] = []
    real = sqlite3.connect

    def spy(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        seen.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    yield seen


def _files(store: Path) -> dict[str, tuple[int, str]]:
    return {
        p.relative_to(store).as_posix(): (p.stat().st_size, _sha(p))
        for p in sorted(store.rglob("*"))
        if p.is_file()
    }


CRASH = r"""
import sqlite3, os, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("PRAGMA cache_size = 1")
c.execute("BEGIN IMMEDIATE")
c.execute("CREATE TABLE crash_probe (x)")
c.executemany("INSERT INTO crash_probe VALUES (?)", [("z" * 800,) for _ in range(400)])
os._exit(0)  # killed mid-transaction: the spilled pages leave a hot journal
"""


def _hot_journal(world: World) -> None:
    db = world.store / "record.sqlite"
    subprocess.run([sys.executable, "-c", CRASH, str(db)], check=True)
    assert (world.store / "record.sqlite-journal").is_file()
    assert maintenance.journal_state(world.store) == "hot"


# ----- F1: agents restore only into the managed area ----------------------------------------


def test_an_agent_cannot_restore_to_a_location_it_names(damaged: World) -> None:
    outside = damaged.tmp / "agent-chosen" / "public-share"
    err = _refused(
        lambda: damaged.svc.maintenance_restore(damaged.pid, damaged.backup_name, outside)
    )
    assert err.code == "policy_denied" and not outside.exists()
    label = _refused(
        lambda: damaged.svc.maintenance_restore(
            damaged.pid, damaged.backup_name, "<PrometheOS home>/elsewhere/x"
        )
    )
    assert label.code == "policy_denied"
    declared = _refused(
        lambda: damaged.svc.maintenance_restore(
            damaged.pid, damaged.backup_name, declare_local=True
        )
    )
    assert declared.code == "policy_denied"
    with running_hub(damaged.svc, damaged.pid) as hub:
        reply = _agent(
            hub, "POST", "/api/v1/maintenance/restore",
            {"backup": damaged.backup_name, "into": str(outside)},
        )  # fmt: skip
        assert reply.status == 403 and reply.json()["error"]["code"] == "policy_denied"
    assert not outside.exists()

    for into in (str(outside), "<PrometheOS home>/elsewhere/y"):  # one rule on MCP too (F8)
        envelope = _tool_error(
            _run_mcp(
                damaged,
                lambda c, i=into: c.call_tool(
                    "maintenance_restore", {"backup": damaged.backup_name, "into": i}
                ),
            )
        )
        assert envelope["code"] == "policy_denied"
    assert not outside.exists()


def test_an_agents_switch_target_must_be_a_managed_restore(damaged: World) -> None:
    outside = damaged.tmp / "owner-chosen"
    restored = damaged.svc.maintenance_restore(
        damaged.pid, damaged.backup_name, outside, owner=OWNER_ACT
    )
    assert restored["target_managed"] is False
    err = _refused(
        lambda: damaged.svc.maintenance_switch(
            damaged.pid, AGENT, damaged.backup_name, str(outside)
        )
    )
    assert err.code == "policy_denied" and "stores" in err.message
    assert damaged.svc.registry.maintenance_approvals(damaged.pid) == []
    # The owner may switch to a location the owner chose; the record says where it went.
    done = damaged.svc.maintenance_switch(
        damaged.pid, OWNER, damaged.backup_name, str(outside), owner=OWNER_ACT
    )
    assert done["store_path"] == str(outside)


def test_unswitched_agent_restores_are_capped_and_known_stores_do_not_count(
    damaged: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "MAX_UNSWITCHED_RESTORES", 1)
    target = _restore(damaged)
    err = _refused(lambda: _restore(damaged))
    assert err.code == "policy_denied" and err.details == {"limit": 1}
    assert (
        len(list(maintenance.Maintenance(damaged.svc).managed_dir(damaged.pid).glob("restored-*")))
        == 1
    )
    approval_id = _request_switch(damaged, target)
    _grant(damaged, approval_id)
    damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id)
    # The registered replacement and historical quarantine paths are retained, rather than
    # counted as disposable unswitched agent output.
    kept = damaged.tmp / "kept.zip"
    kept.write_bytes(damaged.backup_path.read_bytes())
    done = damaged.svc.maintenance_restore(damaged.pid, str(kept))
    assert done["restored"] is True


@pytest.mark.skipif(not WINDOWS, reason="junction regression needs Windows")
def test_agents_cannot_restore_through_a_managed_area_junction(damaged: World) -> None:
    managed = maintenance.Maintenance(damaged.svc).managed_dir(damaged.pid)
    outside = damaged.tmp / "outside-home"
    outside.mkdir()
    managed.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(managed), str(outside)], check=True, capture_output=True
    )
    before = list(outside.iterdir())
    try:
        err = _refused(lambda: _restore(damaged))
        assert err.code == "policy_denied" and list(outside.iterdir()) == before
    finally:
        managed.rmdir()  # removes the junction only; the outside directory is untouched


def test_the_owner_card_shows_what_becomes_live_and_the_current_store(damaged: World) -> None:
    target = _restore(damaged)
    approval_id = _request_switch(damaged, target)
    view = damaged.svc.maintenance_approval_get(damaged.pid, OWNER, approval_id)
    live, current = view["summary"][0], view["summary"][1]
    verified = damaged.svc.maintenance_verify(damaged.pid, damaged.backup_name)
    assert live.startswith("becomes live: the restored store <PrometheOS home>/stores/")
    assert damaged.backup_name in live and verified["manifest"]["created_at"] in live
    assert f"project sequence {verified['manifest']['project_seq']}" in live
    assert "is corrupt" in current and "latest sequence unknown" in current
    assert view["plan"]["store_health"] == "corrupt"
    assert view["plan"]["target_managed"] is True


# ----- F2: only a store that does not open is switched -----------------------------------------


def test_a_healthy_store_cannot_be_switched_away(world: World) -> None:
    world.env.apply([create_entry(title="work after the backup")])
    target = _restore(world)
    for kwargs in ({}, {"owner": OWNER_ACT}):
        err = _refused(
            lambda kw=kwargs: world.svc.maintenance_switch(
                world.pid, AGENT, world.backup_name, target, **kw
            )
        )
        assert err.code == "policy_denied" and err.details["store_health"] == "ok"
        assert "backup restore" in err.message
    assert world.svc.registry.maintenance_approvals(world.pid) == []
    assert world.registry_row()["store_path"] == str(world.store)


def test_backup_restore_over_a_healthy_store_confirms_a_rollback(world: World) -> None:
    from prometheos.cli import _restore_summary

    world.env.apply([create_entry(title="work after the backup")])
    manifest = world.svc.verify_backup(world.backup_path)["manifest"]
    summary, purpose = _restore_summary(world.svc, world.backup_path, world.tmp / "x", manifest)
    assert "ROLLBACK" in summary and purpose == "To roll the project back to this backup"
    assert f"at sequence {world.env.seq()}" in summary
    assert f"this backup is at sequence {manifest['project_seq']}" in summary
    world.damage("garbage")
    maintenance._CACHE.clear()
    summary, purpose = _restore_summary(world.svc, world.backup_path, world.tmp / "x", manifest)
    assert "ROLLBACK" not in summary and "is corrupt" in summary


# ----- F3: a hot rollback journal ---------------------------------------------------------------


def test_a_hot_journal_is_recovered_into_a_new_store_never_in_place(
    world: World, opened: list[str]
) -> None:
    world.env.apply([create_entry(title="the last committed work")])
    committed = world.env.seq()
    _hot_journal(world)
    before = _files(world.store)
    opened.clear()
    err = _refused(lambda: world.svc.open(world.pid))
    assert err.code == "io_error" and err.details["store_health"] == "needs_journal_recovery"
    report = world.svc.maintenance_inspect(world.pid, AGENT)
    assert report["health"] == "needs_journal_recovery" and report["opens"] is False
    assert report["database"]["journal"]["state"] == "hot"
    assert report["database"]["journal_recovery"]["project_seq"] == committed
    assert _inside(opened, world.store) == []
    recovered = world.svc.maintenance_journal_recover(world.pid)
    assert recovered["journal_recovered"] is True and recovered["target_managed"] is True
    assert recovered["recovered_project_seq"] == committed
    assert recovered["backup"]["name"].startswith("journal-recovered-")
    err = _refused(
        lambda: world.svc.maintenance_switch(
            world.pid, AGENT, recovered["backup"]["name"], recovered["target_store_label"]
        )
    )
    assert err.code == "approval_required"
    approval_id = err.details["approval_request_id"]
    _grant(world, approval_id)
    world.svc.maintenance_resume(world.pid, AGENT, approval_id)
    assert world.env.seq() == committed  # the project opens at its last committed work
    assert _inside(opened, world.store) == []
    assert _files(world.store) == before  # the journal was never applied in place
    [record] = world.svc.registry.quarantine_records(world.pid)
    assert record.damaged_store_path == str(world.store)


@pytest.mark.skipif(not WINDOWS, reason="source sharing is guarded on Windows only")
def test_an_active_write_is_not_a_hot_journal(world: World) -> None:
    target = _restore(world)
    writer = sqlite3.connect(world.store / "record.sqlite", isolation_level=None)
    try:
        writer.execute("PRAGMA cache_size = 1")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("CREATE TABLE in_progress (x)")
        # Enough to spill pages: the journal's header is written while the writer still holds
        # its lock, exactly what a crash would leave, except that the writer is alive.
        writer.executemany("INSERT INTO in_progress VALUES (?)", [("w" * 800,)] * 400)
        head = (world.store / "record.sqlite-journal").read_bytes()[:8]
        assert head == maintenance.JOURNAL_MAGIC
        # Decision 0025 refuses unavailable writable handles before sampling/classification.
        assert _refused(lambda: maintenance.journal_state(world.store)).code == "busy"
        assert _refused(lambda: world.svc.open(world.pid)).code == "busy"
        err = _refused(
            lambda: world.svc.maintenance_switch(world.pid, AGENT, world.backup_name, target)
        )
        assert err.code == "busy" and world.svc.registry.maintenance_approvals(world.pid) == []
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_the_owner_never_migrates_a_store_with_a_hot_journal(world: World) -> None:
    _hot_journal(world)
    before = _files(world.store)
    err = _refused(lambda: world.svc.migrate(world.pid, authority=OWNER_ACT))
    assert err.code == "io_error" and err.details["store_health"] == "needs_journal_recovery"
    with running_hub(world.svc, world.pid) as hub:
        hub.pair()
        reply = hub.request("POST", "/api/v1/maintenance/migrate", {})
        assert reply.status == 503 and "recovery" in reply.json()["error"]["details"]
    assert _files(world.store) == before


# ----- F4: unreadable databases and scratch failures ------------------------------------------


@pytest.mark.skipif(not WINDOWS, reason="a mandatory byte-range lock needs Windows")
def test_an_unreadable_database_refuses_switch_with_a_typed_error(world: World) -> None:
    """Decision 0025: an existing writable handle refuses before unreadable/HOT diagnosis."""
    target = _restore(world)
    before = _files(world.store)
    db = world.store / "record.sqlite"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import msvcrt, os, sys, time\nfd = os.open(sys.argv[1], os.O_RDWR)\n"
         "msvcrt.locking(fd, msvcrt.LK_NBLCK, int(sys.argv[2]))\nprint('locked', flush=True)\n"
         "time.sleep(120)\n", str(db), str(db.stat().st_size)],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        err = _refused(lambda: world.svc.open(world.pid))
        assert err.code == "busy" and err.details.get("store_health") not in maintenance.DAMAGED
        assert err.details["recovery"]["hub"] == "/recovery"
        inspect_error = _refused(lambda: world.svc.maintenance_inspect(world.pid, AGENT))
        assert inspect_error.code == "busy"
        err = _refused(
            lambda: world.svc.maintenance_switch(world.pid, AGENT, world.backup_name, target)
        )
        assert err.code == "busy" and err.details.get("store_health") not in maintenance.DAMAGED
        digest_error = _refused(lambda: maintenance.db_digest(world.store))
        assert digest_error.code == "busy"
        assert world.svc.registry.maintenance_approvals(world.pid) == []
        assert world.registry_row()["store_path"] == str(world.store)
    finally:
        holder.kill()
        holder.wait()
        if holder.stdout is not None:
            holder.stdout.close()
    # Windows frees a killed process's byte-range lock asynchronously, so the first read can
    # still be refused (decision 0074; the papers-1 integration run hit it again).
    deadline = time.monotonic() + 10
    while True:
        try:
            after = _files(world.store)
            break
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    assert after == before


def test_a_scratch_failure_is_a_typed_error_not_an_unreadable_store(
    damaged: World, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    target = _restore(damaged)
    before = damaged.snapshot()
    registry_before = damaged.registry_row()
    approvals_before = damaged.svc.registry.maintenance_approvals(damaged.pid)
    quarantine_before = damaged.svc.registry.quarantine_records(damaged.pid)
    scratch = damaged.svc.home / "scratch"
    hits: list[Path] = []
    opened.clear()

    def destination_full(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        destination = Path(file) if isinstance(file, (str, Path)) else None
        if (
            mode == "xb"
            and destination is not None
            and destination.name == "record.sqlite"
            and destination.parent.name.startswith("inspect-")
            and destination.parent.is_relative_to(scratch)
        ):
            hits.append(destination)
            raise OSError(errno.ENOSPC, "No space left on device")
        return builtins.open(file, mode, *args, **kwargs)

    monkeypatch.setattr(maintenance, "open", destination_full, raising=False)
    maintenance._CACHE.clear()
    hit_count = len(hits)
    err = _refused(lambda: damaged.svc.maintenance_inspect(damaged.pid, AGENT))
    assert err.code == "io_error" and err.details == {"where": "scratch"}
    assert isinstance(err.__cause__, OSError) and err.__cause__.errno == errno.ENOSPC
    assert len(hits) > hit_count
    hit_count = len(hits)
    switch = _refused(
        lambda: damaged.svc.maintenance_switch(
            damaged.pid, AGENT, damaged.backup_name, target, owner=OWNER_ACT
        )
    )
    assert switch.code == "io_error" and switch.details == {"where": "scratch"}
    assert isinstance(switch.__cause__, OSError) and switch.__cause__.errno == errno.ENOSPC
    assert len(hits) > hit_count
    hit_count = len(hits)
    envelope = _tool_error(_run_mcp(damaged, lambda c: c.call_tool("maintenance_inspect", {})))
    assert envelope["code"] == "io_error" and envelope["details"] == {"where": "scratch"}
    assert len(hits) > hit_count
    with running_hub(damaged.svc, damaged.pid) as hub:
        hit_count = len(hits)
        reply = _agent(hub, "GET", "/api/v1/maintenance/inspect")
        error = reply.json()["error"]
        assert reply.status == 503 and error["code"] == "io_error"
        assert error["details"] == {"where": "scratch"}
        assert len(hits) > hit_count
    assert _inside(opened, damaged.store) == []
    assert damaged.snapshot() == before
    assert damaged.registry_row() == registry_before
    assert damaged.svc.registry.maintenance_approvals(damaged.pid) == approvals_before
    assert damaged.svc.registry.quarantine_records(damaged.pid) == quarantine_before


def test_file_system_errors_are_typed_on_mcp_and_http(
    damaged: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(errno.EACCES, "Permission denied", str(damaged.store))

    monkeypatch.setattr(type(damaged.svc), "maintenance_verify", fails)
    envelope = _tool_error(
        _run_mcp(damaged, lambda c: c.call_tool("maintenance_verify", {"backup": "x.zip"}))
    )
    assert envelope["code"] == "io_error" and str(damaged.store) not in json.dumps(envelope)
    with running_hub(damaged.svc, damaged.pid) as hub:
        reply = _agent(hub, "POST", "/api/v1/maintenance/verify", {"backup": "x.zip"})
        assert reply.status == 503 and reply.json()["error"]["code"] == "io_error"


# ----- F5: no side files ------------------------------------------------------------------------


def test_opening_a_damaged_wal_store_creates_no_side_file(world: World, opened: list[str]) -> None:
    db = world.store / "record.sqlite"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    before = _files(world.store)
    opened.clear()
    err = _refused(lambda: world.svc.open(world.pid))
    assert err.code == "unsupported" and err.details["store_health"] == "newer_schema"
    from prometheos.hub.app import create_app
    from prometheos.hub.security import Gate

    assert create_app(world.svc, world.pid, Gate(port=1)).state.maintenance is True
    assert world.svc.doctor(world.pid)["project"]["opens"] is False
    report = world.svc.maintenance_inspect(world.pid, AGENT)
    assert report["health"] == "newer_schema" and report["opens"] is False
    assert report["database"]["schema_version_source"] == "main_header_hint"
    assert report["database"]["integrity_checked"] is False
    assert _inside(opened, world.store) == []
    assert _files(world.store) == before


# ----- F6: maintenance mode opens no project database ------------------------------------------


def test_maintenance_mode_backups_and_owner_migrate_never_open_the_store(
    world: World, opened: list[str]
) -> None:
    world.damage("truncated")  # a valid header: an open attempt would reach the database
    damaged = world
    before = damaged.snapshot()
    with running_hub(damaged.svc, damaged.pid) as hub:
        opened.clear()
        hub.pair()
        for who in ("owner", "agent"):
            if who == "owner":
                reply = hub.request("POST", "/api/v1/maintenance/backups", {})
            else:
                reply = _agent(hub, "POST", "/api/v1/maintenance/backups", {})
            assert reply.status == 503 and "recovery" in reply.json()["error"]["details"]
        migrate = hub.request("POST", "/api/v1/maintenance/migrate", {})
        assert migrate.status == 503 and migrate.json()["error"]["details"]["store_health"] in (
            "corrupt",
        )
        assert _inside(opened, damaged.store) == []
    assert damaged.snapshot() == before


def test_owner_queue_polling_never_probes_the_database(
    damaged: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = _request_switch(damaged, _restore(damaged))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("request polling must not inspect or open the project")

    monkeypatch.setattr(maintenance, "probe_store", forbidden)
    with running_hub(damaged.svc, damaged.pid) as hub:
        monkeypatch.setattr(type(damaged.svc), "open", forbidden)
        hub.pair()
        reply = hub.request("GET", "/api/v1/maintenance/approvals")
        assert reply.status == 200
        assert reply.json()["approvals"][0]["approval_request_id"] == approval_id
        denied = _agent(hub, "GET", "/api/v1/maintenance/approvals")
        assert denied.status == 403


# ----- F7: owner restore into the lost store's own location -------------------------------------


def test_owner_restore_into_the_lost_store_location_registers_it(world: World) -> None:
    import shutil

    lost = world.store
    backup = world.tmp / "kept-backup.zip"
    shutil.copyfile(world.backup_path, backup)
    shutil.rmtree(lost)
    owner = type(FULL)(actor="cli-owner-restore", scope="owner", full_data=True)
    result = world.svc.restore(backup, lost, caller=owner)
    assert result["quarantine"] is None and result["store"] == str(lost)
    assert world.svc.open(world.pid).directory == lost
    assert world.svc.registry.quarantine_records(world.pid) == []


# ----- F8, F9: one location rule; labels over MCP -------------------------------------------------


def test_owner_restore_accepts_a_home_label_on_every_owner_route(damaged: World) -> None:
    done = damaged.svc.maintenance_restore(
        damaged.pid, damaged.backup_name, "<PrometheOS home>/owner-pick/one", owner=OWNER_ACT
    )
    assert done["target_store_label"] == "<PrometheOS home>/owner-pick/one"
    with running_hub(damaged.svc, damaged.pid) as hub:
        hub.pair()
        reply = hub.request(
            "POST", "/api/v1/maintenance/restore",
            {"backup": damaged.backup_name, "into": "<PrometheOS home>/owner-pick/two"},
        )  # fmt: skip
        assert reply.status == 200, reply.body
        assert reply.json()["target_store_label"] == "<PrometheOS home>/owner-pick/two"


def test_mcp_maintenance_replies_carry_no_local_path(damaged: World) -> None:
    target = _restore(damaged)
    approval_id = _request_switch(damaged, target)
    home = str(damaged.env.home)

    async def reads(client: Client) -> Any:
        return [
            await client.call_tool("maintenance_inspect", {}),
            await client.call_tool("maintenance_approval_get", {"approval_id": approval_id}),
            await client.call_tool("maintenance_verify", {"backup": damaged.backup_name}),
        ]

    for reply in _run_mcp(damaged, reads):
        text = json.dumps(reply.structured_content)
        assert not reply.is_error
        assert home not in text and home.replace("\\", "\\\\") not in text
        assert "<PrometheOS home>" in text or "backup" in text


# ----- F10: one test per binding layer (the reviewer's surviving mutants) ----------------------


def test_a_grant_needs_the_plan_digest_the_owner_was_shown(damaged: World) -> None:
    approval_id = _request_switch(damaged, _restore(damaged))
    err = _refused(
        lambda: damaged.svc.maintenance_decide(
            damaged.pid, approval_id, grant=True, plan_sha256="0" * 64, owner=OWNER_ACT
        )
    )
    assert err.code == "revision_conflict"
    row = damaged.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "pending"


@pytest.mark.parametrize("what", ["registry", "target"])
def test_the_registry_transaction_rechecks_the_registry_and_the_target(
    damaged: World, monkeypatch: pytest.MonkeyPatch, what: str
) -> None:
    target = _restore(damaged)
    approval_id = _request_switch(damaged, target)
    _grant(damaged, approval_id)
    real = maintenance.lock_state

    def race(store_dir: Path) -> str:  # runs after the plan was verified, before the txn
        if what == "registry":
            damaged.svc.registry.register(
                damaged.pid, damaged.tmp / "elsewhere", "x", "test", replace=True
            )
        else:
            with (Path(target) / "record.sqlite").open("ab") as handle:
                handle.write(b"\x00")
        return real(store_dir)

    monkeypatch.setattr(maintenance, "lock_state", race)
    err = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id))
    assert err.code == "revision_conflict"
    row = damaged.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "granted"
    assert damaged.svc.registry.quarantine_records(damaged.pid) == []


def test_a_granted_migration_rechecks_the_store_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_cp10_migrations import _schema6_env

    env = _schema6_env(tmp_path, monkeypatch)
    asked = _refused(lambda: env.svc.maintenance_migrate_request(env.project_id, AGENT))
    approval_id = asked.details["approval_request_id"]
    view = env.svc.maintenance_approval_get(env.project_id, OWNER, approval_id)
    env.svc.maintenance_decide(
        env.project_id, approval_id, grant=True, plan_sha256=view["plan_sha256"], owner=OWNER_ACT
    )
    conn = sqlite3.connect(env.store_dir / "record.sqlite", isolation_level=None)
    conn.execute("CREATE TABLE written_after_the_grant (x)")  # still needs migration
    conn.close()
    err = _refused(lambda: env.svc.maintenance_resume(env.project_id, AGENT, approval_id))
    assert err.code == "revision_conflict"
    row = env.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "granted"
    check = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] < len(MIGRATIONS)
    finally:
        check.close()


def test_a_request_about_to_expire_cannot_be_granted(
    damaged: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = _request_switch(damaged, _restore(damaged))
    row = damaged.svc.registry.maintenance_approval(approval_id)
    assert row is not None
    expires = datetime.strptime(row.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    monkeypatch.setattr(approvals, "_now", lambda: expires - timedelta(seconds=10))
    err = _refused(lambda: _grant(damaged, approval_id))
    assert err.code == "validation_error" and "expires too soon" in err.message


@pytest.mark.parametrize("change", ["same_size", "truncated"])
def test_restored_media_must_match_their_digests(damaged: World, change: str) -> None:
    target = Path(_restore(damaged))
    media = next(p for p in (target / "media").rglob("*") if p.is_file())
    data = media.read_bytes()
    media.write_bytes(bytes([data[0] ^ 0xFF]) + data[1:] if change == "same_size" else data[:-1])
    err = _refused(
        lambda: damaged.svc.maintenance_switch(
            damaged.pid, AGENT, damaged.backup_name, str(target), owner=OWNER_ACT
        )
    )
    assert err.code == "validation_error" and "media" in err.message
    assert damaged.registry_row()["store_path"] == str(damaged.store)


@pytest.mark.skipif(not WINDOWS, reason="junction regression needs Windows")
def test_a_managed_restore_cannot_link_media_outside_its_store(damaged: World) -> None:
    target = Path(_restore(damaged))
    outside = damaged.tmp / "outside-media"
    (target / "media").rename(outside)
    junction = target / "media"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        err = _refused(
            lambda: damaged.svc.maintenance_switch(
                damaged.pid, AGENT, damaged.backup_name, str(target)
            )
        )
        assert err.code == "validation_error" and "leaves its store" in err.message
        assert damaged.svc.registry.maintenance_approvals(damaged.pid) == []
    finally:
        junction.rmdir()


def test_only_the_requesting_rank_withdraws(damaged: World) -> None:
    approval_id = _request_switch(damaged, _restore(damaged))
    err = _refused(lambda: damaged.svc.maintenance_withdraw(damaged.pid, OWNER, approval_id))
    assert err.code == "request_key_conflict"
    row = damaged.svc.registry.maintenance_approval(approval_id)
    assert row is not None and row.state == "pending"
    assert damaged.svc.maintenance_withdraw(damaged.pid, AGENT, approval_id)["state"] == (
        "withdrawn"
    )


def _owner_route_handlers(source: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    from test_cp10_authority_r3 import _route_of

    found = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                route = _route_of(decorator)
                if route:
                    found[route] = node
    return found


def _calls_require_owner(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for stmt in node.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # the docstring
        return (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and getattr(stmt.value.func, "id", None) == "_require_owner"
        )
    return False


def test_every_owner_channel_route_checks_the_owner_itself() -> None:
    """MM7: the handler of every owner-channel HTTP route starts with ``_require_owner``, so
    the middleware's allow-list is not the only guard (review F14 of CP10.2)."""
    from prometheos import registry
    from prometheos.hub import app as hub_app

    source = Path(hub_app.__file__).read_text(encoding="utf-8")  # the module actually imported
    handlers = _owner_route_handlers(source)
    from prometheos.hub.app import AGENT_ROUTES

    # Routes an agent operation shares branch on the caller (checked by test_cp10_authority_r3);
    # every owner-only route must refuse anyone else by itself.
    routes = [
        r
        for o in registry.operations()
        if o.authority == "owner_channel"
        for r in o.http
        if r not in AGENT_ROUTES
    ]
    assert "POST /api/v1/maintenance/approvals/{approval_id}/grant" in routes
    missing = [r for r in routes if r in handlers and not _calls_require_owner(handlers[r])]
    assert missing == []
    mutated = source.replace(
        '        the grant is held in the registry and used once by the requester\'s resume."""\n'
        "        _require_owner(request)\n",
        '        the grant is held in the registry and used once by the requester\'s resume."""\n',
    )
    assert mutated != source
    handlers = _owner_route_handlers(mutated)
    assert not _calls_require_owner(
        handlers["POST /api/v1/maintenance/approvals/{approval_id}/grant"]
    )


# ----- F11: one probe per store state -----------------------------------------------------------


def test_inspect_probes_once_per_store_state_and_one_at_a_time(
    damaged: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance._CACHE.clear()
    real = maintenance.probe_store
    calls: list[float] = []

    def slow(*args: Any) -> dict[str, Any]:
        calls.append(time.monotonic())
        time.sleep(0.3)
        return real(*args)

    monkeypatch.setattr(maintenance, "probe_store", slow)
    threads = [
        threading.Thread(target=damaged.svc.maintenance_inspect, args=(damaged.pid, AGENT))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(calls) == 1  # concurrent polls wait for the one running probe
    damaged.svc.maintenance_inspect(damaged.pid, AGENT)
    assert len(calls) == 1  # unchanged store: the cached result
    with (damaged.store / "record.sqlite").open("ab") as handle:
        handle.write(b"\x00")
    damaged.svc.maintenance_inspect(damaged.pid, AGENT)
    assert len(calls) == 2  # a changed file is probed again


def test_inspect_serializes_a_probe_even_when_the_store_changes_during_it(
    damaged: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance._CACHE.clear()
    started, release = threading.Event(), threading.Event()
    real = maintenance.probe_store
    calls: list[int] = []
    active = 0
    peak = 0
    guard = threading.Lock()

    def slow(*args: Any) -> dict[str, Any]:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(10)
        try:
            return real(*args)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(maintenance, "probe_store", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(damaged.svc.maintenance_inspect, damaged.pid, AGENT)
        assert started.wait(10)
        with (damaged.store / "record.sqlite").open("ab") as handle:
            handle.write(b"changed")
        two = pool.submit(damaged.svc.maintenance_inspect, damaged.pid, AGENT)
        release.set()
        first = one.result(20)
        second = two.result(20)
    assert peak == 1 and len(calls) == 2
    assert first["database"]["sha256"] == second["database"]["sha256"]
    first["database"]["integrity"] = "invented"
    assert damaged.svc.maintenance_inspect(damaged.pid, AGENT)["database"]["integrity"] == "corrupt"


@pytest.mark.parametrize("transport", ["service", "mcp", "http"])
def test_explicit_refresh_retries_health_when_file_metadata_is_unchanged(
    world: World, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    key = maintenance._signature(world.store)
    monkeypatch.setattr(maintenance, "_signature", lambda store: key)
    assert world.svc.maintenance_inspect(world.pid, AGENT)["health"] == "ok"
    world.damage("garbage")
    assert world.svc.maintenance_inspect(world.pid, AGENT)["health"] == "ok"
    if transport == "service":
        result = world.svc.maintenance_inspect(world.pid, AGENT, refresh=True)
    elif transport == "mcp":
        reply = _run_mcp(world, lambda c: c.call_tool("maintenance_inspect", {"refresh": True}))
        assert not reply.is_error
        result = reply.structured_content
    else:
        with running_hub(world.svc, world.pid) as hub:
            reply = _agent(hub, "GET", "/api/v1/maintenance/inspect?refresh=true")
            assert reply.status == 200
            result = reply.json()
    assert result["health"] == "corrupt"
    assert world.svc.maintenance_inspect(world.pid, AGENT)["health"] == "corrupt"


def test_inspect_does_not_cache_an_unreadable_result(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = maintenance.probe_store
    count = 0

    def inaccessible_once(*args: Any) -> dict[str, Any]:
        nonlocal count
        count += 1
        if count == 1:
            return {"health": "unreadable", "opens": False, "database": {"integrity": "unreadable"}}
        return real(*args)

    monkeypatch.setattr(maintenance, "probe_store", inaccessible_once)
    assert world.svc.maintenance_inspect(world.pid, AGENT)["health"] == "unreadable"
    assert world.svc.maintenance_inspect(world.pid, AGENT)["health"] == "ok"
    assert count == 2


def test_cli_explicit_refresh_accepts_the_same_switch_label_rule(damaged: World) -> None:
    from test_cp1_cli import Cli

    code, report, _ = Cli(damaged.tmp)(
        "maintenance", "inspect", "--refresh", "--project", damaged.pid
    )
    assert code == 0 and report["health"] == "corrupt"


# ----- helpers ------------------------------------------------------------------------------------


def _run_mcp(world: World, steps: Any) -> Any:
    async def run() -> Any:
        async with Client(build_maintenance_server(world.svc, world.pid)) as client:
            return await steps(client)

    return anyio.run(run)


def test_reading_a_wal_store_identity_creates_no_side_file(world: World, opened: list[str]) -> None:
    """Decision 0025: unavailable WAL identity still exposes recovery without source opens."""
    conn = sqlite3.connect(world.store / "record.sqlite", isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.close()
    before = _files(world.store)
    opened.clear()
    err = _refused(lambda: world.svc.open(world.pid))
    assert err.code == "unsupported" and err.details["store_health"] == "unsupported_journal_mode"
    assert err.details["recovery"]["hub"] == "/recovery"
    from prometheos.hub.app import create_app
    from prometheos.hub.security import Gate

    assert create_app(world.svc, world.pid, Gate(port=1)).state.maintenance is True
    assert world.svc.doctor(world.pid)["project"]["opens"] is False
    report = world.svc.maintenance_inspect(world.pid, AGENT)
    assert report["health"] == "unsupported_journal_mode" and report["opens"] is False
    assert report["database"]["schema_version"] is None
    assert report["database"]["integrity_checked"] is False
    assert world.svc.maintenance_inspect(world.pid, AGENT) == report  # cached diagnosis, no grant
    assert _inside(opened, world.store) == []
    assert _files(world.store) == before


def test_a_copy_failure_names_the_side_that_failed(tmp_path: Path) -> None:
    """F4: a source that cannot be read is the store's problem; a scratch folder that cannot
    take the copy is a typed scratch error (never 'the store is unreadable')."""
    source = tmp_path / "record.sqlite"
    source.write_bytes(b"x" * 10)
    with pytest.raises(maintenance._SourceUnreadable):
        maintenance._copy_file(tmp_path / "absent.sqlite", tmp_path / "copy")
    with pytest.raises(maintenance.PrometheosError) as err:
        maintenance._copy_file(source, tmp_path / "no-such-folder" / "copy")
    assert err.value.code == "io_error" and err.value.details == {"where": "scratch"}
