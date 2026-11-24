"""Bounded model generation over the same task contract an external coding agent uses.

``preview`` is pure: it shows the policy, the route, exactly what would leave this computer
(with a digest), the estimated cost and every reason the call cannot go ahead. A run needs
the owner's capability bound to that digest (``check_run``, spent when the operation run is
created), prepares an ordinary persisted task first (so an external agent can always take it
over), reserves budget, makes one call, validates the reply through the task-submit gate
(schema, source locators, quantity bindings) and, only when that fails, makes at most one
repair call (``execute_run``, the stages of an operation run, decision 0017). An unknown
outcome is never repeated automatically.
Every failure leaves the notebook, context and the prepared task exactly as usable as before.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from prometheos.app import reads, tasks
from prometheos.domain.canonical import (
    CanonicalJSONError,
    canonical_sha256,
    dumps_canonical,
    loads_internal,
    loads_strict,
)
from prometheos.domain.ids import derived_request_id
from prometheos.domain.models import PrometheosError
from prometheos.providers import adapter as ad
from prometheos.providers import config as cfg
from prometheos.providers import ledger, secrets
from prometheos.providers import transport as tp
from prometheos.store import capabilities, db
from prometheos.store.project import ProjectStore

PURPOSES = tuple(tasks.RESULT_MODELS)
EXCERPT_CHARS = 1500
SYSTEM_SUFFIX = "\nReturn only one JSON object that matches the supplied JSON schema."


def _context_caller(include_restricted: bool) -> reads.Caller:
    return reads.Caller(actor="provider", scope="owner" if include_restricted else "agent")


def build(
    store: ProjectStore,
    config: cfg.ProviderConfig,
    *,
    purpose: str,
    meeting_id: str | None,
    context_budget: int,
    include_restricted: bool,
) -> tuple[tasks.Rendered, ad.GenerationRequest]:
    rendered = tasks.render(
        store,
        _context_caller(include_restricted),
        purpose=purpose,
        budget=context_budget,
        meeting_id=meeting_id,
    )
    schema = tasks.RESULT_MODELS[purpose].model_json_schema()
    request = ad.GenerationRequest(
        system=tasks.INSTRUCTIONS[purpose] + SYSTEM_SUFFIX,
        user=rendered.text,
        schema=schema,
        max_output_tokens=config.budget.max_output_tokens,
        timeout_seconds=config.budget.timeout_seconds,
    )
    return rendered, request


def request_identity(
    config: cfg.ProviderConfig,
    purpose: str,
    meeting_id: str | None,
    context_budget: int,
    include_restricted: bool,
) -> str:
    """Who/what/where of a request, without the volatile bits of the rendered context."""
    return canonical_sha256(
        {
            "purpose": purpose,
            "meeting_id": meeting_id,
            "context_budget": context_budget,
            "include_restricted": include_restricted,
            "model": config.model,
            "host": cfg.host_of(config.endpoint or ""),
        }
    )


def preview(
    store: ProjectStore,
    config: cfg.ProviderConfig,
    secret_store: secrets.SecretStore,
    *,
    purpose: str,
    meeting_id: str | None = None,
    context_budget: int = 12000,
    include_restricted: bool = False,
    transport: tp.Transport | None = None,
    reveal_hidden_count: bool = False,
) -> dict[str, Any]:
    """Pure egress preview. Reads the record; writes nothing; makes no request."""
    if purpose not in PURPOSES:
        raise PrometheosError("validation_error", "unknown purpose", {"purposes": list(PURPOSES)})
    blockers: list[str] = []
    if config.policy == "no-ai":
        blockers.append("the project's AI routing policy is no-ai: nothing is generated")
    if config.endpoint is None or config.model is None:
        blockers.append("no endpoint and model are configured")
    rendered, request = build(
        store,
        config,
        purpose=purpose,
        meeting_id=meeting_id,
        context_budget=context_budget,
        include_restricted=include_restricted,
    )
    characters = len(request.system) + len(request.user)
    if characters > config.budget.max_input_chars:
        blockers.append(
            f"the payload ({characters} characters) exceeds the input budget "
            f"({config.budget.max_input_chars}); lower the context budget"
        )
    if rendered.rank > 0 and not include_restricted:
        blockers.append("restricted records are included but egress was not requested")  # defensive
    health = secrets.health(secret_store, config.credential_ref)
    if config.credential_ref is not None and not health["present"]:
        blockers.append(f"no credential is stored for reference {config.credential_ref}")
    payload = None
    payload_sha = None
    if config.endpoint is not None and config.model is not None:
        provider = ad.ChatCompletionsProvider(
            config, secret_store, transport or tp.UrllibTransport()
        )
        payload = provider.payload(request)
        payload_sha = canonical_sha256(payload)
    input_tokens = ledger.estimate_tokens(characters)
    cost = (
        ledger.estimate_cost(config, input_tokens, config.budget.max_output_tokens)
        if payload is not None
        else None
    )
    identity = request_identity(config, purpose, meeting_id, context_budget, include_restricted)
    conn = store.connect(readonly=True)
    try:
        with db.read_snapshot(conn):
            usage = ledger.usage_summary(conn, config, full=reveal_hidden_count)
            unknown = ledger.unknown_unacknowledged(conn, identity)
    finally:
        conn.close()
    if unknown:
        blockers.append(
            "an earlier call for this exact request has an unknown outcome and may have been "
            "billed; acknowledge it to send again"
        )
    cap = config.budget.spend_cap_usd
    if (
        cap is not None
        and cost is not None
        and Decimal(usage["spent_estimate_usd"]) + cost > Decimal(cap)
    ):
        blockers.append("the estimated cost would take spending past the cap")
    sensitivity = ("normal", "restricted", "private")[rendered.rank]
    digest = canonical_sha256(
        {
            "op": "provider_run",
            "purpose": purpose,
            "meeting_id": meeting_id,
            "context_budget": context_budget,
            "include_restricted": include_restricted,
            "payload_sha256": payload_sha,
            "config": cfg.digest(config),
        }
    )
    return {
        "purpose": purpose,
        "meeting_id": meeting_id,
        "capability": "generation",
        "policy": config.policy,
        "route": config.route,
        "adapter": ad.ChatCompletionsProvider.name,
        "endpoint_host": cfg.host_of(config.endpoint or ""),
        "model": config.model,
        "inputs": {
            "records": len(rendered.refs),
            "sensitivity": sensitivity,
            "restricted_included": include_restricted and rendered.rank > 0,
            # Only the owner learns how many records were left out; an agent must not be able
            # to detect that restricted records exist (review cp6-a M2).
            "excluded_above_scope": rendered.excluded_hidden if reveal_hidden_count else None,
        },
        "transmits": {
            "characters": characters,
            "estimated_input_tokens": input_tokens,
            "max_output_tokens": config.budget.max_output_tokens,
            "payload_sha256": payload_sha,
            "excerpt": request.user[:EXCERPT_CHARS],
            "excerpt_truncated": len(request.user) > EXCERPT_CHARS,
        },
        "cost": {
            "estimate_upper_bound_usd": None if cost is None else ledger.money(cost),
            "label": ledger.COST_LABEL,
            "usage_so_far": usage,
        },
        "calls": {
            "max_per_run": config.budget.max_calls_per_run,
            "note": "one generation and at most one repair attempt; an unknown outcome is "
            "never repeated automatically",
        },
        "credential": health,
        "blockers": blockers,
        "unacknowledged_unknown_calls": unknown,
        "would_send": not blockers,
        "needs_egress_grant": include_restricted and rendered.rank > 0,
        "digest": digest,
        "fallback": "the same task stays available to an external coding agent "
        "(task prepare/submit); nothing else depends on this provider",
    }


def _fail(
    status: str,
    message: str,
    *,
    code: str,
    task_id: str | None,
    calls: list[str],
    extra: dict[str, Any] | None = None,
) -> PrometheosError:
    details: dict[str, Any] = {
        "status": status,
        "task_id": task_id,
        "calls": calls,
        "fallback": {
            "model_free": True,
            "task_id": task_id,
            "next": "an external coding agent can complete this task: prometheos task get / "
            "task submit; the notebook, search and context are unaffected",
        },
    }
    details.update(extra or {})
    return PrometheosError(code, message, details)


def _repair_text(message: str, errors: Any) -> str:
    detail = ""
    if errors:
        detail = " " + (json.dumps(errors)[:800] if not isinstance(errors, str) else errors[:800])
    return (
        f"Your previous output was rejected: {message}.{detail} "
        "Return corrected JSON only, citing only records and locators present in the context."
    )


def check_run(
    store: ProjectStore,
    config: cfg.ProviderConfig,
    secret_store: secrets.SecretStore,
    transport: tp.Transport,
    *,
    purpose: str,
    meeting_id: str | None,
    context_budget: int,
    include_restricted: bool,
    approved_digest: str,
    capability_secret: capabilities.Secret,
    egress_secret: capabilities.Secret,
    acknowledge_unknown: str | None = None,
) -> dict[str, Any]:
    """Everything that must hold before a provider run is created (decision 0017): the
    preview has no blocker, it is the reviewed one, the owner's capability (and the egress
    grant for restricted inputs) would be accepted, and the bytes to send are exactly the
    approved ones. Nothing is persisted. Returns the preview and the capabilities the run's
    creation spends, once, in the same transaction as the run row."""
    shown = preview(
        store,
        config,
        secret_store,
        purpose=purpose,
        meeting_id=meeting_id,
        context_budget=context_budget,
        include_restricted=include_restricted,
        transport=transport,
    )
    blockers = list(shown["blockers"])
    if acknowledge_unknown is not None:
        blockers = [b for b in blockers if "unknown outcome" not in b]
        if acknowledge_unknown not in shown["unacknowledged_unknown_calls"]:
            raise PrometheosError(
                "validation_error",
                "that call is not an unacknowledged unknown outcome for this request",
                {"unacknowledged": shown["unacknowledged_unknown_calls"]},
            )
    if blockers:
        code = "policy_denied" if config.policy == "no-ai" else "validation_error"
        raise PrometheosError(code, blockers[0], {"blockers": blockers, "route": shown["route"]})
    if approved_digest != shown["digest"]:
        raise PrometheosError(
            "revision_conflict",
            "what would be sent changed since it was reviewed; preview it again",
            {"digest": shown["digest"]},
        )
    # Authority is checked before anything is persisted: an unauthorised attempt leaves no run,
    # no task and no trace.
    consume = [("provider_run", shown["digest"], capability_secret)]
    if shown["needs_egress_grant"]:
        consume.append(("provider_egress", shown["transmits"]["payload_sha256"], egress_secret))
    conn = store.connect(readonly=True)
    try:
        for operation, digest, secret in consume:
            capabilities.verify(
                conn, secret=secret, project_id=store.project_id, operation=operation,
                digest=digest,
            )  # fmt: skip
    finally:
        conn.close()
    # The bytes that will be sent must be exactly the bytes whose digest the owner approved
    # (and, for restricted inputs, egress-granted): checked before any task is prepared, any
    # budget reserved or any capability spent (review cp6-a M1).
    if (
        payload_digest(
            store,
            config,
            secret_store,
            transport,
            purpose=purpose,
            meeting_id=meeting_id,
            context_budget=context_budget,
            include_restricted=include_restricted,
        )
        != shown["transmits"]["payload_sha256"]
    ):
        raise PrometheosError(
            "revision_conflict",
            "what would be sent changed after it was reviewed; nothing was sent, preview it again",
            {"digest": shown["digest"]},
        )
    return {"shown": shown, "consume": consume}


def spend(conn: Any, store: ProjectStore, checked: dict[str, Any]) -> None:
    """Spend the run's capabilities inside the transaction that creates the run."""
    for operation, digest, secret in checked["consume"]:
        capabilities.consume(
            conn, secret=secret, project_id=store.project_id, operation=operation, digest=digest
        )


