"""Bidirectional parity inventory (A02, CP10.1; C10-PARITY; decision 0015).

Enumerates the entry points the built application actually exposes -- every CLI command
(walking the real Typer command tree), every MCP tool and resource (from the real server
object) and every HTTP route (from the real hub router) -- and compares them with the
operation registry in both directions. ``tools/parity_inventory.py`` writes the result as a
JSON evidence artifact; ``tests/test_cp10_parity_inventory.py`` asserts it.
"""

from __future__ import annotations

import asyncio
import importlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prometheos import registry
from prometheos.registry import EXEMPTIONS, Operation

PLACEHOLDER_PROJECT = "project_00000000-0000-4000-8000-000000000000"


class _InertService:
    """Stands in for ``Service`` while the transports are built for enumeration only: the
    MCP server and the hub app call ``open`` at construction and nothing else."""

    def open(self, project_id: str) -> None:
        return None


@dataclass(frozen=True)
class Surfaces:
    cli: tuple[str, ...]
    mcp: tuple[str, ...]
    http: tuple[str, ...]
    mcp_tools: dict[str, dict[str, Any]]


def cli_commands(root: Any | None = None) -> tuple[str, ...]:
    """Every leaf command path of the real CLI (``"paper overleaf push"``)."""
    import typer

    from prometheos import cli

    command = root if root is not None else typer.main.get_command(cli.app)

    def walk(node: Any, path: list[str]) -> list[str]:
        if hasattr(node, "commands"):
            found: list[str] = []
            for name, sub in sorted(node.commands.items()):
                found += walk(sub, [*path, name])
            return found
        return [" ".join(path)]

    return tuple(walk(command, []))


def mcp_entries(server: Any | None = None) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    """Tool names and ``resource:<uri template>`` entries of the real MCP server, plus each
    tool's advertised input and output schemas."""
    from prometheos import mcp_adapter

    built = server or mcp_adapter.build_server(_InertService(), PLACEHOLDER_PROJECT)

    async def collect() -> tuple[list[Any], list[Any], list[Any]]:
        return (
            await built.list_tools(),
            await built.list_resources(),
            await built.list_resource_templates(),
        )

    tools, resources, templates = asyncio.run(collect())
    entries: list[str] = []
    schemas: dict[str, dict[str, Any]] = {}
    for t in tools:
        advertised = {
            "input": getattr(t, "input_schema", None),
            "output": getattr(t, "output_schema", None),
            "annotations": getattr(t, "annotations", None),
        }
        members = family_members(advertised["input"])
        if members is None:
            entries.append(t.name)
            schemas[t.name] = advertised
            continue
        # An operation-family tool (A02, CP10.6): each sub-operation is an entry point of its
        # own, ``<tool>#<op>``, with the schema of its member of the request union.
        from prometheos.mcp_schemas import _inline

        assert isinstance(advertised["output"], dict)
        output = _inline(advertised["output"])
        outputs = {
            m["properties"]["op"]["const"]: m
            for m in (output.get("oneOf") or [output])
            if "op" in m.get("properties", {})
        }
        for sub, member in members.items():
            entries.append(f"{t.name}#{sub}")
            schemas[f"{t.name}#{sub}"] = advertised | {"input": member, "output": outputs.get(sub)}
    entries += [
        "resource:" + str(r.uri).replace(PLACEHOLDER_PROJECT, "{project_id}") for r in resources
    ]
    entries += [
        "resource:" + str(t.uri_template).replace(PLACEHOLDER_PROJECT, "{project_id}")
        for t in templates
    ]
    return tuple(entries), schemas


def family_members(input_schema: Any) -> dict[str, dict[str, Any]] | None:
    """The sub-operations of an operation-family tool, read from its advertised input schema:
    a single ``request`` property that is an object with a constant ``op`` member, or a union
    (``oneOf``) of such objects. Any other tool is not a family tool."""
    if not isinstance(input_schema, dict):
        return None
    properties = input_schema.get("properties", {})
    if set(properties) != {"request"}:
        return None
    request = properties["request"]
    options = request.get("oneOf") or request.get("anyOf") or [request]
    found: dict[str, dict[str, Any]] = {}
    for option in options:
        op = option.get("properties", {}).get("op") if isinstance(option, dict) else None
        if not isinstance(op, dict) or not isinstance(op.get("const"), str):
            return None
        found[op["const"]] = option
    return found or None


