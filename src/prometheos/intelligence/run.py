"""A task run over named subjects (A07.2, A07.6, A07.9; decision 0092).

Each subject is a digest-pinned bundle with its sensitivity and the root-alias paths it names.
For each one the runner takes ``k`` samples (the task's agreement), each from the cache when a
sample with the same key is stored, else from the executor; validates every sample with the same
checks whatever the executor; stores it; and derives the subject's outcome:

- ``accepted``: every sample passed and they agree (a proposal, a hint when the task's effect is
  a hint or a reply came from outside the evaluated list); with ``k >= 2`` the samples come from
  ``k`` different models, as CP17.4's agreement rule needs (review cp22-a R4-F2), else the
  subject is ``rejected`` with ``one_model``;
- ``rejected`` (with reasons), ``abstained``, ``disagreed``;
- ``denied``: restricted or private input never reaches a remote executor (``policy_denied``);
- ``held``: a call to this subject has an unknown outcome; it is never repeated;
- ``not_run``: the batch stopped before it.

A batch stops cleanly at a cap, at the key's limit, when rate limited or at an unknown outcome,
and keeps every finished subject. Nothing here changes a status, class, relation or default.

Executors: ``GatewayExecutor`` (OpenRouter inside the standing policy) and ``CassetteExecutor``
(recorded reply bodies, for tests and walks: it sends nothing and confers no trust). The coding
agent's executor is CP17.4's prepare-and-submit path, which applies the same validators.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from prometheos.adoption import assist
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError
from prometheos.store.project import ProjectStore

from . import results
from .gateway import (
    DATA_POLICY,
    BatchStop,
    Gateway,
    OverBudget,
    UnknownOutcome,
    parse_completion,
)
from .policy import SENSITIVITY_RANK, TaskGrant
from .tasks import Task
from .validate import Verdict, agree, check


@dataclass(frozen=True)
class Subject:
    bundle: assist.Bundle
    current_digest: str | None
    sensitivity: str
    refs: list[str] = field(default_factory=list)
    # Reads the file's digest again when its reply is checked, so a file changed during the call
    # makes the reply stale (review cp22-a F4); without it the digest above is used.
    reread: Callable[[], str | None] | None = None
    scan_gen: int | None = None  # the adoption scan the file came from, when it did

    def digest_now(self) -> str | None:
        return self.reread() if self.reread is not None else self.current_digest


@dataclass(frozen=True)
class Sample:
    text: str
    model: str
    executor: str
    served_model: str | None
    served_provider: str | None
    in_list: bool
    call_id: str | None
    truncated: bool
    over_budget: bool = False


class Executor(Protocol):
    name: str

    def models(self) -> list[str]: ...

    def params_sha256(self, model: str) -> str: ...

    def sample(
        self,
        task: Task,
        subject: Subject,
        index: int,
        system: str,
        user: str,
        run_id: str,
        exclude: tuple[str, ...] = (),
    ) -> Sample: ...


class NoDistinctModel(Exception):
    """Every evaluated model has already given this subject a sample."""


class GatewayExecutor:
    name = "gateway"

    def __init__(
        self,
        store: ProjectStore,
        gateway: Gateway,
        grant: TaskGrant,
        run_cap: Decimal,
        guard: Callable[[], None] | None = None,
    ) -> None:
        self.store, self.gateway, self.grant, self.run_cap = store, gateway, grant, run_cap
        self.guard = guard

    def models(self) -> list[str]:
        return [m.model for m in self.grant.models]

    def params_sha256(self, model: str) -> str:
        pair = next(m for m in self.grant.models if m.model == model)
        return canonical_sha256(
            {"provider": pair.provider, "parameters": sorted(pair.supported_parameters),
             "effort": pair.lowest_effort}
        )  # fmt: skip

    def sample(
        self,
        task: Task,
        subject: Subject,
        index: int,
        system: str,
        user: str,
        run_id: str,
        exclude: tuple[str, ...] = (),
    ) -> Sample:
        store = self.store
        grant = self.grant
        if exclude:
            left = [m for m in grant.models if m.model not in exclude]
            if not left:
                raise NoDistinctModel(task.kind)
            grant = grant.model_copy(update={"models": left})

        class _Receipts:
            def sent(self, pair: Any, request_sha256: str, estimate: Decimal) -> str:
                return results.call_sent(
                    store, run_id=run_id, task_kind=task.kind, attempt=index + 1,
                    host="openrouter.ai", model=pair.model,
                    bundle_sha256=subject.bundle.input_sha256, request_sha256=request_sha256,
                    input_refs=subject.refs, estimate=str(estimate), data_policy=DATA_POLICY,
                    sensitivity=subject.sensitivity,
                )  # fmt: skip

            def finish(self, call_id: str, state: str, **fields: Any) -> None:
                results.call_finish(store, call_id, state, **fields)

        over = False
        try:
            reply = self.gateway.call(
                run_id=run_id, task_kind=task.kind, grant=grant, run_cap=self.run_cap,
                system=system, user=user, wire=task.wire, max_tokens=task.max_output_tokens,
                receipts=_Receipts(), guard=self.guard,
            )  # fmt: skip
        except OverBudget as stop:
            reply, over = stop.reply, True
        from .replay import ReplayTransport

        # A replayed reply is a recording, never the gateway's (review cp22-a D3): no trust.
        source = "cassette" if isinstance(self.gateway.transport, ReplayTransport) else self.name
        return Sample(reply.text, reply.pair.model, source, reply.served_model,
                      reply.served_provider, reply.in_list, reply.receipt.get("call_id"),
                      reply.truncated, over)  # fmt: skip


class CassetteExecutor:
    """Recorded reply bodies keyed by (task kind, input digest, sample index). It sends
    nothing; its results are tagged ``cassette`` and confer no trust (A07.7)."""

    name = "cassette"

    def __init__(self, cassettes: list[dict[str, Any]], model: str = "cassette/recorded") -> None:
        self.model = model
        self.cassettes = {
            (c["task_kind"], c["input_sha256"], int(c["sample"])): c for c in cassettes
        }

    @classmethod
    def from_folder(cls, folder: Path) -> CassetteExecutor:
        found = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]
        return cls(found)

    def models(self) -> list[str]:
        named = {str(c["model"]) for c in self.cassettes.values() if c.get("model")}
        return [self.model, *sorted(named - {self.model})]

    def params_sha256(self, model: str) -> str:
        return canonical_sha256({"cassette": model})

    def sample(
        self,
        task: Task,
        subject: Subject,
        index: int,
        system: str,
        user: str,
        run_id: str,
        exclude: tuple[str, ...] = (),
    ) -> Sample:
        cassette = self.cassettes.get((task.kind, subject.bundle.input_sha256, index))
        if cassette is None:
            raise BatchStop("no_route", f"no cassette for {task.kind} sample {index}")
        try:
            parsed = parse_completion(json.dumps(cassette["body"]).encode("utf-8"))
        except ValueError:
            raise BatchStop("invalid_reply", "the cassette is not a chat completion") from None
        return Sample(
            parsed["text"], str(cassette.get("model") or self.model), self.name,
            parsed["served_model"],
            parsed["served_provider"], bool(cassette.get("in_list", True)), None,
            parsed["finish_reason"] == "length",
        )  # fmt: skip


def _held(store: ProjectStore, task: Task, subject: Subject) -> bool:
    """An unknown outcome for this task and bundle that nobody has acknowledged."""
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM provider_calls c WHERE c.purpose = ? AND c.bundle_digest = ? "
            "AND c.state IN ('unknown', 'sent') AND NOT EXISTS (SELECT 1 FROM provider_calls a "
            "WHERE a.acknowledges = c.id)",
            (f"ai_task:{task.kind}", subject.bundle.input_sha256),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _outcome(task: Task, stored: list[results.Stored]) -> dict[str, Any]:
    rejected = [s for s in stored if s.outcome == "rejected"]
    if rejected:
        reasons = sorted({r for s in rejected for r in s.reasons})
        return {"outcome": "rejected", "reasons": reasons, "proposal": None}
    if any(s.outcome == "abstained" for s in stored):
        return {"outcome": "abstained", "reasons": [], "proposal": None}
    outputs = [s.output for s in stored if s.output is not None]
    if not agree(task, outputs):
        return {"outcome": "disagreed", "reasons": ["disagreement"], "proposal": None}
    if task.agreement >= 2 and len({s.model for s in stored}) < 2:
        # Agreement from one model proves repeatability, not independence (CP17.4; R4-F2).
        return {"outcome": "rejected", "reasons": ["one_model"], "proposal": None}
    hint = task.effect == "evidence_hint" or any(s.hint_only for s in stored)
    return {"outcome": "accepted", "reasons": [], "proposal": outputs[0]["result"],
            "hint_only": hint}  # fmt: skip


def _provenance(stored: list[results.Stored]) -> dict[str, Any]:
    """Where each sample came from (A07.3: proposals carry their provenance)."""
    return {
        "samples": [s.result_key for s in stored],
        "provenance": [
            {"executor": s.executor, "served_model": s.served_model,
             "served_provider": s.served_provider, "call_id": s.call_id,
             "hint_only": s.hint_only}
            for s in stored
        ],
    }  # fmt: skip


def run(
    store: ProjectStore,
    task: Task,
    subjects: list[Subject],
    executor: Executor,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    run_id = run_id or "airun_" + uuid.uuid4().hex
    items: list[dict[str, Any]] = []
    stopped: dict[str, Any] | None = None
    new_samples = cached_samples = 0
    for position, subject in enumerate(subjects):
        bundle = subject.bundle
        item: dict[str, Any] = {"subject": bundle.subject, "input_sha256": bundle.input_sha256}
        if stopped is not None:
            items.append(item | {"outcome": "not_run", "reasons": [stopped["reason"]]})
            continue
        if SENSITIVITY_RANK.get(subject.sensitivity, 2) > SENSITIVITY_RANK["normal"]:
            items.append(item | {"outcome": "denied", "reasons": ["policy_denied"]})
            continue
        if _held(store, task, subject):
            items.append(item | {"outcome": "held", "reasons": ["unknown_outcome"]})
            continue
        stored: list[results.Stored] = []
        distinct = task.agreement >= 2
        try:
            for index in range(task.agreement):
                used = tuple(s.model for s in stored) if distinct else ()
                found = None
                for model in executor.models():
                    if model in used:
                        continue
                    key = results.result_key(task, model, executor.params_sha256(model),
                                             bundle.input_sha256, index)  # fmt: skip
                    found = results.lookup(store, key)
                    if found is not None:
                        break
                if found is not None:
                    cached_samples += 1
                    stored.append(found)
                    continue
                system, user = task.messages(bundle.input)
                sample = executor.sample(task, subject, index, system, user, run_id, used)
                verdict = (
                    Verdict("rejected", ("truncated",), None)
                    if sample.truncated
                    else check(task, bundle, sample.text, current_digest=subject.digest_now())
                )
                stored.append(
                    results.save(
                        store, key=results.result_key(task, sample.model,
                                                      executor.params_sha256(sample.model),
                                                      bundle.input_sha256, index),
                        task=task, model=sample.model,
                        params_sha256=executor.params_sha256(sample.model),
                        input_sha256=bundle.input_sha256, sample=index, subject=bundle.subject,
                        executor=sample.executor, outcome=verdict.outcome, reasons=verdict.reasons,
                        output=verdict.output, hint_only=not sample.in_list,
                        served_model=sample.served_model, served_provider=sample.served_provider,
                        call_id=sample.call_id, run_id=run_id, sensitivity=subject.sensitivity,
                        scan_gen=subject.scan_gen, subject_sha256=bundle.subject_sha256,
                    )
                )  # fmt: skip
                new_samples += 1
                if sample.over_budget:
                    raise BatchStop("over_budget", "a reply was billed above its upper estimate")
        except NoDistinctModel:
            items.append(item | {"outcome": "not_run", "reasons": ["needs_distinct_models"]})
            continue
        except UnknownOutcome:
            items.append(item | {"outcome": "held", "reasons": ["unknown_outcome"]})
            stopped = {"reason": "unknown_outcome", "at": position}
            continue
        except BatchStop as stop:
            if stop.reason == "over_budget" and len(stored) == task.agreement:
                judged = _outcome(task, stored)
                items.append(item | judged | _provenance(stored))
                results.save_verdict(
                    store, task=task, run_id=run_id, input_sha256=bundle.input_sha256,
                    subject=bundle.subject, outcome=judged["outcome"],
                    result_keys=[s.result_key for s in stored],
                    hint_only=bool(judged.get("hint_only")),
                )  # fmt: skip
            else:
                items.append(item | {"outcome": "not_run", "reasons": [stop.reason]})
            stopped = {"reason": stop.reason, "detail": stop.detail, "at": position}
            continue
        except PrometheosError as exc:
            if exc.code != "budget_exceeded":
                raise
            items.append(item | {"outcome": "not_run", "reasons": ["budget_exceeded"]})
            stopped = {"reason": "budget_exceeded", "cap": exc.details.get("cap"), "at": position}
            continue
        judged = _outcome(task, stored)
        items.append(item | judged | _provenance(stored))
        results.save_verdict(
            store, task=task, run_id=run_id, input_sha256=bundle.input_sha256,
            subject=bundle.subject, outcome=judged["outcome"],
            result_keys=[s.result_key for s in stored], hint_only=bool(judged.get("hint_only")),
        )  # fmt: skip
    counts: dict[str, int] = {}
    for item in items:
        counts[item["outcome"]] = counts.get(item["outcome"], 0) + 1
    return {
        "run_id": run_id,
        "task_kind": task.kind,
        "task_version": task.version,
        "executor": executor.name,
        "items": items,
        "counts": counts,
        "stopped": stopped,
        "new_samples": new_samples,
        "cached_samples": cached_samples,
    }