def payload_digest(
    store: ProjectStore,
    config: cfg.ProviderConfig,
    secret_store: secrets.SecretStore,
    transport: tp.Transport,
    *,
    purpose: str,
    meeting_id: str | None,
    context_budget: int,
    include_restricted: bool,
) -> str:
    """Digest of the exact request body that would be sent now (no request is made)."""
    _rendered, request = build(
        store,
        config,
        purpose=purpose,
        meeting_id=meeting_id,
        context_budget=context_budget,
        include_restricted=include_restricted,
    )
    provider = ad.ChatCompletionsProvider(config, secret_store, transport)
    return canonical_sha256(provider.payload(request))


def task_request(
    store: ProjectStore, config: cfg.ProviderConfig, task_id: str, purpose: str
) -> ad.GenerationRequest:
    """The generation request of a prepared task, from its stored context (what ``build``
    rendered when the task was prepared)."""
    conn = store.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT context_text FROM task_contexts WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise PrometheosError("not_found", "the run's prepared task is gone", {"id": task_id})
    return ad.GenerationRequest(
        system=tasks.INSTRUCTIONS[purpose] + SYSTEM_SUFFIX,
        user=str(row[0]),
        schema=tasks.RESULT_MODELS[purpose].model_json_schema(),
        max_output_tokens=config.budget.max_output_tokens,
        timeout_seconds=config.budget.timeout_seconds,
    )


