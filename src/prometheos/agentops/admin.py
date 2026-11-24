"""Administration, diagnostics, schemas, projects, provider extras and the engine plan
(decision 0022).

MCP family tools ``administration_read``, ``administration_manage``, ``provider_read`` and
``latex_read``. Backup, restore, migration and the recovery screen belong to the maintenance
service (CP10.5); protected steps stay approval requests (purge commit, provider configure).
A project created over MCP or HTTP carries no research roots and no custom store location: an
agent cannot widen what a project may read by creating one (roots are the owner's, set in the
CLI or the hub).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import Field

from prometheos.agentops import (
    UUID4 as RequestIdPattern,
)
from prometheos.agentops import (
    Ctx,
    Params,
    family,
    operation,
)
from prometheos.domain.canonical import loads_strict
from prometheos.domain.models import JsonObjectInput, PrometheosError
from prometheos.schemas import REGISTRY, schema_text

family(
    "administration_read",
    "read",
    "Environment and store diagnostics, the exported JSON Schema names and documents, the "
    "registered projects, and the owner-only purge plan (an agent is told it needs owner "
    "scope; the owner's plan is committed through retention_purge_commit).",
)
family(
    "administration_manage",
    "destructive",
    "Create a project (no research roots, default store), add an approved research folder "
    "(the owner's decision: from an agent it is an approval request, approval_required) and "
    "report or collect unreferenced media and staging files older than a grace period.",
)
family(
    "provider_read",
    "read",
    "Generation provider: the call ledger (routes, states, estimates, digests; never message "
    "text) and a preview of a configuration change (credential references only).",
)
family(
    "latex_read",
    "read",
    "The pinned Tectonic engine install plan: size, licence, destination and network use. "
    "Installing needs the owner (tex_engine_install is an approval request).",
)


class NoParams(Params):
    pass


@operation(
    "doctor",
    NoParams,
    "Read-only environment and store report",
    tool="administration_read",
    sub="doctor",
    http="GET /api/v1/doctor",
)
def doctor(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.doctor(ctx.project_id)


@operation(
    "schema_list",
    NoParams,
    "Names of the exported JSON Schemas",
    tool="administration_read",
    sub="schema_list",
    http="GET /api/v1/schemas",
)
def schema_list(ctx: Ctx, p: NoParams) -> Any:
    return {"schemas": sorted(REGISTRY)}


class SchemaShowParams(Params):
    name: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,80}$")]


@operation(
    "schema_show",
    SchemaShowParams,
    "One exported JSON Schema document",
    tool="administration_read",
    sub="schema_show",
    http="GET /api/v1/schemas/{name}",
)
def schema_show(ctx: Ctx, p: SchemaShowParams) -> Any:
    if p.name not in REGISTRY:
        raise PrometheosError(
            "not_found", "unknown schema", {"name": p.name, "schemas": sorted(REGISTRY)}
        )
    return {"name": p.name, "schema": json.loads(schema_text(p.name))}


@operation(
    "project_list",
    NoParams,
    "Registered projects",
    tool="administration_read",
    sub="projects",
    http="GET /api/v1/projects",
)
def project_list(ctx: Ctx, p: NoParams) -> Any:
    projects = ctx.service.list_projects()
    if not ctx.owner:
        projects = [p for p in projects if p["project_id"] == ctx.project_id]
    return {"projects": projects}


class PurgePlanParams(Params):
    ids: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=80)]],
        Field(min_length=1, max_length=200),
    ]


@operation(
    "retention_purge_plan",
    PurgePlanParams,
    "Compute the purge closure and limitations (owner scope; no deletion)",
    tool="administration_read",
    sub="purge_plan",
    http="POST /api/v1/retention/purge/plan",
)
def purge_plan(ctx: Ctx, p: PurgePlanParams) -> Any:
    return ctx.service.purge_plan(ctx.project_id, list(p.ids), ctx.caller)


class InitParams(Params):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    objective: Annotated[str, Field(min_length=1, max_length=2000)]
    timezone: Annotated[str, Field(min_length=1, max_length=64)] | None = None


@operation(
    "project_init",
    InitParams,
    "Create one project store in the default location and register it",
    tool="administration_manage",
    sub="project_init",
    http="POST /api/v1/projects",
)
def project_init(ctx: Ctx, p: InitParams) -> Any:
    if not ctx.owner:
        raise PrometheosError("policy_denied", "project creation requires the owner", {})
    return ctx.service.init_project(title=p.title, objective=p.objective, timezone=p.timezone)


class OrphansParams(Params):
    collect: bool = False
    grace_seconds: Annotated[int, Field(ge=0, le=365 * 24 * 3600)] = 24 * 3600


@operation(
    "maintenance_orphans",
    OrphansParams,
    "Report (and with collect, delete) unreferenced media and staging files past the grace",
    tool="administration_manage",
    sub="orphans",
    http="POST /api/v1/maintenance/orphans",
)
def orphans(ctx: Ctx, p: OrphansParams) -> Any:
    return ctx.service.orphans(ctx.project_id, p.collect, p.grace_seconds)


@operation(
    "provider_calls",
    NoParams,
    "The call ledger: routes, states, estimates and digests; never message text",
    tool="provider_read",
    sub="calls",
)
def provider_calls(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.provider_calls(ctx.project_id, ctx.caller)


class ConfigureParams(Params):
    config: JsonObjectInput


@operation(
    "provider_configure_preview",
    ConfigureParams,
    "Show what a configuration change would do (credential references only); applies nothing",
    tool="provider_read",
    sub="configure_preview",
)
def provider_configure_preview(ctx: Ctx, p: ConfigureParams) -> Any:
    revision = ctx.service.provider_status(ctx.project_id, ctx.caller)["revision"]
    loads_strict(json.dumps(p.config).encode("utf-8"))  # duplicate keys cannot arrive; defence
    return ctx.service.provider_configure_preview(ctx.project_id, p.config, revision)


@operation(
    "tex_engine_plan",
    NoParams,
    "Install plan: size, licence, destination and network use",
    tool="latex_read",
    sub="engine_plan",
)
def tex_engine_plan(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.tex_engine_plan(redact=True)


# ----- CP11 S11.4b: research folders (decision 0066) ---------------------------------------------


@operation(
    "project_roots",
    NoParams,
    "The project's approved research folders (alias, path, whether the folder is there now); "
    "folders cannot be removed",
    tool="administration_read",
    sub="project_roots",
    http="GET /api/v1/project/roots",
)
def project_roots(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.project_roots(ctx.project_id, ctx.caller)


class RootAddParams(Params):
    alias: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
    path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1024,
            description="Absolute path of an existing local folder (not the PrometheOS home, a "
            "system folder or a folder overlapping another research folder)",
        ),
    ]
    request_id: Annotated[str, Field(pattern=RequestIdPattern)] | None = None


@operation(
    "project_root_add",
    RootAddParams,
    "Add an approved research folder (audited as a project settings update); the owner's "
    "decision, an approval request from an agent",
    tool="administration_manage",
    sub="project_root_add",
    http="POST /api/v1/project/roots",
)
def project_root_add(ctx: Ctx, p: RootAddParams) -> Any:
    if not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id, ctx.caller, "project_root_add",
            {"alias": p.alias, "path": p.path}, p.request_id,
        )  # fmt: skip
    return ctx.service.project_root_add(
        ctx.project_id, p.alias, p.path, authority=ctx.authority, request_id=p.request_id
    )
