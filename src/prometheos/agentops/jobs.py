"""Recurring jobs and the job inbox (decision 0022).

MCP family tools ``jobs_read`` and ``jobs_manage``. Jobs run only fixed handlers
(``source_refresh``, ``paper_check``, ``digest``) and only when ``tick`` runs; resolving an
unknown slot stays the owner's (``job_resolve`` is an approval request).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, JsonValue, TypeAdapter, WithJsonSchema

from prometheos.agentops import (
    Ctx,
    Params,
    RecordId,
    RequestId,
    family,
    operation,
)
from prometheos.domain.bodies import JobBody
from prometheos.domain.models import MAX_INT
from prometheos.mcp_schemas import _inline

family(
    "jobs_read",
    "read",
    "Recurring jobs: preview a job's schedule and budget, list jobs with their slots, read the "
    "inbox of findings and failures. Nothing runs and nothing is recorded.",
)
family(
    "jobs_manage",
    "write_new",
    "Record, enable, disable or cancel a job slot, run due slots once (tick) and dismiss inbox "
    "items. A job runs only when tick runs; a slot with an unknown outcome is resolved by the "
    "owner (job_resolve).",
)

JobArg = Annotated[
    dict[str, JsonValue],
    WithJsonSchema(_inline(TypeAdapter(JobBody).json_schema())),
]
JobId = RecordId


class JobPlanParams(Params):
    job: JobArg


@operation(
    "job_plan",
    JobPlanParams,
    "Preview a job: next due slots (UTC and local), DST behaviour, side effects and budget",
    tool="jobs_read",
    sub="plan",
    http="POST /api/v1/jobs/plan",
)
def job_plan(ctx: Ctx, p: JobPlanParams) -> Any:
    return ctx.service.job_plan(ctx.project_id, ctx.caller, p.job)


class NoParams(Params):
    pass


@operation(
    "job_list",
    NoParams,
    "Jobs with recent slots and the next due times",
    tool="jobs_read",
    sub="list",
)
def job_list(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.job_list(ctx.project_id, ctx.caller)


class JobIdParams(Params):
    job_id: JobId


@operation(
    "job_slots",
    JobIdParams,
    "Every recorded slot of one job: state, attempts, reason and result",
    tool="jobs_read",
    sub="slots",
    http="GET /api/v1/jobs/{job_id}/slots",
)
def job_slots(ctx: Ctx, p: JobIdParams) -> Any:
    return ctx.service.job_slots(ctx.project_id, ctx.caller, p.job_id)


class InboxParams(Params):
    include_dismissed: bool = False


@operation(
    "jobs_inbox",
    InboxParams,
    "Meaningful job findings and failures needing a decision",
    tool="jobs_read",
    sub="inbox",
)
def jobs_inbox(ctx: Ctx, p: InboxParams) -> Any:
    return ctx.service.inbox(ctx.project_id, ctx.caller, p.include_dismissed)


class JobAddParams(Params):
    job: JobArg
    request_id: RequestId | None = None


@operation(
    "job_add",
    JobAddParams,
    "Record a job; it runs only when tick runs",
    tool="jobs_manage",
    sub="add",
    http="POST /api/v1/jobs",
)
def job_add(ctx: Ctx, p: JobAddParams) -> Any:
    return ctx.service.job_add(ctx.project_id, ctx.caller, p.job, ctx.rid(p.request_id))


class JobEnabledParams(JobIdParams):
    base_rev: Annotated[int, Field(ge=1, le=MAX_INT)]
    enabled: bool
    request_id: RequestId | None = None


@operation(
    "job_set_enabled",
    JobEnabledParams,
    "Enable or disable a job; slots missed while disabled are not run",
    tool="jobs_manage",
    sub="set_enabled",
    http="POST /api/v1/jobs/{job_id}/enabled",
)
def job_set_enabled(ctx: Ctx, p: JobEnabledParams) -> Any:
    return ctx.service.job_set_enabled(
        ctx.project_id, ctx.caller, p.job_id, p.base_rev, p.enabled, ctx.rid(p.request_id)
    )


class JobCancelParams(JobIdParams):
    slot: Annotated[str, Field(min_length=1, max_length=40, description="Due time (UTC)")]


@operation(
    "job_cancel",
    JobCancelParams,
    "Cancel one slot (pending: skipped; running: cancellation requested)",
    tool="jobs_manage",
    sub="cancel",
    http="POST /api/v1/jobs/{job_id}/cancel",
)
def job_cancel(ctx: Ctx, p: JobCancelParams) -> Any:
    return ctx.service.job_cancel(ctx.project_id, ctx.caller, p.job_id, p.slot)


@operation(
    "jobs_tick",
    NoParams,
    "Run due slots and the event detectors once (bounded); nothing repeats on its own",
    tool="jobs_manage",
    sub="tick",
)
def jobs_tick(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.jobs_tick(ctx.project_id)


class DismissParams(Params):
    notification_id: Annotated[int, Field(ge=1, le=MAX_INT)]


@operation(
    "jobs_dismiss",
    DismissParams,
    "Dismiss an inbox item (inbox state only)",
    tool="jobs_manage",
    sub="dismiss",
)
def jobs_dismiss(ctx: Ctx, p: DismissParams) -> Any:
    return ctx.service.inbox_dismiss(ctx.project_id, ctx.caller, p.notification_id)