def http_routes(app: Any | None = None) -> tuple[str, ...]:
    """``"METHOD /path"`` for every method route of the real hub app (HEAD is implied by
    GET). Anything else the router serves (a mount, a WebSocket, a sub-application, an
    unknown route type) is listed as ``"MOUNT /path"``, ``"WS /path"`` or
    ``"UNCLASSIFIED:<Type> /path"``: no registry operation can map those, so the reverse
    inventory fails on them instead of skipping them (decision 0015 round 2, R6)."""
    from starlette.routing import Mount, WebSocketRoute

    from prometheos.hub import app as hub_app
    from prometheos.hub.security import Gate

    built = app or hub_app.create_app(_InertService(), PLACEHOLDER_PROJECT, Gate(port=1))
    found: list[str] = []
    for route in built.routes:
        path = str(getattr(route, "path", ""))
        methods = sorted(set(getattr(route, "methods", None) or ()) - {"HEAD"})
        if methods:
            found += [f"{method} {path}" for method in methods]
        elif isinstance(route, WebSocketRoute):
            found.append(f"WS {path}")
        elif isinstance(route, Mount):
            found.append(f"MOUNT {path}")
        else:
            found.append(f"UNCLASSIFIED:{type(route).__name__} {path}")
    return tuple(found)


def surfaces() -> Surfaces:
    mcp, tools = mcp_entries()
    return Surfaces(cli=cli_commands(), mcp=mcp, http=http_routes(), mcp_tools=tools)


# ----- comparison -----------------------------------------------------------------------


def _resolve_service(name: str) -> bool:
    if ":" in name:
        module, _, attribute = name.partition(":")
        try:
            return hasattr(importlib.import_module(module), attribute)
        except ImportError:
            return False
    from prometheos.app.service import Service

    return callable(getattr(Service, name, None))


def hub_pages(repo: Path) -> set[str] | None:
    """Page components routed by ``hub/src/App.tsx`` (``<NamePage``), when the source exists."""
    source = repo / "hub" / "src" / "App.tsx"
    if not source.is_file():
        return None
    import re

    return set(re.findall(r"<([A-Z][A-Za-z]+)Page\b", source.read_text(encoding="utf-8")))


def _schema_ok(ref: str, tools: dict[str, dict[str, Any]]) -> bool:
    kind, _, rest = ref.partition(":")
    if kind == "contract":
        from prometheos.schemas import REGISTRY

        return rest in REGISTRY
    if kind == "mcp-tool":
        tool, _, part = rest.rpartition("#")  # a family sub-operation is ``tool#op``
        advertised = tools.get(tool, {})
        return bool(advertised.get("input" if part == "input" else "output"))
    return False


