"""The operation registry (A02, CP10.1; spec/04 "Parity and capability discovery").

One declaration per application operation: its schemas, required authority, whether it
mutates, its operation family and capability group, its routes on every transport (CLI,
MCP, local HTTP), its app path and any recorded gap. The transports are not generated from
this module; they are *checked against* it in both directions by
``tests/test_cp10_parity_inventory.py`` (decision 0015):

- forward: every operation has each route its authority requires, or a recorded ``Gap``
  naming the CP10 task that closes it; owner-channel operations have no MCP route;
- reverse: every CLI command, MCP tool or resource and HTTP route the built application
  exposes maps to an operation here or to ``EXEMPTIONS``.

How later tracks use it:

- add an operation: append an ``op(...)`` to ``registry.declarations.OPERATIONS`` with its
  routes, in the same change that adds the command, tool or route;
- owner-only operations: ``authority="owner_channel"``; they need CLI (interactive) and
  HTTP (paired owner session) routes and must have no MCP route;
- protected operations an agent may request: ``authority="grant"`` (always needs an
  exact-plan owner grant) or ``"agent_or_grant"`` (only some requests carry protected
  effects); from CP10.2 these return ``approval_required`` to agents;
- long operations: ``long_running=True``; once it returns an ``oprun_`` ID, also set
  ``returns_run=True`` and drop its ``run`` gap (CP10.3);
- closing a gap: add the route and delete the ``Gap``; the gap budget in the inventory test
  only ever shrinks.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

REGISTRY_VERSION = 6
"""Bumped whenever ``EXEMPTIONS`` or the declaration format changes (reviewed with it)."""

Transport = Literal["cli", "mcp", "http"]
GapKind = Literal["cli", "mcp", "http", "app", "run", "schema", "authority"]
Authority = Literal["agent", "agent_or_grant", "grant", "owner_scope", "owner_channel"]

AUTHORITIES: dict[str, str] = {
    "agent": "agent scope suffices; no protected effect",
    "agent_or_grant": (
        "agent scope; requests with protected effects need an exact-plan owner grant "
        "(approval_required from CP10.2)"
    ),
    "grant": (
        "always needs an exact-plan owner grant; an agent reaches it as an approval request "
        "(CP10.2)"
    ),
    "owner_scope": "needs owner scope (a read_sensitive capability or the paired hub owner)",
    "owner_channel": (
        "issuing or revoking a grant or entering a credential: owner routes only (paired hub "
        "session, interactive CLI), never MCP or an agent-scoped route"
    ),
}

FAMILIES: tuple[str, ...] = (
    "project",
    "records",
    "sources",
    "context",
    "decisions",
    "meetings",
    "tasks",
    "skills",
    "workflows",
    "papers",
    "jobs",
    "latex",
    "audio",
    "provider",
    "administration",
    "discovery",
    "adoption",
    "library",
    "matrix",
)
"""Operation families: the unit of a future MCP family tool (spec/04)."""

CAPABILITY_GROUPS: dict[str, str] = {
    "core": "Project record: records, sources, context, decisions and agent tasks",
    "meetings": "Meeting capture and reconciliation",
    "skills": "Agent Skills catalogue and project-local host projection",
    "workflows": "Recipe runs reported by the host agent",
    "papers": "Paper readiness, export and handoff",
    "jobs": "Fixed-handler recurring jobs",
    "latex": "LaTeX workspace and compile with the pinned Tectonic engine",
    "overleaf": "Overleaf Git connector (system git)",
    "audio_capture": "Live audio capture (optional audio extra, Windows WASAPI)",
    "audio_transcription": "Local transcription (optional audio extra and a speech model)",
    "provider": "Configured generation provider (CP6.3)",
    "administration": "Backup, restore, migration, purge and diagnostics",
}
"""Capability groups: the features ``capabilities`` reports availability and blockers for."""

GAP_OWNERS: tuple[str, ...] = ("CP10.2", "CP10.3", "CP10.4", "CP10.5", "CP10.6")


@dataclass(frozen=True)
class Gap:
    """A required route (or run handling, schema or app path) that does not exist yet."""

    kind: GapKind
    owner: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.owner not in GAP_OWNERS:
            raise ValueError(f"gap owner must be one of {GAP_OWNERS}: {self.owner}")


@dataclass(frozen=True)
class Operation:
    id: str
    family: str
    group: str
    summary: str
    authority: Authority
    mutates: bool
    # Implementing application functions: ``Service`` method names, or ``module:attribute``
    # for the few operations that need no project service (skills authoring, schemas).
    service: tuple[str, ...]
    cli: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()
    http: tuple[str, ...] = ()
    app: str | None = None
    no_app: str | None = None
    no_http: str | None = None
    http_contracts: tuple[tuple[str, str, str], ...] = ()
    request: str | None = None
    response: str | None = None
    long_running: bool = False
    returns_run: bool = False
    gaps: tuple[Gap, ...] = field(default_factory=tuple)
    # The authority the code enforces today, when it is weaker than ``authority`` (always
    # with an ``authority`` gap owned by CP10.2; decision 0015 round 2, R4). Discovery and
    # the inventory report this, never the declared target, until the gap closes.
    enforced: Authority | None = None

    @property
    def current_authority(self) -> Authority:
        return self.enforced if self.enforced is not None else self.authority

    def required(self) -> tuple[Transport, ...]:
        if self.authority == "owner_channel":
            return ("cli",) if self.no_http else ("cli", "http")
        return ("cli", "mcp", "http")

    def routes(self, transport: Transport) -> tuple[str, ...]:
        return {"cli": self.cli, "mcp": self.mcp, "http": self.http}[transport]

    def gap(self, kind: GapKind) -> Gap | None:
        return next((g for g in self.gaps if g.kind == kind), None)

    @property
    def request_schema(self) -> str | None:
        if self.request is not None:
            return self.request
        tool = next((t for t in self.mcp if not t.startswith("resource:")), None)
        return f"mcp-tool:{tool}#input" if tool else None

    @property
    def response_schema(self) -> str | None:
        if self.response is not None:
            return self.response
        tool = next((t for t in self.mcp if not t.startswith("resource:")), None)
        return f"mcp-tool:{tool}#output" if tool else None


def op(
    id: str,
    family: str,
    group: str,
    summary: str,
    *,
    authority: Authority = "agent",
    mutates: bool = False,
    service: tuple[str, ...] | str = (),
    cli: tuple[str, ...] | str = (),
    mcp: tuple[str, ...] | str = (),
    http: tuple[str, ...] | str = (),
    app: str | None = None,
    no_app: str | None = None,
    no_http: str | None = None,
    http_contracts: tuple[tuple[str, str, str], ...] = (),
    request: str | None = None,
    response: str | None = None,
    long_running: bool = False,
    returns_run: bool = False,
    gaps: tuple[Gap, ...] = (),
    enforced: Authority | None = None,
) -> Operation:
    """Declaration helper: a single route may be given as a plain string."""

    def many(value: tuple[str, ...] | str) -> tuple[str, ...]:
        return (value,) if isinstance(value, str) else tuple(value)

    return Operation(
        id=id,
        family=family,
        group=group,
        summary=summary,
        authority=authority,
        mutates=mutates,
        service=many(service),
        cli=many(cli),
        mcp=many(mcp),
        http=many(http),
        app=app,
        no_app=no_app,
        no_http=no_http,
        http_contracts=http_contracts,
        request=request,
        response=response,
        long_running=long_running,
        returns_run=returns_run,
        gaps=gaps,
        enforced=enforced,
    )


# Exemptions: transport and presentation entry points that are not application operations
# (spec/04). Versioned with REGISTRY_VERSION and reviewed whenever they change. CLI help
# (``--help``) and ``--version`` are options, not commands, so the command walk never sees
# them; they are listed for completeness.
EXEMPTIONS: dict[Transport, dict[str, str]] = {
    "cli": {
        "mcp serve": "transport start (spec/04 exemption list)",
        "hub serve": "transport start (spec/04 exemption list)",
        # Version 4 (A07, CP21; decision 0088): the launcher starts, stops and reads the same
        # transport `hub serve` starts; none is an application operation.
        "start": "transport start: the home's hub in the background (A07, CP21)",
        "stop": "transport stop of the hub `start` started (A07, CP21)",
        "status": "transport status from the hub lock (A07, CP21)",
        "--help": "CLI help (an option on every command; spec/04 exemption list)",
        "--version": "CLI version (a root option; spec/04 exemption list)",
    },
    "mcp": {},
    "http": {
        "POST /api/v1/pair": "pairing route (spec/04 exemption list)",
        "GET /api/v1/session": "session route (spec/04 exemption list)",
        "POST /api/v1/logout": "session route (spec/04 exemption list)",
        # Version 5 (A07, CP21.2; decision 0089): a remembered browser resumes its session.
        "POST /api/v1/device/resume": "session route: a remembered device (A07, CP21.2)",
        "GET /api/v1/changes": "changes long-poll (spec/04 exemption list)",
        "POST /api/v1/launcher/code": "pairing: a launch code, owner channel only (A07, CP21)",
        "POST /api/v1/launcher/stop": "transport stop, owner channel only (A07, CP21)",
        "GET /{path:path}": "static asset routes (spec/04 exemption list)",
        "GET /sw.js": "the PWA's service worker, a static asset written by the hub (decision 0090)",
    },
}


def operations() -> tuple[Operation, ...]:
    from prometheos.registry.declarations import OPERATIONS

    return OPERATIONS


def by_id() -> dict[str, Operation]:
    return {o.id: o for o in operations()}


def validate(ops: tuple[Operation, ...]) -> list[str]:
    """Static consistency of the declarations themselves (not of the transports)."""
    problems: list[str] = []
    counts = Counter(o.id for o in ops)
    problems += [f"duplicate operation id: {i}" for i, n in counts.items() if n > 1]
    for o in ops:
        if o.family not in FAMILIES:
            problems.append(f"{o.id}: unknown family {o.family}")
        if o.group not in CAPABILITY_GROUPS:
            problems.append(f"{o.id}: unknown capability group {o.group}")
        if o.authority not in AUTHORITIES:
            problems.append(f"{o.id}: unknown authority {o.authority}")
        if not o.service:
            problems.append(f"{o.id}: names no application service method")
        if o.authority == "owner_channel" and o.mcp:
            problems.append(f"{o.id}: owner-channel operations must not have MCP routes")
        if (o.app is None) == (o.no_app is None) and o.gap("app") is None:
            problems.append(f"{o.id}: declare exactly one of app, no_app or an app gap")
        if o.app is not None and o.gap("app") is not None:
            problems.append(f"{o.id}: has both an app path and an app gap")
        if o.no_http and (o.authority != "owner_channel" or o.http or not o.no_http.strip()):
            problems.append(
                f"{o.id}: no_http needs a reason, owner-channel authority and no HTTP routes"
            )
        if len({r for r, _, _ in o.http_contracts}) != len(o.http_contracts):
            problems.append(f"{o.id}: duplicate HTTP contract bindings")
        for route, request, response in o.http_contracts:
            if (
                route not in o.http
                or not request.startswith("contract:")
                or not response.startswith("contract:")
            ):
                problems.append(f"{o.id}: invalid HTTP contract binding: {route}")
        if o.returns_run and not o.long_running:
            problems.append(f"{o.id}: returns_run without long_running")
        authority_gap = o.gap("authority")
        if (authority_gap is None) != (o.enforced is None):
            problems.append(f"{o.id}: an enforced authority comes with an authority gap")
        if o.enforced is not None and o.enforced == o.authority:
            problems.append(f"{o.id}: enforced authority equals the declared one")
        if authority_gap is not None and authority_gap.owner != "CP10.2":
            problems.append(f"{o.id}: authority gaps are owned by CP10.2")
        for route in o.http:
            method = route.split(" ", 1)[0]
            if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                problems.append(f"{o.id}: HTTP route must be a method route: {route}")
        kinds = [g.kind for g in o.gaps]
        if len(kinds) != len(set(kinds)):
            problems.append(f"{o.id}: duplicate gap kinds")
        for transport in ("cli", "mcp", "http"):
            gap = o.gap(transport)
            if gap is not None and o.routes(transport):
                problems.append(f"{o.id}: {transport} gap recorded although routes exist")
    return problems
