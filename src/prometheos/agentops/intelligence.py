"""The intelligence layer on MCP and HTTP (A07, CP22.4; spec/04 "The intelligence layer";
decision 0094).

Agents may read the policy, results and evaluations, and run a task only where the standing
policy lets agents start runs. An agent's grant of the policy, or a lowering of a root's
sensitivity, becomes an approval request the owner decides (``approval_required``); an agent's
revocation of the policy answers ``policy_denied``. The paired hub owner acts directly.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from prometheos.agentops import Ctx, Params, RequestId, RootAlias, family, operation
from prometheos.domain.models import JsonObjectInput

family("intelligence_read", "read", "The intelligence layer: its policy, results and evaluations.")
family(
    "intelligence_manage",
    "write_new",  # each run or request makes new records (receipts, approval requests)
    "The intelligence layer: run a task inside the owner's standing policy; request a grant of "
    "the policy or a lower root sensitivity (the owner decides).",
)

TaskKind = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z_]+(\.[a-z_]+)?$")]


class NoParams(Params):
    pass


@operation(
    "ai_policy_show",
    NoParams,
    "The standing policy in force (or why none is), the key's health and the month's spend",
    tool="intelligence_read",
    sub="policy",
    http="GET /api/v1/ai/policy",
)
def ai_policy_show(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.ai_policy_show(ctx.project_id)


class ResultsParams(Params):
    task_kind: TaskKind | None = None
    scan_gen: int | None = Field(default=None, ge=1)
    # The layer's own answers: current task versions, the standing policy's models (any model
    # when none is in force), the newest sample per file, sample and model (decision 0104).
    current: bool = False
    limit: int = Field(default=200, ge=1, le=1000)


@operation(
    "ai_results",
    ResultsParams,
    "Stored task samples: subject, outcome, reasons, executor and served model",
    tool="intelligence_read",
    sub="results",
    http="GET /api/v1/ai/results",
)
def ai_results(ctx: Ctx, p: ResultsParams) -> Any:
    return ctx.service.ai_results(ctx.project_id, p.task_kind, p.limit, p.scan_gen, p.current)


@operation(
    "ai_evaluations",
    NoParams,
    "Measurements per task and model, and whether they confer trust",
    tool="intelligence_read",
    sub="evaluations",
    http="GET /api/v1/ai/evaluations",
)
def ai_evaluations(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.ai_evaluations(ctx.project_id)


@operation(
    "root_sensitivity_show",
    NoParams,
    "Each research root's sensitivity (restricted until the owner sets it)",
    tool="intelligence_read",
    sub="root_sensitivity",
    http="GET /api/v1/ai/root-sensitivity",
)
def root_sensitivity_show(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.ai_root_sensitivity_show(ctx.project_id)


class RunParams(Params):
    task_kind: TaskKind
    scan_gen: int = Field(ge=1, le=2**53 - 1)
    paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        min_length=1, max_length=500
    )
    dry_run: bool = False


@operation(
    "ai_run",
    RunParams,
    "Run a registered task over adoption files inside the owner's standing policy (agents only "
    "where it lets them); dry_run shows the calls and the upper cost and sends nothing",
    tool="intelligence_manage",
    sub="run",
    http="POST /api/v1/ai/run",
)
def ai_run(ctx: Ctx, p: RunParams) -> Any:
    return ctx.service.ai_run(ctx.project_id, ctx.caller, task_kind=p.task_kind,
                              scan_gen=p.scan_gen, paths=p.paths, dry_run=p.dry_run)  # fmt: skip


class MapSuggestParams(Params):
    task_kind: TaskKind = "map.link_claim_question"
    claim_ids: list[Annotated[str, Field(pattern=r"^claim_[0-9a-f-]{36}$")]] | None = Field(
        default=None, min_length=1, max_length=500
    )
    dry_run: bool = False


@operation(
    "ai_map_suggest",
    MapSuggestParams,
    "Run a map task inside the owner's standing policy (decision 0109): for claims that answer no "
    "question, propose which one they answer; proposals change nothing, and the owner places each "
    "claim. dry_run shows the calls and the upper cost and sends nothing",
    tool="intelligence_manage",
    sub="map_suggest",
    http="POST /api/v1/ai/map-suggest",
)
def ai_map_suggest(ctx: Ctx, p: MapSuggestParams) -> Any:
    return ctx.service.ai_map_suggest(ctx.project_id, ctx.caller, task_kind=p.task_kind,
                                      claim_ids=p.claim_ids, dry_run=p.dry_run)  # fmt: skip


class GrantParams(Params):
    policy: JsonObjectInput
    binding_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    request_id: RequestId | None = None


@operation(
    "ai_policy_grant",
    GrantParams,
    "Grant the standing policy; from an agent this is an approval request the owner decides",
    tool="intelligence_manage",
    sub="policy_grant",
    http="POST /api/v1/ai/policy/grant",
)
def ai_policy_grant(ctx: Ctx, p: GrantParams) -> Any:
    if not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id, ctx.caller, "ai_policy_grant",
            p.model_dump(mode="json", exclude={"request_id"}), p.request_id,
        )  # fmt: skip
    return ctx.service.ai_policy_grant(
        ctx.project_id, p.policy, p.binding_sha256, authority=ctx.authority
    )


@operation(
    "ai_policy_revoke",
    NoParams,
    "Revoke the standing policy (the owner's alone; an agent's attempt answers policy_denied)",
    tool="intelligence_manage",
    sub="policy_revoke",
    http="POST /api/v1/ai/policy/revoke",
)
def ai_policy_revoke(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.ai_policy_revoke(ctx.project_id, authority=ctx.authority)


class SensitivityParams(Params):
    root_alias: RootAlias
    sensitivity: Literal["normal", "restricted", "private"]
    request_id: RequestId | None = None


@operation(
    "root_sensitivity_set",
    SensitivityParams,
    "Set a research root's sensitivity: raising is open; lowering is an owner decision (from an "
    "agent an approval request)",
    tool="intelligence_manage",
    sub="root_sensitivity",
    http="POST /api/v1/ai/root-sensitivity",
)
def root_sensitivity_set(ctx: Ctx, p: SensitivityParams) -> Any:
    order = ("normal", "restricted", "private")
    roots = ctx.service.ai_root_sensitivity_show(ctx.project_id)["roots"]
    current = next((r for r in roots if r["root_alias"] == p.root_alias), None)
    lowering = current is not None and order.index(p.sensitivity) < order.index(
        current["sensitivity"]
    )
    if lowering and not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id, ctx.caller, "root_sensitivity_set",
            p.model_dump(mode="json", exclude={"request_id"}), p.request_id,
        )  # fmt: skip
    return ctx.service.ai_root_sensitivity_set(
        ctx.project_id, p.root_alias, p.sensitivity, authority=ctx.authority if lowering else None
    )


class AcceptParams(Params):
    task_kind: TaskKind
    scan_gen: int = Field(ge=1, le=2**53 - 1)
    subject: Annotated[str, Field(min_length=1, max_length=4096)]
    # The samples the owner chose (the hub's row); without them, the first unused ones.
    result_keys: list[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] | None = Field(
        default=None, max_length=9
    )
    request_id: RequestId | None = None


@operation(
    "ai_accept",
    AcceptParams,
    "Accept the layer's proposal for one file as proposed evidence (an owner action; from an "
    "agent an approval request)",
    tool="intelligence_manage",
    sub="accept",
    http="POST /api/v1/ai/accept",
)
def ai_accept(ctx: Ctx, p: AcceptParams) -> Any:
    # The exact samples are fixed before any approval request, so the plan the owner approves
    # names every sample that will be submitted (review cp22-a R6-F1). A resumed plan already
    # names its full set, which this leaves unchanged.
    keys = ctx.service.ai_accept_keys(
        ctx.project_id, p.task_kind, p.scan_gen, p.subject, p.result_keys
    )
    if not ctx.owner:
        ctx.service.approval_request(
            ctx.project_id, ctx.caller, "ai_accept",
            p.model_dump(mode="json", exclude={"request_id"}) | {"result_keys": keys},
            p.request_id,
        )  # fmt: skip
    return ctx.service.ai_accept(
        ctx.project_id, p.task_kind, p.scan_gen, p.subject, authority=ctx.authority,
        result_keys=keys,
    )  # fmt: skip
