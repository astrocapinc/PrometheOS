"""The owner's standing policy for the intelligence layer (A07.4; spec/03 (u); decision 0092).

The policy names, per task kind, the evaluated (model, provider) pairs a run may use in order,
and for the whole project the sensitivity ceiling (normal only), the data policy (zero data
retention, data collection denied), the per-run cap, the home's monthly cap and whether agents
may start runs. It is granted, changed and revoked only by the owner, bound to its own digest
and to the provider configuration revision it was granted on: a changed endpoint, key reference
or any field voids it until it is granted again.

``check_run`` is the one gate a run passes before any byte leaves: restricted or private input
answers ``policy_denied``; a run outside the policy (no policy in force, a voided one, a task kind
it does not name, an agent without its leave) raises ``OutsidePolicy``, which the service turns
into ``approval_required`` with the CP6.3 per-call preview (an approval request it stores).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError, StrictModel
from prometheos.providers.config import DecimalText, ProviderConfig, host_of

OPENROUTER_HOST = "openrouter.ai"
ModelId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
Parameter = Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,40}$")]
SENSITIVITY_RANK = {"normal": 0, "restricted": 1, "private": 2}
# Router models, moving aliases and free tiers are refused for project content (A07.5).
_ROUTER = re.compile(r"^(openrouter/|~)|(:free|-latest)$|/auto$")


class EvaluatedModel(StrictModel):
    """One (model, provider) pair and the stored, dated metadata its request profile uses."""

    model: ModelId
    provider: Slug
    company: Slug
    supported_parameters: list[Parameter] = Field(max_length=60)
    lowest_effort: Literal["none", "minimal", "low"] | None
    price_input_per_mtok_usd: DecimalText
    price_output_per_mtok_usd: DecimalText
    metadata_as_of: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]

    @model_validator(mode="after")
    def _not_a_router(self) -> EvaluatedModel:
        if _ROUTER.search(self.model):
            raise ValueError(f"{self.model}: router models, aliases and free tiers are refused")
        return self


class TaskGrant(StrictModel):
    task_kind: Annotated[str, StringConstraints(pattern=r"^[a-z_]+\.[a-z_]+$|^[a-z_]+$")]
    models: list[EvaluatedModel] = Field(min_length=1, max_length=4)
    single_company_reason: Annotated[str, StringConstraints(min_length=10, max_length=300)] | None
    task_ceiling_usd: DecimalText

    @model_validator(mode="after")
    def _two_companies(self) -> TaskGrant:
        companies = {m.company for m in self.models}
        if len(companies) < 2 and self.single_company_reason is None:
            raise ValueError("the evaluated list spans one company: say why (A07.5)")
        pairs = [(m.model, m.provider) for m in self.models]
        if len(set(pairs)) != len(pairs):
            raise ValueError("a (model, provider) pair is listed twice")
        return self


class StandingPolicy(StrictModel):
    tasks: list[TaskGrant] = Field(min_length=1, max_length=40)
    sensitivity_ceiling: Literal["normal"]
    data_policy: Literal["zdr_deny_collection"]
    run_cap_usd: DecimalText
    monthly_cap_usd: DecimalText
    agents_may_run: bool

    @model_validator(mode="after")
    def _caps(self) -> StandingPolicy:
        if Decimal(self.run_cap_usd) <= 0 or Decimal(self.monthly_cap_usd) <= 0:
            raise ValueError("both caps must be set above zero (A07.4)")
        if Decimal(self.run_cap_usd) > Decimal(self.monthly_cap_usd):
            raise ValueError("the per-run cap is above the monthly cap")
        kinds = [t.task_kind for t in self.tasks]
        if len(set(kinds)) != len(kinds):
            raise ValueError("a task kind is listed twice")
        return self

    def grant_for(self, task_kind: str) -> TaskGrant | None:
        return next((t for t in self.tasks if t.task_kind == task_kind), None)


class OutsidePolicy(Exception):
    """The run is outside the standing policy; the owner decides it per call."""

    def __init__(self, reason: str, message: str, task_kind: str) -> None:
        super().__init__(message)
        self.reason, self.message, self.task_kind = reason, message, task_kind


def policy_sha256(policy: StandingPolicy) -> str:
    return canonical_sha256(policy.model_dump(mode="json"))


def binding(policy: StandingPolicy, provider_revision: int) -> str:
    """What an owner grant is bound to: the policy's digest and the provider revision."""
    return canonical_sha256(
        {"policy_sha256": policy_sha256(policy), "provider_revision": provider_revision}
    )


def openrouter_configured(config: ProviderConfig) -> bool:
    return (
        config.policy == "remote-allowed"
        and config.endpoint is not None
        and host_of(config.endpoint) == OPENROUTER_HOST
        and config.credential_ref is not None
    )


def check_run(
    granted: dict[str, Any] | None,
    config: ProviderConfig,
    provider_revision: int,
    *,
    task_kind: str,
    sensitivity: str,
    agent: bool,
) -> TaskGrant:
    """The gate before a remote task run. ``granted`` is the stored policy row in force
    (``policy``, ``provider_revision``) or None. Answers the task's grant or raises."""
    if SENSITIVITY_RANK.get(sensitivity, 2) > SENSITIVITY_RANK["normal"]:
        raise PrometheosError(
            "policy_denied",
            "restricted or private input never leaves this computer",
            {"sensitivity": sensitivity},
        )
    if granted is None:
        raise OutsidePolicy(
            "no_policy", "no standing policy is in force for the intelligence layer", task_kind
        )
    if int(granted["provider_revision"]) != provider_revision or not openrouter_configured(config):
        raise OutsidePolicy(
            "policy_void",
            "the provider configuration changed since the policy was granted; grant it again",
            task_kind,
        )
    policy = StandingPolicy.model_validate(granted["policy"])
    grant = policy.grant_for(task_kind)
    if grant is None:
        raise OutsidePolicy(
            "task_not_granted", f"the standing policy does not cover {task_kind}", task_kind
        )
    if agent and not policy.agents_may_run:
        raise OutsidePolicy(
            "agents_not_allowed", "the standing policy does not let agents start runs", task_kind
        )
    return grant
