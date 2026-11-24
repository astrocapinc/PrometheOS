"""Agent-reachable operations added for parity completion (A02, CP10.6; decision 0022).

CP10.1-CP10.5 gave the earlier operations one hand-written MCP tool and HTTP route each.
CP10.6 closes the rest of the registry (skills, jobs, papers, administration, recording,
provider extras, version text and content, notebook export...) from one table, so the two
agent transports cannot drift:

- an :class:`AgentOp` has a closed, typed parameter model and one handler that calls the same
  application service the CLI calls;
- the MCP adapter serves the operations as *operation-family tools*: one tool per family and
  effect class (``skills_read``, ``skills_manage``, ``jobs_read``...) whose single argument
  ``request`` is a discriminated union on ``op`` (each member the exact parameters of one
  sub-operation). The registry names a sub-operation ``<tool>#<op>``;
- the hub installs the route the registry declares for the operation (``AgentOp.http``) when it
  is not hand-written, with the same parameter model; a body is validated strictly, a query
  string leniently, and a failure is the same ``validation_error`` on both transports.

Rules every handler follows:

- the caller is the transport's caller (agent scope on MCP and for agent tokens; the paired
  owner on the hub's session); nothing here builds an :class:`OwnerAct`;
- a file or folder operand is a research root alias plus a path inside it
  (:func:`research_path`), resolved without following links; an absolute path is never accepted
  and a reply names files by root alias and relative path;
- a result that can be large is paged by a cursor-like ``offset``/``limit`` and says where the
  next page starts; generated artifacts are written inside a research root and read back with
  ``version_content``/``version_text`` or the owning family's bounded read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError, create_model

from prometheos.app.reads import Caller
from prometheos.domain.canonical import dumps_canonical
from prometheos.domain.ids import RECORD_ID_PATTERN, new_request_id
from prometheos.domain.models import PrometheosError, StrictModel
from prometheos.sources.paths import resolve_in_root

Effect = Literal["read", "write", "write_new", "destructive"]
"""Which MCP annotations a family tool carries (a tool's annotations must hold for every
sub-operation, so a family is split into a read-only tool and a changing tool)."""

UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
MAX_PAGE_BYTES = 256 * 1024
MAX_PAGE_CHARS = 100_000


class Params(BaseModel):
    """Base of every parameter model: closed, frozen, no coercion unless a query string."""

    model_config = ConfigDict(extra="forbid", frozen=True)


RecordId = Annotated[str, Field(pattern=RECORD_ID_PATTERN, max_length=80)]
RequestId = Annotated[str, Field(pattern=UUID4, description="Canonical lowercase UUID4")]
RootAlias = Annotated[str, Field(min_length=1, max_length=64)]
RelPath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=1024,
        description="Path inside the research root (no drive, no '..', no absolute path)",
    ),
]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Offset = Annotated[int, Field(ge=0, le=2**40)]


@dataclass
class Ctx:
    """Who asks and through what: the transport's service view and caller."""

    service: Any
    project_id: str
    caller: Caller
    owner: bool = False  # the paired hub owner; always False on MCP and for agent tokens
    transport: str = "mcp"
    authority: object = None  # authenticated paired owner only

    def rid(self, given: str | None) -> str:
        return given or new_request_id()


@dataclass(frozen=True)
class AgentOp:
    id: str  # the registry operation id
    params: type[Params]
    run: Callable[[Ctx, Any], Any]
    summary: str
    tool: str | None = None  # the MCP family tool; None when the MCP route already exists
    sub: str | None = None  # the sub-operation name inside that tool
    http: str | None = None  # ``"METHOD /api/v1/..."`` installed by the hub; None: hand-written

    @property
    def mcp_route(self) -> str | None:
        return f"{self.tool}#{self.sub}" if self.tool and self.sub else None


@dataclass(frozen=True)
class FamilyTool:
    name: str
    effect: Effect
    description: str
    ops: list[str] = field(default_factory=list)


OPS: dict[str, AgentOp] = {}
FAMILIES: dict[str, FamilyTool] = {}


def family(name: str, effect: Effect, description: str) -> None:
    FAMILIES[name] = FamilyTool(name, effect, description)


def operation(
    op_id: str,
    params: type[Params],
    summary: str,
    *,
    tool: str | None = None,
    sub: str | None = None,
    http: str | None = None,
) -> Callable[[Callable[[Ctx, Any], Any]], Callable[[Ctx, Any], Any]]:
    """Register a handler. ``tool``/``sub`` put it in an MCP family tool."""

    def wrap(run: Callable[[Ctx, Any], Any]) -> Callable[[Ctx, Any], Any]:
        if op_id in OPS:
            raise ValueError(f"agent operation registered twice: {op_id}")
        OPS[op_id] = AgentOp(op_id, params, run, summary, tool, sub, http)
        if tool is not None:
            FAMILIES[tool].ops.append(op_id)
        return run

    return wrap


# ----- validation shared by MCP and HTTP ------------------------------------------------------


