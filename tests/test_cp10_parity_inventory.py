"""Bidirectional parity inventory (C10-PARITY, spec/04; decision 0015).

Forward: every registry operation has every route its authority requires (CLI, MCP and
local HTTP, or owner routes only for owner-channel operations), or a recorded gap owned by
a CP10 task. Reverse: every CLI command, MCP tool or resource and HTTP route the built
application exposes maps to a registry operation or to the reviewed exemption list. The
merged gap budget is zero and rejects independently injected gaps. Capability discovery
answers the same on all three transports.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import Env
from hub_support import running_hub
from mcp import Client
from test_cp1_cli import Cli

from prometheos import registry
from prometheos.mcp_adapter import build_server
from prometheos.registry import inventory

REPO = Path(__file__).resolve().parents[1]

# The accepted recovery and walk tracks close every recorded gap (decision 0024).
EXPECTED_GAPS: frozenset[tuple[str, str, str]] = frozenset()

EXPECTED_EXEMPTIONS = {
    "cli": {"mcp serve", "hub serve", "start", "stop", "status", "--help", "--version"},
    "mcp": set(),
    "http": {
        "POST /api/v1/pair",
        "GET /api/v1/session",
        "POST /api/v1/logout",
        "POST /api/v1/device/resume",
        "GET /api/v1/changes",
        "POST /api/v1/launcher/code",
        "POST /api/v1/launcher/stop",
        "GET /{path:path}",
        "GET /sw.js",
    },
}


@pytest.fixture(scope="module")
def found() -> inventory.Surfaces:
    return inventory.surfaces()


@pytest.fixture(scope="module")
def report(found: inventory.Surfaces) -> dict[str, Any]:
    return inventory.compare(registry.operations(), found, REPO)


def test_registry_declarations_are_consistent() -> None:
    assert registry.validate(registry.operations()) == []


def test_forward_every_operation_has_its_routes_or_a_recorded_gap(report: dict[str, Any]) -> None:
    assert report["forward_problems"] == []


def test_reverse_every_exposed_entry_point_is_registered_or_exempt(report: dict[str, Any]) -> None:
    assert report["reverse_unmapped"] == {"cli": [], "mcp": [], "http": []}
    # The walk really saw the application (not an empty tree).
    assert len(report["exposed"]["cli"]) > 100 and "paper overleaf push" in report["exposed"]["cli"]
    assert "capabilities" in report["exposed"]["mcp"]
    assert "GET /api/v1/capabilities" in report["exposed"]["http"]


def test_exemption_list_is_the_reviewed_one() -> None:
    # Changing the exemption list is a reviewed change: bump REGISTRY_VERSION with it.
    # Version 2 (round 2, R5): exactly spec/04's list; the OpenAPI route is not served.
    # Version 4 (A07, CP21; decision 0088): the launcher's transport commands and routes.
    # Version 5 (A07, CP21.2; decision 0089): the remembered device's session route.
    # Version 6 (A07, CP21.3; decision 0090): the PWA's service worker, a static asset.
    assert registry.REGISTRY_VERSION == 6
    assert {k: set(v) for k, v in registry.EXEMPTIONS.items()} == EXPECTED_EXEMPTIONS


def test_owner_channel_operations_are_owner_routes_only(found: inventory.Surfaces) -> None:
    owner_ops = [o for o in registry.operations() if o.authority == "owner_channel"]
    assert {o.id for o in owner_ops} >= {
        "decision_apply",
        "decision_grant",
        "decision_revoke",
        "approval_deny",
        "overleaf_push_grant",
        "provider_approve",
        "provider_secret_set",
        "provider_secret_delete",
    }
    for o in owner_ops:
        assert o.mcp == () and o.gap("mcp") is None, o.id
        assert o.required() == (("cli",) if o.no_http else ("cli", "http"))
        if o.no_http:
            assert o.id == "provider_secret_set" and "spec/08" in o.no_http
    owner_tools = {t for o in owner_ops for t in o.mcp}
    assert not owner_tools & set(found.mcp)
    # CP10.2 (decision 0016): no owner-channel HTTP route is reachable with an agent token.
    from prometheos.hub.app import AGENT_ROUTES

    owner_http = {r for o in owner_ops for r in o.http}
    agent_owned = {
        r for o in registry.operations() if o.authority != "owner_channel" for r in o.http
    }
    assert not (owner_http - agent_owned) & AGENT_ROUTES
    assert "GET /api/v1/changes" not in AGENT_ROUTES  # decision 0011 R2-L1
    assert AGENT_ROUTES <= set(found.http)


def test_cp9_and_cp6_surfaces_are_registered() -> None:
    ops = registry.operations()
    services = {name for o in ops for name in o.service}
    tools = {t for o in ops for t in o.mcp}
    # Decision 0012: the 22 CP9 service operations.
    cp9 = {
        "tex_engine_status", "tex_engine_plan", "tex_engine_install", "workspace_bind",
        "workspace_files", "workspace_read", "workspace_write", "workspace_snapshot",
        "workspace_structure", "workspace_insert", "paper_compile", "paper_compile_status",
        "synctex_forward", "synctex_inverse", "overleaf_link", "overleaf_status",
        "overleaf_pull_preview", "overleaf_pull_apply", "overleaf_commit",
        "overleaf_push_preview", "overleaf_push_grant", "overleaf_push",
    }  # fmt: skip
    assert cp9 <= services
    # Decision 0013: the 12 CP6 MCP tools.
    cp6 = {
        "audio_import", "audio_transcribe", "audio_transcription_status",
        "audio_transcription_cancel", "transcript_get", "audio_span", "transcript_correct",
        "meeting_from_audio", "audio_models_plan", "audio_models_status", "provider_status",
        "provider_preview",
    }  # fmt: skip
    assert cp6 <= tools
    administration = {
        "backup_create", "backup_verify", "backup_restore", "maintenance_migrate", "doctor",
        "retention_purge_plan", "retention_purge_commit", "decision_grant", "meeting_capture",
        "context_agenda", "skill_install", "workflow_start", "job_add", "paper_check",
    }  # fmt: skip
    assert administration <= {o.id for o in ops}


def test_gap_set_only_shrinks(report: dict[str, Any]) -> None:
    found = {(g["operation"], g["kind"], g["owner"]) for g in report["gaps"]}
    added = sorted(found - EXPECTED_GAPS)
    assert not added, f"gaps not in the pinned set (close them or review the change): {added}"
    # Round 3 (N2): equality, so a closed gap must also leave the pin and cannot return.
    closed = sorted(EXPECTED_GAPS - found)
    assert not closed, f"closed gaps still pinned (delete these lines from the pin): {closed}"
    assert len(found) == len(report["gaps"])  # one gap per (operation, kind)


def test_an_injected_gap_cannot_pass_the_zero_budget(found: inventory.Surfaces) -> None:
    ops = list(registry.operations())
    index = next(i for i, op in enumerate(ops) if op.id == "capabilities")
    ops[index] = dataclasses.replace(
        ops[index], mcp=(), gaps=(registry.Gap("mcp", "CP10.6", "injected missing route"),)
    )
    broken = inventory.compare(tuple(ops), found, REPO)
    assert broken["gaps"] == [
        {
            "operation": "capabilities",
            "kind": "mcp",
            "owner": "CP10.6",
            "note": "injected missing route",
        }
    ]
    with pytest.raises(AssertionError, match="gaps not in the pinned set"):
        test_gap_set_only_shrinks(broken)
    assert "capabilities" in broken["reverse_unmapped"]["mcp"]


def test_unenforced_authority_is_a_tracked_gap_not_a_claim() -> None:
    # CP10.2 (decision 0016) enforced every declared grant: no authority gap remains, and a
    # new one could only be recorded with an enforced authority that differs (validate()).
    pending = {o.id: o for o in registry.operations() if o.enforced is not None}
    assert pending == {}
    assert not [o.id for o in registry.operations() if o.gap("authority") is not None]
    for o in registry.operations():
        assert o.current_authority == o.authority, o.id


def test_openapi_is_not_served_but_still_generated_offline() -> None:
    from prometheos.hub.app import create_app
    from prometheos.hub.security import Gate

    app = create_app(inventory._InertService(), inventory.PLACEHOLDER_PROJECT, Gate(port=1))
    assert "/api/v1/openapi.json" not in {getattr(r, "path", "") for r in app.routes}
    document = app.openapi()
    assert "/api/v1/capabilities" in document["paths"]


def test_no_gap_remains(report: dict[str, Any]) -> None:
    assert report["gaps"] == []


# ----- the inventory catches what it should (mutation-style negatives) ------------------


def _surfaces_with(
    found: inventory.Surfaces, *, cli: str = "", mcp: str = "", http: str = ""
) -> inventory.Surfaces:
    return dataclasses.replace(
        found,
        cli=found.cli + ((cli,) if cli else ()),
        mcp=found.mcp + ((mcp,) if mcp else ()),
        http=found.http + ((http,) if http else ()),
    )


def test_an_unregistered_cli_command_fails_the_reverse_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prometheos import cli

    monkeypatch.setattr(cli.app, "registered_commands", list(cli.app.registered_commands))

    @cli.app.command("rogue-export")
    def rogue_export() -> None:
        """A command added outside the registry."""

    commands = inventory.cli_commands()
    assert "rogue-export" in commands
    mcp, tools = inventory.mcp_entries()
    found = inventory.Surfaces(commands, mcp, inventory.http_routes(), tools)
    result = inventory.compare(registry.operations(), found)
    assert result["reverse_unmapped"]["cli"] == ["rogue-export"]


def test_an_unregistered_mcp_tool_or_resource_fails_the_reverse_inventory() -> None:
    from prometheos.mcp_adapter import build_server as build

    server = build(inventory._InertService(), inventory.PLACEHOLDER_PROJECT)

    @server.tool()
    def rogue_dump() -> str:
        """A tool added outside the registry."""
        return "x"

    @server.resource(f"prometheos://project/{inventory.PLACEHOLDER_PROJECT}/everything")
    def rogue_resource() -> str:
        return "x"

    mcp, tools = inventory.mcp_entries(server)
    found = inventory.Surfaces(inventory.cli_commands(), mcp, inventory.http_routes(), tools)
    result = inventory.compare(registry.operations(), found)
    assert result["reverse_unmapped"]["mcp"] == [
        "resource:prometheos://project/{project_id}/everything",
        "rogue_dump",
    ]


def test_an_unregistered_http_route_fails_the_reverse_inventory() -> None:
    from prometheos.hub.app import create_app
    from prometheos.hub.security import Gate

    app = create_app(inventory._InertService(), inventory.PLACEHOLDER_PROJECT, Gate(port=1))

    @app.delete("/api/v1/records/{record_id}")
    def rogue_delete(record_id: str) -> dict[str, str]:
        return {"id": record_id}

    mcp, tools = inventory.mcp_entries()
    found = inventory.Surfaces(inventory.cli_commands(), mcp, inventory.http_routes(app), tools)
    result = inventory.compare(registry.operations(), found)
    assert result["reverse_unmapped"]["http"] == ["DELETE /api/v1/records/{record_id}"]


def test_mounts_websockets_and_unknown_routes_fail_instead_of_being_skipped() -> None:
    from fastapi import FastAPI, WebSocket
    from starlette.routing import BaseRoute

    from prometheos.hub.app import create_app
    from prometheos.hub.security import Gate

    app = create_app(inventory._InertService(), inventory.PLACEHOLDER_PROJECT, Gate(port=1))
    app.mount("/api/v1/sub", FastAPI())

    @app.websocket("/api/v1/stream")
    async def stream(socket: WebSocket) -> None:
        await socket.close()

    class Odd(BaseRoute):
        path = "/odd"

    app.router.routes.append(Odd())
    mcp, tools = inventory.mcp_entries()
    found = inventory.Surfaces(inventory.cli_commands(), mcp, inventory.http_routes(app), tools)
    result = inventory.compare(registry.operations(), found)
    assert set(result["reverse_unmapped"]["http"]) == {
        "MOUNT /api/v1/sub",
        "WS /api/v1/stream",
        "UNCLASSIFIED:Odd /odd",
    }


def test_forward_inventory_catches_missing_routes_and_owner_leaks(
    found: inventory.Surfaces,
) -> None:
    ops = list(registry.operations())
    index = {o.id: i for i, o in enumerate(ops)}

    def problems(replaced: registry.Operation, surfaces: inventory.Surfaces = found) -> list[str]:
        changed = list(ops)
        changed[index[replaced.id]] = replaced
        return list(inventory.compare(tuple(changed), surfaces)["forward_problems"])

    by_id = registry.by_id()
    # A required route dropped without a recorded gap.
    lost = dataclasses.replace(by_id["record_get"], mcp=())
    assert any("required mcp route missing" in p for p in problems(lost))
    # A route declared but not exposed by the application.
    phantom = dataclasses.replace(by_id["record_get"], http=("GET /api/v1/nowhere",))
    assert any("not exposed" in p for p in problems(phantom))
    # An owner-channel operation reachable over MCP.
    leak = dataclasses.replace(by_id["decision_grant"], mcp=("apply",))
    assert any("owner-channel" in p for p in problems(leak))
    # A long operation without run handling or a recorded run gap.
    silent = dataclasses.replace(by_id["paper_compile"], returns_run=False, gaps=())
    assert any("long operation" in p for p in problems(silent))
    # CP10.3: a run-returning operation without a run handler, and a handler of an operation
    # not declared to return runs.
    unhandled = dataclasses.replace(by_id["doctor"], long_running=True, returns_run=True)
    assert any("has no run handler" in p for p in problems(unhandled))
    assert any("not declared returns_run" in p for p in problems(silent))
    # A service function that does not exist.
    missing = dataclasses.replace(by_id["doctor"], service=("no_such_method",))
    assert any("does not exist" in p for p in problems(missing))
    # Both mapped and exempted.
    both = _surfaces_with(found)
    exempt = dataclasses.replace(by_id["project_info"], cli=("project info", "mcp serve"))
    assert any("both exempted and mapped" in p for p in problems(exempt, both))


def test_inventory_tool_writes_the_evidence_artifact(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("parity_tool", REPO / "tools/parity_inventory.py")
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    out = tmp_path / "inventory.json"
    assert tool.main(["--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["operation_count"] == len(registry.operations())
    assert data["forward_problems"] == [] and data["gaps_by_owner"] == {}
    assert tool.main(["--out", str(out)]) == 2  # never overwrites


# ----- capability discovery on every transport ------------------------------------------


def test_capabilities_are_the_same_on_cli_mcp_and_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app import texengine
    from prometheos.audio import models

    monkeypatch.setenv(texengine.ENGINE_DIR_ENV, str(tmp_path / "missing-engine"))
    monkeypatch.setenv(models.MODELS_DIR_ENV, str(tmp_path / "missing-models"))
    env = Env(tmp_path)
    direct = env.svc.capabilities(env.project_id)
    features = {f["feature"]: f for f in direct["features"]}
    assert set(features) == set(registry.CAPABILITY_GROUPS)
    # Truthful blockers in a fresh home: no engine installed, no provider configured.
    assert features["latex"]["available"] is False
    assert features["latex"]["blocker"]["code"] == "not_installed"
    assert features["provider"]["blocker"]["code"] in ("disabled_by_policy", "not_configured")
    assert features["core"]["available"] is True and features["core"]["blocker"] is None
    assert "grant" in features["provider"]["required_authority"]
    # Round 2 (R11): no version for what is not installed.
    assert features["latex"]["version"] is None
    # Round 2 (R4) and CP10.2: the enforced authority is reported; every grant is enforced
    # now, so nothing is pending and the engine install reports its real authority.
    assert all(f["authority_pending"] == [] for f in features.values())
    assert "grant" in features["latex"]["required_authority"]
    assert features["overleaf"]["execution"] == "remote"
    assert features["latex"]["cost_class"] == "download"
    assert "capabilities" in features["core"]["operations"]

    async def over_mcp() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            result = await client.call_tool("capabilities", {})
            return result.structured_content

    via_mcp = anyio.run(over_mcp)
    cli = Cli(env.tmp)
    code, via_cli, _ = cli("capabilities", "--project", env.project_id)
    assert code == 0
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        reply = hub.request("GET", "/api/v1/capabilities")
    assert reply.status == 200
    assert via_mcp == direct and via_cli == direct and reply.json() == direct

    # Discovery reveals no local path, endpoint or secret.
    text = json.dumps(direct)
    assert str(env.home) not in text and str(env.store_dir) not in text
    assert "pcap_" not in text and "\\\\" not in text


def test_capabilities_work_without_a_project(tmp_path: Path) -> None:
    code, payload, _ = Cli(tmp_path)("capabilities")
    assert code == 0 and payload["project_id"] is None
    provider = next(f for f in payload["features"] if f["feature"] == "provider")
    assert provider["blocker"] == {"code": "not_configured", "message": "no project selected"}


def test_a_provider_status_failure_degrades_only_that_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app import texengine
    from prometheos.domain.models import PrometheosError

    monkeypatch.setenv(texengine.ENGINE_DIR_ENV, str(tmp_path / "missing-engine"))
    env = Env(tmp_path)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise PrometheosError("io_error", "provider settings unreadable", {})

    monkeypatch.setattr(env.svc, "provider_status", broken)
    report = env.svc.capabilities(env.project_id)
    features = {f["feature"]: f for f in report["features"]}
    assert features["provider"]["available"] is False
    assert features["provider"]["blocker"]["message"] == "provider status unavailable (io_error)"
    assert features["core"]["available"] is True and features["latex"]["blocker"] is not None


# ----- the A02 error codes on HTTP and MCP (round 2, R12) -------------------------------


class _RaisingService:
    """``project_info`` raises the given error; everything else is unused."""

    def __init__(self, error: Any) -> None:
        self.error = error

    def open(self, project_id: str) -> None:
        return None

    def project_info(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error


def _approval_error() -> Any:
    from prometheos.domain.ids import new_operational_id
    from prometheos.domain.models import PrometheosError

    request_id = new_operational_id("apreq")
    return PrometheosError(
        "approval_required",
        "needs the owner",
        {
            "approval_request_id": request_id,
            "plan_sha256": "e" * 64,
            "required_scope": "exact_request",
            "owner_route": {"hub": f"/approvals/{request_id}", "cli": "prometheos decision review"},
            "expires_at": "2026-10-06T12:00:00Z",
        },
    )


def test_new_error_codes_map_to_http_403_and_503_and_the_mcp_envelope() -> None:
    from prometheos.domain.models import PrometheosError

    pid = inventory.PLACEHOLDER_PROJECT
    unavailable = PrometheosError(
        "capability_unavailable", "engine missing", {"capability": "latex", "blocker": "offline"}
    )
    for error, status in ((_approval_error(), 403), (unavailable, 503)):
        with running_hub(_RaisingService(error), pid) as hub:
            hub.pair()
            reply = hub.request("GET", "/api/v1/project")
        assert reply.status == status
        body = reply.json()
        assert body["error"]["code"] == error.code and body["error"]["retryable"] is False
        assert body["error"]["details"] == error.details

        async def over_mcp(err: Any = error) -> Any:
            async with Client(build_server(_RaisingService(err), pid)) as client:
                return await client.call_tool("project_info", {})

        result = anyio.run(over_mcp)
        assert result.is_error
        text = result.content[0].text
        envelope = json.loads(text[text.index("{") :])
        assert envelope["error"]["code"] == error.code
        assert envelope["error"]["details"] == error.details


def test_malformed_closed_details_become_a_plain_error_not_a_crash() -> None:
    from prometheos.domain.models import PrometheosError

    leaked = PrometheosError(
        "approval_required", "bad", {"approval_request_id": "x", "capability_secret": "pcap_s"}
    )
    assert leaked.code == "validation_error" and leaked.exit_code == 2
    response = leaked.to_response()
    assert response.error.details == {"intended_code": "approval_required"}
    assert "pcap_s" not in response.model_dump_json()
    # Details mutated after construction (the apply loop used to add op_index) degrade too.
    late = _approval_error()
    late.details["op_index"] = 0
    assert late.to_response().error.code == "validation_error"
    with running_hub(_RaisingService(leaked), inventory.PLACEHOLDER_PROJECT) as hub:
        hub.pair()
        reply = hub.request("GET", "/api/v1/project")
    assert reply.status == 400 and reply.json()["error"]["code"] == "validation_error"