def compare(
    ops: tuple[Operation, ...],
    found: Surfaces,
    repo: Path | None = None,
    agent_routes: frozenset[str] | None = None,
) -> dict[str, Any]:
    """The full inventory: forward problems, gaps, reverse unmapped entry points.

    ``agent_routes`` (default: the hub's ``AGENT_ROUTES``) are the HTTP routes an agent token
    reaches: every HTTP route of an operation agents may use must be among them (review
    cp10-2-a F4), and no route of an owner-only operation may be, unless an agent operation
    shares it (its handler branches on the caller)."""
    if agent_routes is None:
        from prometheos.hub.app import AGENT_ROUTES

        agent_routes = AGENT_ROUTES
    exposed = {"cli": set(found.cli), "mcp": set(found.mcp), "http": set(found.http)}
    forward: list[str] = []
    owner_only = ("owner_channel", "owner_scope")
    agent_http = {r for o in ops if o.authority not in owner_only for r in o.http}
    for o in ops:
        for route in o.http:
            if o.authority not in owner_only and route not in agent_routes:
                forward.append(f"{o.id}: agent route not reachable with an agent token: {route}")
            if o.authority in owner_only and route in agent_routes and route not in agent_http:
                forward.append(f"{o.id}: owner-only route admits the agent token: {route}")
    gaps: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    pages = hub_pages(repo) if repo is not None else None
    forward += registry.validate(ops)
    for o in ops:
        row: dict[str, Any] = {
            "id": o.id,
            "family": o.family,
            "group": o.group,
            "authority": o.authority,
            "enforced_authority": o.current_authority,
            "mutates": o.mutates,
            "service": list(o.service),
            "routes": {"cli": list(o.cli), "mcp": list(o.mcp), "http": list(o.http)},
            "app": o.app,
            "no_app": o.no_app,
            "no_http": o.no_http,
            "http_contracts": {
                route: {"request_schema": request, "response_schema": response}
                for route, request, response in o.http_contracts
            },
            "request_schema": o.request_schema,
            "response_schema": o.response_schema,
            "long_running": o.long_running,
            "returns_run": o.returns_run,
            "gaps": [{"kind": g.kind, "owner": g.owner, "note": g.note} for g in o.gaps],
        }
        rows.append(row)
        for route, route_request, route_response in o.http_contracts:
            for bound_ref in (route_request, route_response):
                if not _schema_ok(bound_ref, found.mcp_tools):
                    forward.append(f"{o.id}: unresolved HTTP contract {route}: {bound_ref}")
        for transport in ("cli", "mcp", "http"):
            for route in o.routes(transport):
                if route not in exposed[transport]:
                    forward.append(f"{o.id}: declared {transport} route is not exposed: {route}")
        for transport in o.required():
            if not o.routes(transport) and o.gap(transport) is None:
                forward.append(f"{o.id}: required {transport} route missing and no gap recorded")
        if o.authority == "owner_channel":
            if o.mcp:
                forward.append(f"{o.id}: owner-channel operation exposed over MCP")
        if o.long_running and not o.returns_run and o.gap("run") is None:
            forward.append(f"{o.id}: long operation without a run ID and no run gap recorded")
        for ref in (o.request_schema, o.response_schema):
            if ref is None:
                if o.gap("schema") is None and o.gap("mcp") is None:
                    forward.append(f"{o.id}: no request/response schema and no gap recorded")
            elif not _schema_ok(ref, found.mcp_tools):
                forward.append(f"{o.id}: schema reference does not resolve: {ref}")
        for name in o.service:
            if not _resolve_service(name):
                forward.append(f"{o.id}: service function does not exist: {name}")
        if o.app is not None and pages is not None and o.app not in pages:
            forward.append(f"{o.id}: app path names no hub page: {o.app}")
        gaps += [
            {"operation": o.id, "kind": g.kind, "owner": g.owner, "note": g.note} for g in o.gaps
        ]
    # CP10.3 (decision 0017): an operation declared to return a run has a run handler, and
    # every run handler belongs to such an operation.
    from prometheos.app import runs as runs_mod

    runs_mod.load_handlers()
    declared = {o.id for o in ops if o.returns_run}
    for name in sorted(declared - set(runs_mod.HANDLERS)):
        forward.append(f"{name}: returns a run but has no run handler")
    for name in sorted(set(runs_mod.HANDLERS) - declared):
        forward.append(f"{name}: has a run handler but is not declared returns_run")
    mapped: dict[str, set[str]] = {"cli": set(), "mcp": set(), "http": set()}
    for o in ops:
        for transport in ("cli", "mcp", "http"):
            mapped[transport] |= set(o.routes(transport))  # type: ignore[arg-type]
    unmapped: dict[str, list[str]] = {}
    exempted: dict[str, list[str]] = {}
    for transport in ("cli", "mcp", "http"):
        names = sorted(exposed[transport])
        exempt = EXEMPTIONS[transport]  # type: ignore[index]
        unmapped[transport] = [n for n in names if n not in mapped[transport] and n not in exempt]
        exempted[transport] = [n for n in names if n in exempt]
        overlap = [n for n in names if n in mapped[transport] and n in exempt]
        forward += [f"{n}: both exempted and mapped to an operation" for n in overlap]
    by_owner = Counter(g["owner"] for g in gaps)
    by_kind = Counter(f"{g['owner']}:{g['kind']}" for g in gaps)
    return {
        "registry_version": registry.REGISTRY_VERSION,
        "operation_count": len(ops),
        "exposed": {k: sorted(v) for k, v in exposed.items()},
        "operations": rows,
        "forward_problems": forward,
        "reverse_unmapped": unmapped,
        "exempted": exempted,
        "exemptions": {k: dict(v) for k, v in EXEMPTIONS.items()},
        "gaps": gaps,
        "gaps_by_owner": dict(sorted(by_owner.items())),
        "gaps_by_owner_and_kind": dict(sorted(by_kind.items())),
    }


def build(repo: Path | None = None) -> dict[str, Any]:
    return compare(registry.operations(), surfaces(), repo)
