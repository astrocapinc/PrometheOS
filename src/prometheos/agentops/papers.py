"""Paper overview, readiness save, export, hand-off and requirements draft (decision 0022).

MCP family tools ``papers_read`` and ``papers_manage``. The export bundle and the hand-off
folder are research-root relative; an export is one new zip inside a root and is read back with
the version tools once registered by the researcher, or from the folder by the owner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from prometheos.agentops import (
    Ctx,
    Params,
    RecordId,
    RelPath,
    RequestId,
    RootAlias,
    Sha256,
    family,
    labelled,
    operation,
    research_path,
)

family(
    "papers_read",
    "read",
    "Paper overview, where every number in the manuscript comes from, a pure comparison of the "
    "manuscript bundle with a folder, and requirement rows drafted from a retained venue source. "
    "Nothing is written.",
)
family(
    "papers_manage",
    "destructive",
    "Save a readiness check as a notebook entry, write the bundle as one new zip, or write a "
    "reviewed hand-off plan's new files into a folder (never deletes, never runs Git).",
)

VersionId = Annotated[str, Field(min_length=1, max_length=80)]
OptFolder = Annotated[
    str | None, Field(max_length=1024, description="Folder inside the root; omit for the root")
]


class PaperParams(Params):
    paper_id: RecordId


@operation(
    "paper_get",
    PaperParams,
    "Paper overview: record, venue requirements, anchors, versions, findings summary",
    tool="papers_read",
    sub="get",
)
def paper_get(ctx: Ctx, p: PaperParams) -> Any:
    return ctx.service.paper_overview(ctx.project_id, ctx.caller, p.paper_id)


class RequirementsParams(Params):
    version_id: VersionId


@operation(
    "paper_requirements_draft",
    RequirementsParams,
    "Draft unknown requirement rows from list items of a retained venue source version",
    tool="papers_read",
    sub="requirements_draft",
    http="GET /api/v1/versions/{version_id}/requirements-draft",
)
def requirements_draft(ctx: Ctx, p: RequirementsParams) -> Any:
    return ctx.service.requirements_draft(ctx.project_id, ctx.caller, p.version_id)


class HandoffPlanParams(PaperParams):
    root_alias: RootAlias
    folder: OptFolder = None
    version_id: VersionId | None = None


def _into(ctx: Ctx, root_alias: str, folder: str | None) -> tuple[Path, str]:
    path, relative = research_path(ctx, root_alias, folder or None)
    return Path(path), relative


@operation(
    "paper_handoff_plan",
    HandoffPlanParams,
    "Compare the bundle with a folder: new, identical, conflict, folder-only (pure)",
    tool="papers_read",
    sub="handoff_plan",
    http="GET /api/v1/papers/{paper_id}/handoff/plan",
)
def handoff_plan(ctx: Ctx, p: HandoffPlanParams) -> Any:
    into, relative = _into(ctx, p.root_alias, p.folder)
    plan = ctx.service.paper_handoff_plan(
        ctx.project_id, ctx.caller, p.paper_id, into, p.version_id
    )
    return _without_location(plan) | {"into": labelled(p.root_alias, relative)}


class CompassParams(Params):
    paper_id: RecordId | None = None
    limit: Annotated[int, Field(ge=1, le=50)] = 12


@operation(
    "research_compass",
    CompassParams,
    "Where to look next: ranked directions from the record (results to read, claims to resolve "
    "or verify, papers to frame, numbers to rerun or trace, questions to answer, novelty checks), "
    "each with why, what to do and when it is done (pure)",
    tool="papers_read",
    sub="compass",
)
def research_compass(ctx: Ctx, p: CompassParams) -> Any:
    return ctx.service.research_compass(
        ctx.project_id, ctx.caller, paper_id=p.paper_id, limit=p.limit
    )


@operation(
    "paper_numbers",
    PaperParams,
    "Where every number in the manuscript comes from: project macros traced to their file, run "
    "and claims, claims' bound quantities, and numbers typed by hand (pure)",
    tool="papers_read",
    sub="numbers",
)
def paper_numbers(ctx: Ctx, p: PaperParams) -> Any:
    return ctx.service.paper_numbers(ctx.project_id, ctx.caller, p.paper_id)


class SaveParams(PaperParams):
    request_id: RequestId | None = None


@operation(
    "paper_check_save",
    SaveParams,
    "Run the readiness check and save the agent-visible findings as one notebook entry",
    tool="papers_manage",
    sub="check_save",
)
def check_save(ctx: Ctx, p: SaveParams) -> Any:
    return ctx.service.paper_check_save(ctx.project_id, p.paper_id, ctx.rid(p.request_id))


class ExportParams(PaperParams):
    root_alias: RootAlias
    path: RelPath
    version_id: VersionId | None = None


@operation(
    "paper_export",
    ExportParams,
    "Write a local bundle bound to exact manuscript and source versions as one new zip",
    tool="papers_manage",
    sub="export",
    http="POST /api/v1/papers/{paper_id}/export",
)
def export(ctx: Ctx, p: ExportParams) -> Any:
    out, relative = research_path(ctx, p.root_alias, p.path)
    result = ctx.service.paper_export(
        ctx.project_id, ctx.caller, p.paper_id, Path(out), p.version_id
    )
    return _without_location(result) | {"out": labelled(p.root_alias, relative)}


class HandoffApplyParams(HandoffPlanParams):
    digest: Sha256
    overwrite: list[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(
        default_factory=list, max_length=200
    )


@operation(
    "paper_handoff_apply",
    HandoffApplyParams,
    "Write the reviewed plan's new files into the folder; conflicts only when named",
    tool="papers_manage",
    sub="handoff_apply",
    http="POST /api/v1/papers/{paper_id}/handoff/apply",
)
def handoff_apply(ctx: Ctx, p: HandoffApplyParams) -> Any:
    into, relative = _into(ctx, p.root_alias, p.folder)
    result = ctx.service.paper_handoff_apply(
        ctx.project_id, ctx.caller, p.paper_id, into, p.digest, list(p.overwrite), p.version_id
    )
    return _without_location(result) | {"into": labelled(p.root_alias, relative)}


def _without_location(value: dict[str, Any]) -> dict[str, Any]:
    """The service names the folder or file it wrote by absolute path; the reply names it by
    root alias and relative path instead (added by the caller)."""
    return {k: v for k, v in value.items() if k not in ("written", "into", "target", "folder")}