def validation_error(exc: ValidationError) -> PrometheosError:
    errors = [
        {"loc": "/".join(str(x) for x in e.get("loc", ())), "msg": str(e.get("msg", ""))}
        for e in exc.errors()[:20]
    ]
    return PrometheosError("validation_error", "request parameters are invalid", {"errors": errors})


def parse(op: AgentOp, data: Any, *, strict: bool = True) -> Params:
    """The operation's parameters. A JSON body (MCP arguments, an HTTP body) is validated
    strictly; a query string (every value text) leniently."""
    if not isinstance(data, dict):
        raise PrometheosError("validation_error", "request parameters must be an object", {})
    try:
        return op.params.model_validate(data, strict=strict)
    except ValidationError as exc:
        raise validation_error(exc) from exc


def execute(op: AgentOp, ctx: Ctx, data: Any, *, strict: bool = True) -> dict[str, Any]:
    """Validate, run and return a JSON object (a list result is wrapped by its handler)."""
    value = op.run(ctx, parse(op, data, strict=strict))
    result: dict[str, Any] = json.loads(dumps_canonical(value))
    return result


# ----- the MCP family tools --------------------------------------------------------------------


def sub_models(tool: str) -> list[type[BaseModel]]:
    """One request model per sub-operation: the operation's parameters plus ``op``."""
    models: list[type[BaseModel]] = []
    for op_id in FAMILIES[tool].ops:
        a = OPS[op_id]
        assert a.sub is not None
        models.append(
            create_model(
                f"{tool}_{a.sub}".title().replace("_", ""),
                __base__=a.params,
                op=(Literal[a.sub], Field(description=a.summary)),
            )
        )
    return models


def request_adapter(tool: str) -> Any:
    """Validator and JSON Schema source for a family tool's ``request`` argument: a union of
    the sub-operations discriminated by ``op`` (one member: just that model)."""
    from typing import Union

    from pydantic import TypeAdapter

    members = tuple(sub_models(tool))
    if len(members) == 1:
        return TypeAdapter(members[0])
    return TypeAdapter(Annotated[Union[members], Field(discriminator="op")])  # noqa: UP007


def family_output(tool: str) -> type[BaseModel]:
    from typing import Union

    from prometheos.agentops.outputs import result_model

    members = tuple(
        create_model(
            f"{tool}_{OPS[o].sub}_reply".title().replace("_", ""),
            __base__=StrictModel,
            op=(Literal[OPS[o].sub], ...),
            result=(result_model(o), ...),
        )
        for o in FAMILIES[tool].ops
    )
    if len(members) == 1:
        return members[0]
    union = Annotated[Union[members], Field(discriminator="op")]  # type: ignore[valid-type]  # noqa: UP007
    # Every member is an object, so the root says so: MCP protocol versions before 2026-07-28
    # require an output schema whose root has "type": "object", and without it tools/list
    # failed as a whole for those clients (dogfood P15).
    config = ConfigDict(json_schema_extra=_object_root)
    name = f"{tool}_out".title().replace("_", "")
    return type(name, (RootModel[union],), {"model_config": config, "__module__": __name__})


def _object_root(schema: dict[str, Any]) -> None:
    schema["type"] = "object"


def dispatch(tool: str, ctx: Ctx, request: Any) -> dict[str, Any]:
    """Run the sub-operation a family tool's ``request`` names."""
    if not isinstance(request, dict) or not isinstance(request.get("op"), str):
        raise PrometheosError("validation_error", "request needs an 'op' member", {})
    sub = request["op"]
    chosen = next((OPS[i] for i in FAMILIES[tool].ops if OPS[i].sub == sub), None)
    if chosen is None:
        raise PrometheosError(
            "validation_error",
            "unknown operation for this tool",
            {"op": sub[:80], "operations": [OPS[i].sub for i in FAMILIES[tool].ops]},
        )
    data = {k: v for k, v in request.items() if k != "op"}
    return {"op": sub, "result": execute(chosen, ctx, data)}


# ----- research-root paths ---------------------------------------------------------------------


def research_path(ctx: Ctx, alias: str, relative: str | None = None) -> tuple[Any, str]:
    """A path inside an approved research root: ``(absolute path, canonical relative)``. The
    root must be one the researcher approved for this project; nothing else is reachable."""
    root = ctx.service.research_root(ctx.project_id, alias)
    if relative is None:
        return root, ""
    resolved = resolve_in_root(root, relative)
    return resolved.path, resolved.relative


def labelled(alias: str, relative: str) -> dict[str, str]:
    """How a reply names a file: root alias and relative path, never an absolute path."""
    return {"root_alias": alias, "path": relative}


def page(total: int, offset: int, limit: int) -> tuple[int, int | None]:
    """``(end, next_offset)`` of one page of ``total`` units."""
    end = min(total, offset + limit)
    return end, (end if end < total else None)


def load() -> None:
    """Import every family module (registration happens at import)."""
    from prometheos.agentops import (  # noqa: F401
        admin,
        adoption,
        audio,
        intelligence,
        jobs,
        library,
        matrix,
        papers,
        records,
        skills,
    )
