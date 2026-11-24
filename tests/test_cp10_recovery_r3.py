"""Regressions for the four independently reproduced CP10.5 round-2 defects.

All stores, redirected directories and owner confirmation recorders are synthetic.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, create_entry
from hub_support import running_hub
from test_cp10_recovery import (
    World,
    _agent,
    _grant,
    _refused,
    _request_switch,
    _restore,
    _tool_error,
)
from test_cp10_recovery_r2 import _files, _hot_journal, _run_mcp
from typer.testing import CliRunner

from prometheos import cli
from prometheos.app import maintenance
from prometheos.app.service import Service
from prometheos.domain.models import PrometheosError

WINDOWS = sys.platform == "win32"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def damaged(world: World) -> World:
    world.damage("garbage")
    return world


def _junction(link: Path, destination: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(destination)],
        check=True,
        capture_output=True,
    )


def _unchanged(world: World, source: Any, registry: Any, approval_id: str | None = None) -> None:
    assert world.snapshot() == source
    assert world.registry_row() == registry
    assert world.svc.registry.quarantine_records(world.pid) == []
    if approval_id is not None:
        row = world.svc.registry.maintenance_approval(approval_id)
        assert row is not None and row.state == "granted"
    else:
        assert world.svc.registry.maintenance_approvals(world.pid) == []


@pytest.mark.skipif(not WINDOWS, reason="junction containment needs Windows")
@pytest.mark.parametrize("transport", ["service", "mcp", "http"])
def test_journal_recovery_refuses_an_escaping_backups_child(world: World, transport: str) -> None:
    _hot_journal(world)
    source, registry = world.snapshot(), world.registry_row()
    managed = maintenance.Maintenance(world.svc).managed_dir(world.pid)
    managed.mkdir(parents=True)
    outside = world.tmp / "outside-home"
    outside.mkdir()
    link = managed / "backups"
    _junction(link, outside)
    try:
        if transport == "service":
            error = _refused(lambda: world.svc.maintenance_journal_recover(world.pid))
            assert error.code == "policy_denied"
        elif transport == "mcp":
            error_view = _tool_error(
                _run_mcp(world, lambda c: c.call_tool("maintenance_journal_recover", {}))
            )
            assert error_view["code"] == "policy_denied"
        else:
            with running_hub(world.svc, world.pid) as hub:
                reply = _agent(hub, "POST", "/api/v1/maintenance/journal-recovery", {})
                assert reply.status == 403 and reply.json()["error"]["code"] == "policy_denied"
        assert list(outside.iterdir()) == []
        assert not list(managed.glob(".journal-*"))
        _unchanged(world, source, registry)
    finally:
        link.rmdir()


@pytest.mark.skipif(not WINDOWS, reason="junction containment needs Windows")
@pytest.mark.parametrize("destination", ["archive", "part", "staging"])
def test_journal_recovery_checks_its_archive_part_and_staging_destinations(
    world: World, monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    _hot_journal(world)
    source, registry = world.snapshot(), world.registry_row()
    managed = maintenance.Maintenance(world.svc).managed_dir(world.pid)
    (managed / "backups").mkdir(parents=True)
    outside = world.tmp / "outside-home"
    outside.mkdir()

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 10, 8, tzinfo=UTC)

    fixed_uuid = uuid.UUID("12345678-1234-1234-1234-123456789abc")
    monkeypatch.setattr(maintenance, "datetime", FixedTime)
    monkeypatch.setattr(maintenance.uuid, "uuid4", lambda: fixed_uuid)
    archive = managed / "backups" / "journal-recovered-20261008T000000-123456.zip"
    link = {
        "archive": archive,
        "part": archive.with_name(archive.name + ".part"),
        "staging": managed / ".journal-12345678",
    }[destination]
    _junction(link, outside)
    try:
        error = _refused(lambda: world.svc.maintenance_journal_recover(world.pid))
        assert error.code == "policy_denied"
        assert list(outside.iterdir()) == []
        _unchanged(world, source, registry)
    finally:
        link.rmdir()


@pytest.mark.skipif(not WINDOWS, reason="junction containment needs Windows")
def test_recovered_backup_resolution_and_listing_refuse_an_escaping_child(world: World) -> None:
    managed = maintenance.Maintenance(world.svc).managed_dir(world.pid)
    managed.mkdir(parents=True)
    outside = world.tmp / "outside-home"
    outside.mkdir()
    name = "journal-recovered-kept.zip"
    (outside / name).write_bytes(world.backup_path.read_bytes())
    before = _files(outside)
    link = managed / "backups"
    _junction(link, outside)
    try:
        for action in (
            lambda: world.svc.maintenance_verify(world.pid, name),
            lambda: world.svc.maintenance_inspect(world.pid, AGENT),
        ):
            error = _refused(action)
            assert error.code == "policy_denied"
        assert _files(outside) == before
    finally:
        link.rmdir()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("when", ["request", "resume", "commit"])
def test_a_switch_refuses_unverified_target_sqlite_side_files(
    damaged: World, monkeypatch: pytest.MonkeyPatch, suffix: str, when: str
) -> None:
    target = Path(_restore(damaged))
    source, registry = damaged.snapshot(), damaged.registry_row()
    approval_id = None
    if when != "request":
        approval_id = _request_switch(damaged, str(target))
        _grant(damaged, approval_id)
    side_file = target / ("record.sqlite" + suffix)
    data = maintenance.JOURNAL_MAGIC + b"\x00" * 600
    if when == "commit":
        real = maintenance.lock_state

        def race(store_dir: Path) -> str:
            side_file.write_bytes(data)
            return real(store_dir)

        monkeypatch.setattr(maintenance, "lock_state", race)
    else:
        side_file.write_bytes(data)
    if approval_id is None:
        error = _refused(
            lambda: damaged.svc.maintenance_switch(
                damaged.pid, AGENT, damaged.backup_name, str(target)
            )
        )
    else:
        error = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id))
    assert error.code == ("revision_conflict" if when == "commit" else "validation_error")
    assert side_file.read_bytes() == data
    _unchanged(damaged, source, registry, approval_id)


@pytest.mark.parametrize("change", ["same_size", "truncated", "missing"])
def test_the_switch_transaction_rechecks_retained_media(
    damaged: World, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    target = Path(_restore(damaged))
    approval_id = _request_switch(damaged, str(target))
    _grant(damaged, approval_id)
    source, registry = damaged.snapshot(), damaged.registry_row()
    media = next(p for p in (target / "media").rglob("*") if p.is_file())
    data = media.read_bytes()
    real = maintenance.lock_state

    def race(store_dir: Path) -> str:
        if change == "missing":
            media.unlink()
        else:
            media.write_bytes(
                bytes([data[0] ^ 255]) + data[1:] if change == "same_size" else data[:-1]
            )
        return real(store_dir)

    monkeypatch.setattr(maintenance, "lock_state", race)
    error = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id))
    assert error.code == "revision_conflict"
    _unchanged(damaged, source, registry, approval_id)


@pytest.mark.skipif(not WINDOWS, reason="junction containment needs Windows")
@pytest.mark.parametrize("redirect", ["media", "store"])
def test_the_switch_transaction_rechecks_target_containment(
    damaged: World, monkeypatch: pytest.MonkeyPatch, redirect: str
) -> None:
    target = Path(_restore(damaged))
    approval_id = _request_switch(damaged, str(target))
    _grant(damaged, approval_id)
    source, registry = damaged.snapshot(), damaged.registry_row()
    link = target / "media" if redirect == "media" else target
    outside = damaged.tmp / "outside-target"
    real = maintenance.lock_state

    def race(store_dir: Path) -> str:
        link.rename(outside)
        _junction(link, outside)
        return real(store_dir)

    monkeypatch.setattr(maintenance, "lock_state", race)
    try:
        error = _refused(lambda: damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id))
        assert error.code == "revision_conflict"
        _unchanged(damaged, source, registry, approval_id)
    finally:
        if link.is_junction():
            link.rmdir()
            outside.rename(link)


def test_an_unchanged_verified_target_opens_after_switch(damaged: World) -> None:
    target = Path(_restore(damaged))
    approval_id = _request_switch(damaged, str(target))
    _grant(damaged, approval_id)
    result = damaged.svc.maintenance_resume(damaged.pid, AGENT, approval_id)
    assert result["result"]["switched"] is True
    assert damaged.svc.open(damaged.pid).directory == target
    assert damaged.svc.maintenance_approval_get(damaged.pid, OWNER, approval_id)["state"] == "used"


@pytest.mark.parametrize("code", ["io_error", "busy", "not_found"])
def test_cli_restore_probe_failures_never_prompt_or_roll_back(
    world: World, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    world.env.apply([create_entry(title="work after the backup")])
    source, registry, seq = world.snapshot(), world.registry_row(), world.env.seq()
    target = world.tmp / "owner-restored"
    prompts: list[Any] = []
    monkeypatch.setattr(cli, "_service", lambda: world.svc)
    monkeypatch.setattr(cli, "_confirm_owner", lambda *args, **kwargs: prompts.append(args))

    def failure(*args: Any, **kwargs: Any) -> Any:
        raise PrometheosError(code, "synthetic health preview failure", {"where": "scratch"})

    monkeypatch.setattr(world.svc, "maintenance_inspect", failure)
    result = CliRunner().invoke(
        cli.app, ["backup", "restore", str(world.backup_path), "--into", str(target), "--json"]
    )
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error"]["code"] == code
    assert prompts == [] and not target.exists()
    assert world.env.seq() == seq
    _unchanged(world, source, registry)


def test_cli_restore_into_an_unregistered_home_still_confirms_and_registers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = Service(world.tmp / "fresh-home")
    target = svc.home / "restored"
    prompts: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "_service", lambda: svc)
    monkeypatch.setattr(
        cli,
        "_confirm_owner",
        lambda summary, **kwargs: prompts.append((summary, kwargs["purpose"])),
    )
    result = CliRunner().invoke(
        cli.app, ["backup", "restore", str(world.backup_path), "--into", str(target), "--json"]
    )
    assert result.exit_code == 0, result.stdout
    assert len(prompts) == 1 and "ROLLBACK" not in prompts[0][0]
    assert prompts[0][1] == "To approve exactly this"
    assert svc.open(world.pid).directory == target
    assert svc.registry.quarantine_records(world.pid) == []


def test_cli_rollback_preview_refreshes_cached_health(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.svc.maintenance_inspect(world.pid, AGENT)  # cache the pre-edit sequence
    world.env.apply([create_entry(title="work after the cached preview")])
    manifest = world.svc.verify_backup(world.backup_path)["manifest"]
    real = world.svc.maintenance_inspect
    refreshes: list[bool] = []

    def inspect(*args: Any, **kwargs: Any) -> Any:
        refreshes.append(kwargs.get("refresh", False))
        return real(*args, **kwargs)

    monkeypatch.setattr(world.svc, "maintenance_inspect", inspect)
    summary, purpose = cli._restore_summary(world.svc, world.backup_path, world.tmp / "x", manifest)
    assert refreshes == [True]
    assert "ROLLBACK" in summary and f"at sequence {world.env.seq()}" in summary
    assert purpose == "To roll the project back to this backup"