def payload_digest_from_task(
    store: ProjectStore,
    config: cfg.ProviderConfig,
    secret_store: secrets.SecretStore,
    transport: tp.Transport,
    *,
    task_id: str,
    purpose: str,
) -> str:
    """Digest of the request a prepared task sends (no request is made)."""
    provider = ad.ChatCompletionsProvider(config, secret_store, transport)
    return canonical_sha256(provider.payload(task_request(store, config, task_id, purpose)))


def run_configuration(config: cfg.ProviderConfig) -> dict[str, Any]:
    """The configuration a provider run's resume compares: everything but the spending cap and
    prices, which the owner may raise while a run waits at its budget (decision 0017)."""
    document: dict[str, Any] = config.model_dump(mode="json")
    document.pop("price_input_per_mtok_usd", None)
    document.pop("price_output_per_mtok_usd", None)
    document["budget"] = {k: v for k, v in document["budget"].items() if k != "spend_cap_usd"}
    return document


def execute_run(
    ctx: Any,
    store: ProjectStore,
    config: cfg.ProviderConfig,
    secret_store: secrets.SecretStore,
    transport: tp.Transport,
    *,
    actor: str,
) -> dict[str, Any]:
    """One bounded generation as the stages of an operation run (decision 0017).

    ``task`` prepares the persisted task (under a key derived from the run, so a resume finds
    it); each attempt is ``call:N`` (reserve within the spending cap, mark the external call
    started, send once, record the outcome) and ``submit:N`` (validate through the task gate).
    A completed stage is never repeated: a run paused at the cap resumes without preparing
    again or re-sending (and re-charging) an earlier call. An unknown outcome stops the run as
    ``unknown_external``; it is never sent again automatically."""
    from prometheos.app import runs

    params = ctx.params
    purpose = str(params["purpose"])
    meeting_id = params["meeting_id"]
    context_budget = int(params["context_budget"])
    include_restricted = bool(params["include_restricted"])
    acknowledge_unknown = params.get("acknowledge_unknown")
    provider = ad.ChatCompletionsProvider(config, secret_store, transport)
    task_out = ctx.output("task")
    if task_out is not None:
        # The task is prepared: rebuild the exact request from its stored, immutable context
        # (the task's commit moved the project on; a fresh render would differ; round 2, F2).
        request = task_request(store, config, str(task_out["task_id"]), purpose)
        refs = list(task_out.get("refs", []))
        rendered_text = request.user
    else:
        rendered, request = build(
            store,
            config,
            purpose=purpose,
            meeting_id=meeting_id,
            context_budget=context_budget,
            include_restricted=include_restricted,
        )
        refs = [r["id"] for r in rendered.refs][:200]
        rendered_text = rendered.text
    payload_sha = canonical_sha256(provider.payload(request))
    # Recheck at execution, every time: exactly the approved bytes, or nothing is sent.
    if payload_sha != ctx.run.input_digests[0].sha256:
        raise PrometheosError(
            "revision_conflict",
            "what would be sent changed after it was reviewed; nothing was sent, preview it again",
            {"run_id": ctx.run.id},
        )
    caller = _context_caller(include_restricted)
    if task_out is None:
        ctx.checkpoint("task")
        task = tasks.prepare(
            store,
            purpose=purpose,
            actor=actor,
            budget=context_budget,
            meeting_id=meeting_id,
            request_id=derived_request_id("provider-task", store.project_id, ctx.run.id),
            caller=caller,
        )
        if task["context"] != rendered_text and not task["commit"].get("replayed"):
            raise _fail(
                "context_changed",
                "the record changed while preparing the task; preview it again",
                code="revision_conflict",
                task_id=task["task_id"],
                calls=[],
            )
        task_out = {"task_id": task["task_id"], "refs": refs}
        ctx.complete("task", task_out)
    task_id = str(task_out["task_id"])
    run_key = derived_request_id("provider-run", store.project_id, ctx.run.id)
    host = cfg.host_of(config.endpoint or "")
    submit_rank = 2 if include_restricted else 0
    calls: list[str] = []
    follow_up: tuple[tuple[str, str], ...] = ()
    last_error: dict[str, Any] | None = None
    for attempt in range(1, config.budget.max_calls_per_run + 1):
        call = ctx.output(f"call:{attempt}")
        if call is None:
            ctx.checkpoint(f"call {attempt}")
            attempt_request = ad.GenerationRequest(
                request.system,
                request.user,
                request.schema,
                request.max_output_tokens,
                request.timeout_seconds,
                follow_up,
            )
            characters = (
                len(request.system) + len(request.user) + sum(len(c) for _r, c in follow_up)
            )
            reserved = ledger.estimate_cost(
                config, ledger.estimate_tokens(characters), config.budget.max_output_tokens
            )
            try:
                call_id = ledger.reserve(
                    store,
                    config,
                    run_key=run_key,
                    purpose=purpose,
                    route=config.route,
                    host=host,
                    attempt=attempt,
                    input_digest=payload_sha,
                    request_identity=request_identity(
                        config, purpose, meeting_id, context_budget, include_restricted
                    ),
                    input_refs=refs,
                    task_id=task_id,
                    max_rank=submit_rank,
                    reserved_cost=reserved,
                    acknowledges=acknowledge_unknown if attempt == 1 else None,
                )
            except PrometheosError as exc:
                if exc.code == "budget_exceeded":
                    ledger.refuse(
                        store, run_key=run_key, purpose=purpose, route=config.route, host=host,
                        model=config.model or "", attempt=attempt, input_digest=payload_sha,
                        reason=exc.message, max_rank=submit_rank,
                    )  # fmt: skip
                    error = _fail(
                        "budget_exceeded", exc.message, code="budget_exceeded", task_id=task_id,
                        calls=calls, extra={"budget": exc.details, "run_id": ctx.run.id},
                    )  # fmt: skip
                    raise runs.BudgetPause(
                        f"paused before call {attempt}: {exc.message}", error=error
                    ) from exc
                raise
            ctx.external_started(f"provider {host or 'endpoint'}"[:200], call_id, payload_sha)
            ledger.mark(store, call_id, "sent")
            try:
                result = provider.generate(attempt_request)
            except ad.ProviderFailure as failure:
                outcome = _record_failure(store, call_id, failure)
                error = _fail(
                    outcome["status"], outcome["message"], code=outcome["code"], task_id=task_id,
                    calls=[*calls, call_id], extra={"route": config.route, "attempts": attempt},
                )  # fmt: skip
                if failure.kind == "unknown_outcome":
                    raise runs.ExternalUncertain(
                        f"call {attempt} has an unknown outcome ({failure.detail[:200]}); it is "
                        "not repeated automatically",
                        error=error,
                    ) from None
                ctx.external_confirmed(f"call {attempt} failed: {outcome['status']}")
                raise error from None
            actual = _actual_cost(config, result.usage)
            ledger.mark(
                store,
                call_id,
                "succeeded",
                usage=json.dumps(
                    {
                        "prompt_tokens": result.usage.prompt_tokens,
                        "completion_tokens": result.usage.completion_tokens,
                    }
                ),
                actual_cost=actual[0],
                cost_basis=actual[1],
                output_digest=canonical_sha256({"text": result.text}),
            )
            call = {
                "call_id": call_id,
                "text": result.text,
                "finish_reason": result.finish_reason,
                "model_id": result.model_id,
                "usage": {
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                },
                "cost": {"usd": actual[0], "basis": actual[1]},
                "receipt": result.receipt,
            }
            charged = actual[0] or (ledger.money(reserved) if reserved is not None else None)
            ctx.complete(f"call:{attempt}", call, charged)
            ctx.external_confirmed(f"call {attempt} answered")
        calls.append(str(call["call_id"]))
        submitted = ctx.output(f"submit:{attempt}")
        if submitted is None:
            ctx.checkpoint(f"submit {attempt}")
            submitted = _submit(store, call, task_id, run_key, attempt, config, submit_rank, calls)
            ctx.complete(f"submit:{attempt}", submitted)
        if "commit" in submitted:
            committed = submitted["commit"]
            ctx.artifact(task_id, 1, "task")
            for item in committed.get("results", []):
                ctx.artifact(str(item["id"]), int(item["rev"]), "proposal")
            ctx.result_seq = int(committed["project_seq"])
            return {
                "ok": True,
                "status": "completed",
                "task_id": task_id,
                "attempts": attempt,
                "repaired": attempt > 1,
                "commit": committed,
                "route": config.route,
                "policy": config.policy,
                "endpoint_host": host,
                "model_id": call["model_id"],
                "usage": call["usage"],
                "cost": {
                    "usd": call["cost"]["usd"],
                    "basis": call["cost"]["basis"],
                    "label": ledger.COST_LABEL,
                },
                "receipt": call["receipt"],
                "calls": calls,
            }
        last_error = submitted
        follow_up = (
            ("assistant", str(call["text"])),
            ("user", _repair_text(str(submitted["message"]), submitted.get("errors"))),
        )
    assert last_error is not None
    raise _fail(
        "invalid_output",
        "the provider's reply did not pass validation after one repair attempt; nothing was "
        "recorded: " + str(last_error["message"]),
        code="validation_error",
        task_id=task_id,
        calls=calls,
        extra={"attempts": len(calls), "validation": last_error.get("details") or {}},
    )


