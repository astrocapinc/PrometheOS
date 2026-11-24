"""Agent Skills catalogue and project-local host projection (decision 0022 item 3).

MCP family tools ``skills_read`` and ``skills_manage``. A package folder, a zip, an install
target or an output folder is always a research root alias plus a path inside it
(:func:`prometheos.agentops.research_path`). Catalogue operations act on the per-user catalogue
exactly as the CLI does; a package that carries executable content without resolved rights is
quarantined, and trust is the owner's (``skill_trust`` is an approval request).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from prometheos import skills as skills_mod
from prometheos.agentops import (
    Ctx,
    Params,
    RelPath,
    RequestId,
    RootAlias,
    Sha256,
    family,
    labelled,
    operation,
    research_path,
)
from prometheos.domain.models import PrometheosError

family(
    "skills_read",
    "read",
    "Agent Skills catalogue: list, verify digests and rights, statically inspect or validate a "
    "package folder or zip inside a research root. Nothing is executed or copied.",
)
family(
    "skills_manage",
    "destructive",
    "Agent Skills lifecycle: plan and import a package, plan and apply a project-local install, "
    "update or removal (pass the reviewed plan_sha256 to apply), revoke, scaffold, refresh, "
    "export, adapt, and set up a coding host. Folders are research-root relative; restoring "
    "trust is the owner's (skill_trust).",
)

PackageRef = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:@[0-9A-Za-z.+-]{5,100})?$")]
SkillName = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)]
Host = Literal["claude-code", "opencode"]
# Host setup also writes the Claude Code plugin package (CP19.3, decision 0046).
SetupHost = Literal["claude-code", "opencode", "claude-code-plugin"]
OptRelPath = Annotated[
    str | None,
    Field(max_length=1024, description="Folder inside the root; omit for the root itself"),
]


def _under(base: Path, value: Any) -> Any:
    """Absolute paths under ``base`` become root-relative (POSIX) strings, recursively."""
    if isinstance(value, dict):
        return {k: _under(base, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_under(base, v) for v in value]
    if isinstance(value, str) and len(value) > 2 and (value[1:3] == ":\\" or value[0] in "/\\"):
        try:
            return Path(value).resolve().relative_to(base.resolve()).as_posix() or "."
        except (ValueError, OSError):
            return value
    return value


def _folder(ctx: Ctx, alias: str, folder: str | None) -> tuple[Path, str]:
    path, relative = research_path(ctx, alias, folder or None)
    return Path(path), relative


# ----- skills_read ----------------------------------------------------------------------------


class NoParams(Params):
    pass


@operation(
    "skill_catalog",
    NoParams,
    "Imported packages with status and trust labels, and the revocation list",
    tool="skills_read",
    sub="catalog",
)
def skill_catalog(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.skill_catalog()


class RefParams(Params):
    ref: PackageRef


def skill_detail(service: Any, ref: str) -> dict[str, Any]:
    """The verify report with what the catalogue knows of the package (the hub's skill view,
    shared by the HTTP route and the MCP sub-operation)."""
    report: dict[str, Any] = service.skill_verify(ref)
    entry = service.catalog.get(ref)
    sidecar = service.catalog.sidecar(entry)
    return report | {
        "package_digest": entry["package_digest"],
        "description": entry["description"],
        "capabilities": entry["capabilities"],
        "trust": entry["trust"],
        "origin": {k: v for k, v in entry["source"].items() if k != "path"},
        "files": entry["files"],
        "recipes": [r.model_dump(mode="json") for r in sidecar.recipes],
    }


@operation(
    "skill_verify",
    RefParams,
    "Recompute catalogue digests and rights completeness (nothing is executed)",
    tool="skills_read",
    sub="verify",
)
def skill_verify(ctx: Ctx, p: RefParams) -> Any:
    return skill_detail(ctx.service, p.ref)


class SourceParams(Params):
    root_alias: RootAlias
    path: RelPath


@operation(
    "skill_inspect",
    SourceParams,
    "Static inspection of a package folder or zip inside a research root",
    tool="skills_read",
    sub="inspect",
    http="POST /api/v1/skills/inspect",
)
def skill_inspect(ctx: Ctx, p: SourceParams) -> Any:
    path, relative = research_path(ctx, p.root_alias, p.path)
    result = ctx.service.skill_inspect(Path(path))
    return result | {"source": {"kind": "research_root", **labelled(p.root_alias, relative)}}


@operation(
    "skill_import_plan",
    SourceParams,
    "Plan importing a package into the catalogue; the digest is what the researcher reviews",
    tool="skills_read",
    sub="import_plan",
    http="POST /api/v1/skills/import-plan",
)
def skill_import_plan(ctx: Ctx, p: SourceParams) -> Any:
    path, _ = research_path(ctx, p.root_alias, p.path)
    return ctx.service.skill_import_plan(Path(path))


@operation(
    "skill_validate",
    SourceParams,
    "Validate a skill folder against the Agent Skills specification",
    tool="skills_read",
    sub="validate",
    http="POST /api/v1/skills/validate",
)
def skill_validate(ctx: Ctx, p: SourceParams) -> Any:
    path, relative = research_path(ctx, p.root_alias, p.path)
    errors = skills_mod.validate_skill(Path(path))
    if errors:
        raise PrometheosError("validation_error", "skill is not valid", {"errors": errors})
    return {"valid": True, "skill": Path(relative).name}


# ----- skills_manage --------------------------------------------------------------------------


class ImportParams(SourceParams):
    plan_sha256: Sha256


@operation(
    "skill_import",
    ImportParams,
    "Import exactly the reviewed plan into the catalogue (quarantined without resolved rights)",
    tool="skills_manage",
    sub="import",
    http="POST /api/v1/skills/import",
)
def skill_import(ctx: Ctx, p: ImportParams) -> Any:
    path, _ = research_path(ctx, p.root_alias, p.path)
    return ctx.service.skill_import(Path(path), p.plan_sha256)


class InstallParams(RefParams):
    host: Host = "claude-code"
    root_alias: RootAlias
    folder: OptRelPath = None
    plan_sha256: Sha256 | None = None
    record_in_project: bool = False
    request_id: RequestId | None = None


def _install(ctx: Ctx, p: InstallParams, resolve: str | None) -> Any:
    target, relative = _folder(ctx, p.root_alias, p.folder)
    if p.plan_sha256 is not None and not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id,
            ctx.caller,
            "skill_update" if isinstance(p, UpdateParams) else "skill_install",
            p.model_dump(mode="json", exclude={"request_id"}),
            p.request_id,
        )
    result = ctx.service.skill_install(
        p.ref,
        p.host,
        target,
        p.plan_sha256,
        resolve,
        ctx.project_id if p.record_in_project else None,
        authority=ctx.authority,
    )
    return _under(target, result) | {"target": labelled(p.root_alias, relative)}


@operation(
    "skill_install",
    InstallParams,
    "Plan (no digest) or apply (reviewed plan_sha256) a project-local projection of a package",
    tool="skills_manage",
    sub="install",
    http="POST /api/v1/skills/{ref}/install",
)
def skill_install(ctx: Ctx, p: InstallParams) -> Any:
    return _install(ctx, p, None)


class UpdateParams(InstallParams):
    resolve: Literal["keep-local", "take-package"] | None = None


@operation(
    "skill_update",
    UpdateParams,
    "Plan or apply an update to another version; local edits need an explicit choice",
    tool="skills_manage",
    sub="update",
    http="POST /api/v1/skills/{ref}/update",
)
def skill_update(ctx: Ctx, p: UpdateParams) -> Any:
    return _install(ctx, p, p.resolve)


class RemoveParams(Params):
    name: SkillName
    host: Host = "claude-code"
    root_alias: RootAlias
    folder: OptRelPath = None
    plan_sha256: Sha256 | None = None
    record_in_project: bool = False
    request_id: RequestId | None = None


@operation(
    "skill_remove",
    RemoveParams,
    "Plan or apply removal of lock-owned, unmodified files only",
    tool="skills_manage",
    sub="remove",
    http="POST /api/v1/skills/remove",
)
def skill_remove(ctx: Ctx, p: RemoveParams) -> Any:
    target, relative = _folder(ctx, p.root_alias, p.folder)
    if p.plan_sha256 is not None and not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id,
            ctx.caller,
            "skill_remove",
            p.model_dump(mode="json", exclude={"request_id"}),
            p.request_id,
        )
    result = ctx.service.skill_remove(
        p.name,
        p.host,
        target,
        p.plan_sha256,
        ctx.project_id if p.record_in_project else None,
        authority=ctx.authority,
    )
    return _under(target, result) | {"target": labelled(p.root_alias, relative)}


class RevokeParams(RefParams):
    root_alias: RootAlias | None = None
    folder: OptRelPath = None


@operation(
    "skill_revoke",
    RevokeParams,
    "Add a package to the revocation list and quarantine it; optionally show removal plans",
    tool="skills_manage",
    sub="revoke",
    http="POST /api/v1/skills/{ref}/revoke",
)
def skill_revoke(ctx: Ctx, p: RevokeParams) -> Any:
    target = None
    if p.root_alias is not None:
        target, _ = _folder(ctx, p.root_alias, p.folder)
    elif p.folder is not None:
        raise PrometheosError("validation_error", "folder needs a root_alias", {})
    result = ctx.service.skill_revoke(p.ref, target)
    return _under(target, result) if target is not None else result


class CreateParams(Params):
    name: SkillName
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    license: Annotated[str, Field(min_length=1, max_length=200)] = "unresolved"
    root_alias: RootAlias
    folder: OptRelPath = None


@operation(
    "skill_create",
    CreateParams,
    "Scaffold an original package folder (SKILL.md, references, examples, sidecar)",
    tool="skills_manage",
    sub="create",
    http="POST /api/v1/skills/create",
)
def skill_create(ctx: Ctx, p: CreateParams) -> Any:
    out, relative = _folder(ctx, p.root_alias, p.folder)
    chosen = None if p.license == "unresolved" else p.license
    result = ctx.service.skill_create(p.name, p.description, chosen, out)
    return _under(out, result) | {"out": labelled(p.root_alias, relative)}


class RefreshParams(SourceParams):
    pass


@operation(
    "skill_refresh",
    RefreshParams,
    "Recompute component digests after editing a package folder and record modifications",
    tool="skills_manage",
    sub="refresh",
    http="POST /api/v1/skills/refresh",
)
def skill_refresh(ctx: Ctx, p: RefreshParams) -> Any:
    folder, _ = research_path(ctx, p.root_alias, p.path)
    return _under(Path(folder), ctx.service.skill_refresh(Path(folder)))


class ExportParams(RefParams):
    root_alias: RootAlias
    path: RelPath
    draft: bool = False


@operation(
    "skill_export",
    ExportParams,
    "Export a portable bundle with lock, notices and modification record to a new path",
    tool="skills_manage",
    sub="export",
    http="POST /api/v1/skills/{ref}/export",
)
def skill_export(ctx: Ctx, p: ExportParams) -> Any:
    out, relative = research_path(ctx, p.root_alias, p.path)
    result = ctx.service.skill_export(p.ref, Path(out), p.draft)
    return _under(Path(ctx.service.research_root(ctx.project_id, p.root_alias)), result) | {
        "out": labelled(p.root_alias, relative)
    }


class AdaptParams(RefParams):
    name: SkillName
    root_alias: RootAlias
    folder: OptRelPath = None


@operation(
    "skill_adapt",
    AdaptParams,
    "Copy a rights-resolved package into a new local identity with its parent recorded",
    tool="skills_manage",
    sub="adapt",
    http="POST /api/v1/skills/{ref}/adapt",
)
def skill_adapt(ctx: Ctx, p: AdaptParams) -> Any:
    out, relative = _folder(ctx, p.root_alias, p.folder)
    result = ctx.service.skill_adapt(p.ref, p.name, out)
    return _under(out, result) | {"out": labelled(p.root_alias, relative)}


class HostSetupParams(Params):
    host: SetupHost = "claude-code"
    root_alias: RootAlias
    folder: OptRelPath = None
    apply: bool = False


@operation(
    "host_setup",
    HostSetupParams,
    "Show or write the project-local MCP entry and skills for a coding host (never global)",
    tool="skills_manage",
    sub="host_setup",
    http="POST /api/v1/host/setup",
)
def host_setup(ctx: Ctx, p: HostSetupParams) -> Any:
    target, relative = _folder(ctx, p.root_alias, p.folder)
    plan = skills_mod.host_plan(p.host, target, ctx.project_id)
    written = skills_mod.host_apply(plan) if p.apply else []
    home = getattr(ctx.service, "home", None)
    if p.apply and p.host == "claude-code-plugin" and isinstance(home, Path):
        from prometheos.agent_package import record_setup

        record_setup(home, Path(plan["target"]), ctx.project_id)
    shown = {k: v for k, v in plan.items() if k != "_contents"} | {
        "applied": p.apply,
        "written": written,
    }
    return _under(target, shown) | {"target": labelled(p.root_alias, relative)}
