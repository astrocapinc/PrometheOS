"""The two accepted CP10 tracks preserve no-open maintenance and path-safe transports."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import anyio
import httpx2 as httpx  # the runtime's client; httpx itself is audio-extra only (decision 0072)
import pytest
from cp1_support import Env
from mcp import Client

from prometheos.app.pathscrub import build_redactor
from prometheos.domain.models import PrometheosError
from prometheos.hub.app import create_app
from prometheos.hub.security import CSRF_HEADER, SESSION_COOKIE, Gate
from prometheos.mcp_adapter import build_maintenance_server, build_server


def _never(counter: list[str], name: str) -> Any:
    def refused(*args: Any, **kwargs: Any) -> Any:
        counter.append(name)
        raise AssertionError(f"maintenance attempted {name}")

    return refused


def test_maintenance_redactor_uses_registry_without_open_or_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(env.svc, "open", _never(calls, "open"))
    monkeypatch.setattr(env.svc, "project_info", _never(calls, "project_info"))
    redactor = build_redactor(env.svc, env.project_id, maintenance=True)
    path = str(env.store_dir / "raw-canary.sqlite")
    shown = redactor.body({"error": {"message": path, "details": {"path": path}}}, envelope=True)
    assert shown["error"]["message"].startswith("<project store>/")
    assert str(env.store_dir) not in json.dumps(shown)
    outside = "Q:/synthetic-unmanaged-root/raw-path-canary.sqlite"
    diagnostic = redactor.body({"error": {"message": "read failed at " + outside}}, envelope=True)
    assert outside not in diagnostic["error"]["message"]
    assert calls == []


@pytest.mark.parametrize("mutation", ["none", "missing", "extra", "wrong_type"])
def test_restored_project_quarantine_has_a_closed_path_safe_mcp_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from cp1_support import OWNER, OWNER_ACT
    from test_cp10_recovery import World, _restore

    world = World(tmp_path)
    world.damage("garbage")
    target = _restore(world)
    world.svc.maintenance_switch(world.pid, OWNER, world.backup_name, target, owner=OWNER_ACT)
    payload = world.svc.list_projects()
    nested = payload[0]["quarantined_stores"][0]
    assert nested["damaged_store_path"] == str(world.store)
    if mutation == "missing":
        nested.pop("damaged_integrity")
    elif mutation == "extra":
        nested["unexpected"] = "cannot-widen-fixed-schema"
    elif mutation == "wrong_type":
        nested["damaged_integrity"] = 42
    monkeypatch.setattr(world.svc, "list_projects", lambda: payload)

    async def run() -> Any:
        async with Client(build_server(world.svc, world.pid)) as client:
            return await client.call_tool("administration_read", {"request": {"op": "projects"}})

    reply = anyio.run(run)
    if mutation == "none":
        assert not reply.is_error
        shown = reply.structured_content["result"]["projects"][0]["quarantined_stores"][0]
        assert shown["quarantine_id"] == nested["quarantine_id"]
        assert shown["damaged_integrity"] == nested["damaged_integrity"]
        assert shown["damaged_store_path"].startswith("<PrometheOS home>/")
        assert str(world.env.home) not in json.dumps(shown).replace("\\\\", "\\")
    else:
        assert reply.is_error


def test_checkpoint_runner_stops_after_failed_environment_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import subprocess

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "integration_runner", root / "tools/run_checkpoint.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "tools").mkdir()
    suite = {
        "suites": {"CP10": {"C10-WALK": {"summary": "stub", "commands": {"walk": ["never-run"]}}}}
    }
    (tmp_path / "tools/checkpoint_suites.json").write_text(json.dumps(suite), encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module.tree_manifest, "write_manifest", lambda path, root=None: "stub-tree")
    monkeypatch.setattr(module.tree_manifest, "tree_sha256", lambda root=None: "stub-tree")
    seen: list[list[str]] = []

    def fail_env(argv: list[str], **kwargs: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(module.subprocess, "run", fail_env)
    assert module.main(["CP10", "environment-failure"]) == 1
    assert len(seen) == 1 and "env" in seen[0]
    assert ["--offline", "--no-sync"] == seen[0][-4:-2]
    assert not (
        tmp_path / "development/receipts/CP10/environment-failure/receipt-plan.json"
    ).exists()


def test_browser_unavailable_model_fixture_keeps_an_explicit_safe_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    import hub_fixture

    from prometheos.audio import models

    env = Env(tmp_path)
    staged = tmp_path / "present-synthetic-models"
    monkeypatch.setenv(models.MODELS_DIR_ENV, str(staged))
    spec = replace(models.FASTER_WHISPER_BASE, files=())  # presence-only test; no engine call
    monkeypatch.setattr(models, "KNOWN", {spec.key: spec})
    target = models.target_dir(env.home, spec)
    target.mkdir(parents=True)
    (target / models.MANIFEST).write_text('{"synthetic_test":true}', encoding="utf-8")
    assert env.svc.audio_models_status()["models"][0]["installed"] is True
    empty = hub_fixture.unavailable_models(tmp_path / "browser-fixture")
    assert env.svc.audio_models_status()["models"][0]["installed"] is False
    assert empty.is_dir() and list(empty.iterdir()) == []
    assert models.target_dir(env.home, models.spec_for(models.DEFAULT_MODEL)).is_relative_to(empty)
    (empty / "existing-data").write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        hub_fixture.unavailable_models(tmp_path / "browser-fixture")
    assert (empty / "existing-data").read_text(encoding="utf-8") == "preserve"
    monkeypatch.setenv(models.MODELS_DIR_ENV, str(staged))
    assert env.svc.audio_models_status()["models"][0]["installed"] is True


@pytest.mark.parametrize("transport", ["mcp", "http"])
def test_allowed_managed_restore_and_damaged_switch_request_hide_absolute_roots(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    from cp1_support import AGENT, FULL, OWNER, create_entry
    from test_cp10_recovery import World

    from prometheos.app import maintenance

    # Keep SQLite's Windows pathname below its limit while retaining a marked absolute root.
    marked = tmp_path_factory.mktemp("absolute-root-canary")
    world = World(marked)
    restricted = create_entry(sensitivity="restricted", title="INTEGRATION-RESTORE-CANARY")
    world.env.apply([restricted], caller=OWNER)
    world.backup_name = Path(world.svc.backup(world.pid, None, FULL)["backup"]).name
    world.damage("garbage")
    before = world.snapshot()
    registry_before = world.registry_row()
    calls: list[str] = []
    monkeypatch.setattr(world.svc, "open", _never(calls, "open"))
    monkeypatch.setattr(world.svc, "project_info", _never(calls, "project_info"))

    async def run() -> tuple[Any, Any, Any]:
        if transport == "mcp":
            async with Client(build_maintenance_server(world.svc, world.pid)) as client:
                restored = await client.call_tool(
                    "maintenance_restore", {"backup": world.backup_name}
                )
                assert not restored.is_error, restored.content
                shown = restored.structured_content
                request = await client.call_tool(
                    "maintenance_switch",
                    {
                        "backup": world.backup_name,
                        "target": shown["target_store_label"],
                    },
                )
                assert request.is_error
                text = request.content[0].text
                error = json.loads(text[text.index("{") :])["error"]
                approval = await client.call_tool(
                    "maintenance_approval_get",
                    {
                        "approval_id": error["details"]["approval_request_id"],
                    },
                )
                assert not approval.is_error
                return shown, error, approval.structured_content
        gate = Gate(port=1)
        app = create_app(world.svc, world.pid, gate, maintenance=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:1",
            headers={"Authorization": f"Bearer {gate.new_agent_token()}"},
        ) as client:
            restored = await client.post(
                "/api/v1/maintenance/restore", json={"backup": world.backup_name}
            )
            assert restored.status_code == 200, restored.text
            shown = restored.json()
            asked = await client.post(
                "/api/v1/maintenance/switch",
                json={
                    "backup": world.backup_name,
                    "target": shown["target_store_label"],
                },
            )
            error = asked.json()["error"]
            assert error["code"] == "approval_required"
            approval = await client.get(
                "/api/v1/maintenance/approvals/" + error["details"]["approval_request_id"]
            )
            assert approval.status_code == 200
            return shown, error, approval.json()

    restored, error, approval = anyio.run(run)
    assert error["code"] == "approval_required"
    assert calls == [] and world.snapshot() == before
    assert world.registry_row() == registry_before
    target = maintenance.Maintenance(world.svc).resolve_location(restored["target_store_label"])
    assert target.is_relative_to(world.env.home) and target != world.store
    with closing(
        sqlite3.connect(f"file:{(target / 'record.sqlite').as_posix()}?mode=ro", uri=True)
    ) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        record = conn.execute(
            "SELECT r.sensitivity, b.encoded FROM records r "
            "JOIN revisions v ON v.record_id=r.id AND v.rev=r.current_rev "
            "JOIN bodies b ON b.id=v.body_id WHERE r.id=?",
            (restricted["id"],),
        ).fetchone()
        assert record[0] == "restricted" and "INTEGRATION-RESTORE-CANARY" in record[1]
    request_id = error["details"]["approval_request_id"]
    raw = world.svc.maintenance_approval_get(world.pid, AGENT, request_id)
    assert raw["plan_sha256"] == approval["plan_sha256"]
    assert raw["state"] == "pending"
    for value in (restored, error, approval):
        assert str(world.env.home) not in json.dumps(value).replace("\\\\", "\\")
        assert "INTEGRATION-RESTORE-CANARY" not in json.dumps(value)


@pytest.mark.parametrize("maintenance_mode", [False, True])
@pytest.mark.parametrize("kind", ["generic", "typed", "sqlite", "os"])
def test_both_mcp_servers_scrub_maintenance_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maintenance_mode: bool, kind: str
) -> None:
    env = Env(tmp_path)
    raw = str(env.store_dir / "raw-canary.sqlite")
    calls: list[str] = []
    if maintenance_mode:
        monkeypatch.setattr(env.svc, "open", _never(calls, "open"))
        monkeypatch.setattr(env.svc, "project_info", _never(calls, "project_info"))
    errors = {
        "generic": RuntimeError(raw),
        "typed": PrometheosError("io_error", raw, {"path": raw}),
        "sqlite": sqlite3.DatabaseError(raw),
        "os": OSError(5, raw, raw),
    }

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise errors[kind]

    monkeypatch.setattr(env.svc, "maintenance_inspect", broken)

    async def run() -> Any:
        builder = build_maintenance_server if maintenance_mode else build_server
        async with Client(builder(env.svc, env.project_id)) as client:
            return await client.call_tool("maintenance_inspect", {})

    reply = anyio.run(run)
    assert reply.is_error
    text = reply.content[0].text
    error = json.loads(text[text.index("{") :])["error"]
    assert error["code"] == "io_error"
    unescaped = json.dumps(error).replace("\\\\", "\\")
    assert raw not in unescaped and str(env.home) not in unescaped
    if kind == "generic":
        assert error["message"] == "internal error in the MCP server"
    if kind == "sqlite":
        assert "recovery" in error["details"]
    assert calls == []


@pytest.mark.parametrize("principal", ["agent", "owner"])
@pytest.mark.parametrize(
    ("method", "route"),
    [("GET", "/api/v1/maintenance/orphans"), ("POST", "/api/v1/maintenance/backups")],
)
def test_maintenance_refuses_store_opening_routes_before_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, principal: str, method: str, route: str
) -> None:
    env = Env(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(env.svc, "open", _never(calls, "open"))
    monkeypatch.setattr(env.svc, "project_info", _never(calls, "project_info"))
    gate = Gate(port=1)
    app = create_app(env.svc, env.project_id, gate, maintenance=True)
    headers = {"Origin": "http://127.0.0.1:1"}
    cookies: dict[str, str] = {}
    if principal == "agent":
        headers["Authorization"] = f"Bearer {gate.new_agent_token()}"
    else:
        session = gate.pair(gate.new_pairing_token())
        assert session is not None
        cookies[SESSION_COOKIE], headers[CSRF_HEADER] = session

    async def run() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:1",
            headers=headers,
            cookies=cookies,
        ) as client:
            return await client.request(method, route, json={})

    reply = anyio.run(run)
    if principal == "agent" and route.endswith("/orphans"):
        assert reply.status_code == 403  # owner-only before maintenance admission
    else:
        assert reply.status_code == 503
        assert "recovery" in reply.json()["error"]["details"]
    assert calls == []