def _submit(
    store: ProjectStore,
    call: dict[str, Any],
    task_id: str,
    run_key: str,
    attempt: int,
    config: cfg.ProviderConfig,
    submit_rank: int,
    calls: list[str],
) -> dict[str, Any]:
    """Validate a reply through the task gate: ``{"commit": ...}`` when it was recorded, or
    ``{"message", "errors", "details"}`` when a repair attempt may fix it."""
    text = str(call["text"])
    try:
        try:
            loads_strict(text)
        except (CanonicalJSONError, ValueError) as exc:
            raise PrometheosError(
                "validation_error", "the reply is not strict JSON", {"errors": str(exc)[:200]}
            ) from None
        if call["finish_reason"] == "length":
            raise PrometheosError(
                "validation_error", "the reply was cut off at the output limit", {}
            )
        committed = tasks.submit(
            store,
            task_id,
            text.encode("utf-8"),
            derived_request_id("provider-submit", store.project_id, run_key, str(attempt)),
            f"provider:{config.model}"[:80],
            max_rank=submit_rank,
        )
    except PrometheosError as exc:
        if exc.code in ("stale_task", "policy_denied", "busy", "io_error"):
            raise _fail(
                "rejected", exc.message, code=exc.code, task_id=task_id, calls=calls
            ) from exc
        errors = exc.details.get("errors")
        return {
            "message": exc.message,
            "errors": (
                json.dumps(errors)[:800] if errors and not isinstance(errors, str) else errors
            ),
            "details": loads_internal(dumps_canonical(exc.details))
            if len(dumps_canonical(exc.details)) <= 16_000
            else {},
        }
    return {"commit": committed}


