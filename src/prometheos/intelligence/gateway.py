"""The gateway: OpenRouter as the layer's first remote route (A07.5, A07.6, A07.9; decision 0092).

It generalises the CP6.3 adapter's rules (one documented wire format, the transport as the only
code that opens a socket, no automatic repeat of an unknown outcome) for typed tasks:

- **Request defaults.** Every request carries ``provider: {only: [the pair's provider],
  allow_fallbacks: false, data_collection: "deny", zdr: true, require_parameters: true}``, a
  strict JSON schema (the task's wire schema), ``stream: false`` and ``usage: {include: true}``.
- **Per-model profiles** from the policy's stored metadata: ``temperature: 0`` and a seed only
  where the model lists them, the lowest reasoning effort the model allows.
- **Fallback inside the evaluated pairs**, in the policy's order, one call per pair, each with its
  own reservation. A pair the network never reached, or that answered with no eligible provider
  or a server error, gives way to the next; nothing is ever retried without zero retention.
- **Batch stops.** 402 and 403 (the key's own limit or credit) and 429 (rate limited) stop the
  batch cleanly; a reached cap answers ``budget_exceeded`` before anything is sent; a timeout or
  a reset after sending holds the reservation and is never repeated.
- **What is recorded.** The served model and provider (an output from outside the evaluated list
  is a hint only), the billed cost when the reply reports it (else the estimate, labelled), the
  data policy sent, the request's digest and the reply body itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

from prometheos.domain.canonical import canonical_sha256, loads_strict
from prometheos.providers import transport as tp
from prometheos.providers.config import ProviderConfig, host_of
from prometheos.providers.secrets import SecretStore

from . import spend
from .policy import EvaluatedModel, TaskGrant

DATA_POLICY = {"data_collection": "deny", "zdr": True, "require_parameters": True}
SEED = 7
PER_CALL_PROVIDER = "any"  # a run the owner approved per call, outside the policy
TIMEOUT_SECONDS = 60.0


class BatchStop(Exception):
    """The batch ends cleanly here; finished items are kept, nothing more is sent."""

    def __init__(self, reason: str, detail: str, retry_after: str | None = None) -> None:
        super().__init__(detail)
        self.reason = reason  # key_limit | rate_limited | no_route | no_credential | invalid_reply
        self.detail = detail
        self.retry_after = retry_after


class OverBudget(Exception):
    """A reply billed above its upper estimate: kept, and the batch stops."""

    def __init__(self, reply: GatewayReply) -> None:
        super().__init__("a reply was billed above its upper estimate")
        self.reply = reply


class UnknownOutcome(Exception):
    """The request may have been received and billed; it is held and never repeated."""

    def __init__(self, pair: EvaluatedModel, request_sha256: str, reservation: str) -> None:
        super().__init__(f"the outcome of the call to {pair.model} is unknown")
        self.pair, self.request_sha256, self.reservation = pair, request_sha256, reservation


@dataclass(frozen=True)
class GatewayReply:
    text: str
    pair: EvaluatedModel
    served_model: str
    served_provider: str | None
    in_list: bool
    finish_reason: str | None
    billed: Decimal | None
    estimate: Decimal
    usage: dict[str, Any]
    receipt: dict[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


def request_body(
    pair: EvaluatedModel, system: str, user: str, wire: dict[str, Any], max_tokens: int
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": pair.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": wire},
        },
        # A reasoning model's thinking is billed inside max_tokens: it gets the doubled budget
        # the estimate already counts, or it may spend the whole answer on reasoning (0103).
        "max_tokens": max_tokens * (2 if pair.lowest_effort is not None else 1),
        "stream": False,
        # A per-call run outside the policy (provider "any") is not pinned to a provider, but
        # zero retention, denied data collection and required parameters still hold.
        "provider": (
            {**DATA_POLICY}
            if pair.provider == PER_CALL_PROVIDER
            else {"only": [pair.provider], "allow_fallbacks": False, **DATA_POLICY}
        ),
        "usage": {"include": True},
    }
    listed = set(pair.supported_parameters)
    if "temperature" in listed:
        body["temperature"] = 0
    if "seed" in listed:
        body["seed"] = SEED
    if pair.lowest_effort is not None and "reasoning" in listed:
        body["reasoning"] = {"effort": pair.lowest_effort}
    return body


def estimate_cost(pair: EvaluatedModel, input_chars: int, max_tokens: int) -> Decimal:
    """An upper bound of one call: every input character counted as a token (no tokenizer
    yields more tokens than characters, review cp22-a F9) and the whole output budget, doubled
    for a model that reasons (its reasoning may be billed beside the answer)."""
    output = max_tokens * (2 if pair.lowest_effort is not None else 1)
    cost = (
        Decimal(input_chars) * Decimal(pair.price_input_per_mtok_usd)
        + Decimal(output) * Decimal(pair.price_output_per_mtok_usd)
    ) / Decimal(1_000_000)
    return cost.quantize(Decimal("0.000001"), rounding=ROUND_UP)


def parse_completion(body: bytes) -> dict[str, Any]:
    """The parts of a chat completion the layer uses; ``ValueError`` when it is not one.
    Cassettes are exact reply bodies and go through this same parser."""
    try:
        document = loads_strict(body)
        choice = document["choices"][0]
        text = choice["message"]["content"]
        finish = choice.get("finish_reason")
        if text is None and finish == "length":
            text = ""  # the budget ran out before any answer: a truncated reply, not a stop (0103)
        if not isinstance(text, str):
            raise TypeError("content is not text")
        usage = document.get("usage") or {}
        provider = document.get("provider")
        return {
            "text": text,
            "served_model": str(document.get("model") or "")[:200],
            "served_provider": provider if isinstance(provider, str) else None,
            "finish_reason": finish if isinstance(finish, str) else None,
            "usage": usage if isinstance(usage, dict) else {},
            "generation_id": str(document.get("id") or "")[:100],
        }
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ValueError("not a chat completion") from exc


# OpenRouter names a few providers in replies by a name that does not normalise to their slug
# (its own provider list, read 2026-10-31: "Google" is google-vertex). Decision 0103.
PROVIDER_NAMES = {"google": "google-vertex"}


def _same_provider(served: str | None, pair: EvaluatedModel) -> bool:
    if served is None:
        return False
    name = served.strip().lower().replace(" ", "-")
    return PROVIDER_NAMES.get(name, name) == pair.provider


class Receipts(Protocol):
    """Where the gateway records every call: before the send (so an interrupted call is held,
    never repeated) and after it, whatever happened (review cp22-a F7, F8)."""

    def sent(self, pair: EvaluatedModel, request_sha256: str, estimate: Decimal) -> str: ...

    def finish(self, call_id: str, state: str, **fields: Any) -> None: ...


class Gateway:
    def __init__(
        self,
        config: ProviderConfig,
        secrets: SecretStore,
        transport: tp.Transport,
        home: Path,
        project_id: str,
    ) -> None:
        self.config, self.secrets, self.transport = config, secrets, transport
        self.home, self.project_id = home, project_id

    def _url(self, path: str) -> str:
        return f"{self.config.endpoint}/{path}"

    def _headers(self) -> dict[str, str]:
        reference = self.config.credential_ref
        secret = self.secrets.get(reference) if reference else None
        if secret is None:
            raise BatchStop("no_credential", "no key is stored for the provider's reference")
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {secret}",
        }

    def key_limit(self) -> dict[str, Any] | None:
        """The key's own limit and what remains of it (``GET /key``), read before a batch.
        None when the transport cannot ask or the provider does not say."""
        getter = getattr(self.transport, "get", None)
        if getter is None:
            return None
        try:
            reply = getter(self._url("key"), self._headers(), TIMEOUT_SECONDS)
        except (tp.NotSent, tp.OutcomeUnknown, BatchStop):  # BatchStop: no key stored
            return None
        if reply.status != 200:
            return None
        try:
            data = loads_strict(reply.body)["data"]
            return {"limit": data.get("limit"), "limit_remaining": data.get("limit_remaining")}
        except (ValueError, KeyError, TypeError):
            return None

    def call(
        self,
        *,
        run_id: str,
        task_kind: str,
        grant: TaskGrant,
        run_cap: Decimal,
        system: str,
        user: str,
        wire: dict[str, Any],
        max_tokens: int,
        receipts: Receipts | None = None,
        guard: Callable[[], None] | None = None,
    ) -> GatewayReply:
        """One sample through the evaluated pairs. ``guard`` runs before every send and stops
        the batch when the policy is no longer in force (review cp22-a F5); ``receipts`` records
        each call before it is sent and with its outcome after."""
        headers = self._headers()
        tried: list[str] = []
        for pair in grant.models:
            if guard is not None:
                guard()
            estimate = estimate_cost(pair, len(system) + len(user), max_tokens)
            reservation = spend.reserve(
                self.home, project_id=self.project_id, run_id=run_id, task_kind=task_kind,
                model=pair.model, estimate=estimate, run_cap=run_cap,
                task_ceiling=Decimal(grant.task_ceiling_usd),
                monthly_required=pair.provider != PER_CALL_PROVIDER,
            )  # fmt: skip
            body = request_body(pair, system, user, wire, max_tokens)
            raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            request_sha = canonical_sha256(body)
            call_id = receipts.sent(pair, request_sha, estimate) if receipts else None

            def finish(state: str, _call: str | None = call_id, **fields: Any) -> None:
                if receipts is not None and _call is not None:
                    receipts.finish(_call, state, **fields)

            try:
                reply = self.transport.post(
                    self._url("chat/completions"), headers, raw, TIMEOUT_SECONDS
                )
            except tp.NotSent:
                spend.release(self.home, reservation)
                finish("failed", error="nothing was sent")
                tried.append(f"{pair.model}@{pair.provider}: not sent")
                continue
            except tp.OutcomeUnknown:
                spend.hold(self.home, reservation)
                finish("unknown", error="the outcome is unknown")
                raise UnknownOutcome(pair, request_sha, reservation) from None
            except BaseException:
                # Interrupted while sending: the receipt stays "sent" and the reservation held,
                # so the call is never repeated (review cp22-a F7).
                spend.hold(self.home, reservation)
                raise
            body_text = reply.body.decode("utf-8", "replace")
            if reply.status in (402, 403):
                spend.release(self.home, reservation)
                finish("failed", error=f"HTTP {reply.status}", reply_body=body_text[:4000])
                raise BatchStop("key_limit", f"the provider refused the key (HTTP {reply.status})")
            if reply.status == 429:
                spend.release(self.home, reservation)
                finish("failed", error="HTTP 429", reply_body=body_text[:4000])
                raise BatchStop("rate_limited", "the provider is rate limiting this key")
            if reply.status != 200:
                # Counted at the estimate: a router error may still have been processed upstream.
                spend.settle(self.home, reservation, None)
                finish("failed", error=f"HTTP {reply.status}", reply_body=body_text[:4000])
                tried.append(f"{pair.model}@{pair.provider}: HTTP {reply.status}")
                continue
            try:
                parsed = parse_completion(reply.body)
            except ValueError:
                # A reply arrived and may be billed: settle at the estimate, try nothing else.
                spend.settle(self.home, reservation, None)
                finish("failed", error="not a chat completion", reply_body=body_text[:4000])
                raise BatchStop(
                    "invalid_reply", "the provider's reply is not a chat completion"
                ) from None
            text, usage = parsed["text"], parsed["usage"]
            served_model, provider_name = parsed["served_model"], parsed["served_provider"]
            finish_reason = parsed["finish_reason"]
            cost = usage.get("cost")
            billed = (
                Decimal(str(cost)).quantize(Decimal("0.000001"))
                if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0
                else None
            )
            spend.settle(self.home, reservation, billed)
            in_list = any(
                served_model == m.model and _same_provider(provider_name, m) for m in grant.models
            )
            finish(
                "succeeded", served_model=served_model, served_provider=provider_name,
                billed=None if billed is None else str(billed), reply_body=body_text,
            )  # fmt: skip
            reply_out = GatewayReply(
                text=text,
                pair=pair,
                served_model=served_model,
                served_provider=provider_name,
                in_list=in_list,
                finish_reason=finish_reason,
                billed=billed,
                estimate=estimate,
                usage={k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "cost")},
                receipt={
                    "call_id": call_id,
                    "endpoint_host": host_of(self.config.endpoint or ""),
                    "request_sha256": request_sha,
                    "reply_sha256": canonical_sha256({"body": body_text}),
                    "reply_body": body_text,
                    "generation_id": parsed["generation_id"],
                    "data_policy": DATA_POLICY,
                    "cost_basis": "billed" if billed is not None else "estimate",
                    "tried_before": tried,
                },
            )
            if billed is not None and billed > estimate:
                # The bound was wrong: keep this reply, but stop the batch (review cp22-a F9).
                raise OverBudget(reply_out)
            return reply_out
        raise BatchStop(
            "no_route", "no evaluated (model, provider) pair answered: " + "; ".join(tried)
        )