def _actual_cost(config: cfg.ProviderConfig, usage: ad.Usage) -> tuple[str | None, str]:
    if usage.prompt_tokens is not None and usage.completion_tokens is not None:
        cost = ledger.estimate_cost(config, usage.prompt_tokens, usage.completion_tokens)
        if cost is not None:
            return ledger.money(cost), "reported_usage_times_entered_price"
    return None, "estimate_upper_bound"


def _record_failure(
    store: ProjectStore, call_id: str, failure: ad.ProviderFailure
) -> dict[str, str]:
    if failure.kind in ("not_sent", "no_credential"):
        ledger.mark(
            store, call_id, "failed", error=failure.detail[:300], actual_cost="0.000000",
            cost_basis="nothing_was_sent",
        )  # fmt: skip
        status = "credential_missing" if failure.kind == "no_credential" else "provider_unavailable"
        return {"status": status, "message": failure.detail, "code": "source_unavailable"}
    if failure.kind == "unknown_outcome":
        ledger.mark(store, call_id, "unknown", error=failure.detail[:300])
        return {
            "status": "unknown_outcome",
            "message": failure.detail
            + "; it is not repeated automatically and its cost stays reserved",
            "code": "source_unavailable",
        }
    if (
        failure.kind == "http_error"
        and failure.status is not None
        and (failure.status < 500 or failure.status == 429)
    ):
        # A rejected request (bad key, rate limit, bad request) was not processed or billed.
        ledger.mark(
            store, call_id, "failed", error=failure.detail[:300], actual_cost="0.000000",
            cost_basis="rejected_by_provider",
        )  # fmt: skip
        return {
            "status": "provider_rejected",
            "message": failure.detail,
            "code": "source_unavailable",
        }
    # A server error or an unusable reply: whether work was billed is unknown, so the
    # reservation stays as the cost estimate.
    ledger.mark(store, call_id, "failed", error=failure.detail[:300])
    return {"status": "provider_error", "message": failure.detail, "code": "source_unavailable"}
