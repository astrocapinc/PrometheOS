"""Evaluation before trust (A07.7; spec/05; decision 0093).

- **Fixed in advance.** An evaluation record names the task (kind and version), the golden set
  (items with their labels), the metrics (precision per outcome and coverage, the share of items
  not abstained), a target for each, the confidence level and method (95%, one-sided,
  Clopper-Pearson) and the minimum number of items. It is stored once, by digest, before any
  measurement; a later record for the same task kind may never lower a target.
- **Where trust comes from.** A measurement runs the task over the golden set. Trust is written
  only when every reply was a gateway reply whose body is bound, by digest, to the gateway's own
  call receipt in ``provider_calls``. Replayed cassettes (hand-made or recorded) measure, but they
  confer no trust.
- **What trust is bound to.** The task version, template digest, wire-schema digest, model and the
  set of providers that served the replies. Any change finds no trusted measurement: trust is
  withdrawn until a new one.
- **The rule.** Trusted only when the answered items reach the minimum, coverage meets its target,
  and the one-sided lower confidence bound of precision meets its target overall and for every
  outcome the model gave. The point estimate never decides.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from prometheos.adoption import assist
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError, StrictModel, utc_now
from prometheos.store import db
from prometheos.store.project import ProjectStore

from . import results
from .run import Executor, Subject, run
from .tasks import Task

# ----- the one-sided Clopper-Pearson bound -------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction of the regularized incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def clopper_pearson_lower(successes: int, n: int, confidence: float = 0.95) -> float:
    """The one-sided lower confidence bound of a binomial proportion (exact, Clopper-Pearson):
    the p with P(X >= successes | n, p) = 1 - confidence; 0 when there is no success."""
    if n <= 0 or successes <= 0:
        return 0.0
    if successes > n:
        raise ValueError("more successes than trials")
    alpha = 1.0 - confidence
    low, high = 0.0, 1.0
    for _ in range(200):  # I_p(s, n - s + 1) is increasing in p
        mid = (low + high) / 2.0
        if betainc(successes, n - successes + 1, mid) > alpha:
            high = mid
        else:
            low = mid
    return low


# ----- the record fixed in advance ----------------------------------------------------------------


class GoldenItem(StrictModel):
    item_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,80}$")]
    subject: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    subject_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    input: dict[str, Any]
    label: str | None  # the expected value of the outcome field; None: the right answer is abstain


class Targets(StrictModel):
    precision_lower_bound: float = Field(gt=0, le=1)
    coverage: float = Field(ge=0, le=1)


class EvaluationRecord(StrictModel):
    task_kind: str
    task_version: int
    outcome_field: Annotated[str, StringConstraints(pattern=r"^[a-z_.]+$")]
    # How an answer is scored, bound by the record's digest (review cp22-a R8-F1, decision 0105):
    # ``exact_value`` compares one value (with the per-outcome bound of A07.7); ``statement_set``
    # compares a list of statements as its sorted referent=status set (overall bound only).
    scoring: Literal["exact_value", "statement_set"]
    # ``development``: items the task's prompts were tuned on; such a record measures but never
    # confers trust (review cp22-a R8-F3). ``acceptance``: items the task was not tuned on.
    purpose: Literal["acceptance", "development"]
    golden: list[GoldenItem] = Field(min_length=1, max_length=5000)
    targets: Targets
    confidence: float
    method: Literal["clopper_pearson_one_sided"]
    min_items: int = Field(ge=1)

    @model_validator(mode="after")
    def _enough(self) -> EvaluationRecord:
        ids = [g.item_id for g in self.golden]
        if len(set(ids)) != len(ids):
            raise ValueError("golden item ids repeat")
        if self.min_items > len(self.golden):
            raise ValueError("the golden set is smaller than the minimum")
        if self.confidence != 0.95:
            raise ValueError("the confidence level is 95% (A07.7)")
        return self


def record_sha256(record: EvaluationRecord) -> str:
    return canonical_sha256(record.model_dump(mode="json"))


def _item_keys(record: EvaluationRecord) -> set[str]:
    """An item by its file's digest and by its exact input's digest (review cp22-a J1)."""
    return {g.subject_sha256 for g in record.golden} | {
        "input:" + canonical_sha256(g.input) for g in record.golden
    }


def _unseen(conn: Any, record: EvaluationRecord) -> None:
    """An acceptance record's items were never seen: not in a development record (the items a
    prompt was tuned on) nor in any record already measured, of any task kind, matched by file or
    input digest (review cp22-a R9-F1, R10-F1, J1, J2)."""
    mine = _item_keys(record)
    for digest, text in conn.execute("SELECT record_sha256, record FROM ai_eval_records"):
        try:
            earlier = EvaluationRecord.model_validate(json.loads(text))
        except ValueError as exc:  # fixed before decision 0105 (review cp22-a I2)
            raise PrometheosError(
                "validation_error",
                "an earlier evaluation record cannot be read (it predates the record's scoring "
                "and purpose); no acceptance record can be fixed beside it",
                {},
            ) from exc
        measured = conn.execute(
            "SELECT 1 FROM ai_eval_starts WHERE record_sha256 = ? LIMIT 1", (digest,)
        ).fetchone()
        if (earlier.purpose == "development" or measured) and mine & _item_keys(earlier):
            raise PrometheosError(
                "validation_error",
                "an acceptance record may not use items already seen: items a prompt was tuned "
                "on (a development record) or items of a record already measured",
                {"task_kind": record.task_kind},
            )


def fix(store: ProjectStore, record: EvaluationRecord, *, by: str) -> str:
    """Store the record before any measurement. A later record for the same task kind may raise
    targets and the minimum, never lower them (A07.7)."""
    digest = record_sha256(record)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            if conn.execute(
                "SELECT 1 FROM ai_eval_records WHERE record_sha256 = ?", (digest,)
            ).fetchone():
                return digest
            if record.purpose == "acceptance":
                _unseen(conn, record)
            for (text,) in conn.execute(
                "SELECT record FROM ai_eval_records WHERE task_kind = ?", (record.task_kind,)
            ).fetchall():
                try:
                    earlier = EvaluationRecord.model_validate(json.loads(text))
                except ValueError as exc:  # fixed before decision 0105 (review cp22-a I2)
                    raise PrometheosError(
                        "validation_error",
                        "an earlier record for this task cannot be read (it predates the record's "
                        "scoring and purpose); nothing new can be fixed beside it",
                        {"task_kind": record.task_kind},
                    ) from exc
                if earlier.task_version == record.task_version:
                    kept = {g.item_id: g for g in record.golden}
                    same_metrics = (
                        earlier.outcome_field == record.outcome_field
                        and earlier.confidence == record.confidence
                        and earlier.method == record.method
                        and earlier.scoring == record.scoring  # R9-F1
                        and earlier.purpose == record.purpose  # R9-F1
                    )
                    if not same_metrics or any(kept.get(g.item_id) != g for g in earlier.golden):
                        raise PrometheosError(
                            "validation_error",
                            "a record for this task version is already fixed: the golden set "
                            "may only grow (every earlier item unchanged) and the metrics stay; "
                            "otherwise register a new task version (review cp22-a F13)",
                            {"task_kind": record.task_kind},
                        )
                if (
                    record.targets.precision_lower_bound < earlier.targets.precision_lower_bound
                    or record.targets.coverage < earlier.targets.coverage
                    or record.min_items < earlier.min_items
                ):
                    raise PrometheosError(
                        "validation_error",
                        "a target or the minimum is lower than in an earlier record for this "
                        "task; targets are never lowered",
                        {"task_kind": record.task_kind},
                    )
            conn.execute(
                "INSERT INTO ai_eval_records (record_sha256, task_kind, task_version, record, "
                "fixed_by, fixed_at) VALUES (?,?,?,?,?,?)",
                (digest, record.task_kind, record.task_version,
                 json.dumps(record.model_dump(mode="json"), sort_keys=True), by, utc_now()),
            )  # fmt: skip
    finally:
        conn.close()
    return digest


def _fixed(store: ProjectStore, digest: str) -> EvaluationRecord:
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT record FROM ai_eval_records WHERE record_sha256 = ?", (digest,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise PrometheosError(
            "validation_error", "fix the evaluation record before measuring", {"record": digest}
        )
    return EvaluationRecord.model_validate(json.loads(row[0]))


# ----- measuring ----------------------------------------------------------------------------------


def _value(proposal: dict[str, Any] | None, field: str) -> Any:
    """The scored value: a field's scalar, or for a list of statements its set of
    ``referent=status`` pairs, sorted and joined by ``;`` (decision 0102)."""
    node: Any = proposal
    for part in field.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if isinstance(node, list):
        return ";".join(sorted(
            f"{x.get('referent')}={x.get('status')}" for x in node if isinstance(x, dict)
        ))  # fmt: skip
    return node


def _is_set(proposal: dict[str, Any] | None, record: EvaluationRecord) -> bool:
    node: Any = proposal
    for part in record.outcome_field.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return isinstance(node, list)


def _matches(predicted: Any, label: str | None) -> bool:
    """A label may name fixed alternatives, separated by ``||``, where the golden answer is
    honestly ambiguous; the alternatives are part of the record, fixed before any run."""
    return label is not None and predicted is not None and str(predicted) in label.split("||")


def _bound_to_receipts(store: ProjectStore, keys: list[str]) -> bool:
    """Every sample is a gateway reply whose stored body matches its call receipt."""
    conn = store.connect()
    try:
        for key in keys:
            row = conn.execute(
                "SELECT r.executor, r.output, c.reply_body, c.state FROM ai_results r "
                "LEFT JOIN provider_calls c ON c.id = r.call_id WHERE r.result_key = ?",
                (key,),
            ).fetchone()
            if row is None or row[0] != "gateway" or row[2] is None or row[3] != "succeeded":
                return False
    finally:
        conn.close()
    return True


def measure(
    store: ProjectStore, task: Task, record_digest: str, executor: Executor
) -> dict[str, Any]:
    record = _fixed(store, record_digest)
    if (record.task_kind, record.task_version) != (task.kind, task.version):
        raise PrometheosError("validation_error", "the record is for another task version", {})
    subjects = [
        Subject(
            assist.Bundle(
                task.assist_kind or task.kind,
                g.subject,
                g.subject_sha256,
                g.input,
                canonical_sha256(g.input),
                task.template_sha256,
            ),
            g.subject_sha256,
            "normal",
            [],
        )
        for g in record.golden
    ]
    from .replay import from_environment

    if record.purpose == "acceptance" and (
        executor.name != "gateway" or from_environment() is not None
    ):
        # A replay can never confer trust but would still show and spend the items (K1).
        raise PrometheosError(
            "validation_error",
            "an acceptance record is measured once, through the gateway; replay a development "
            "record instead",
            {"executor": executor.name},
        )
    if record.purpose == "acceptance":
        _fresh(store, task, subjects, executor)
    run_id = "aieval_" + uuid.uuid4().hex
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            # Only an acceptance record's first measurement may confer trust: after it began,
            # its items have been seen, even if it stops part-way (review cp22-a R10-F1, R11-F1).
            first = (
                conn.execute(
                    "SELECT 1 FROM ai_eval_starts WHERE record_sha256 = ? LIMIT 1",
                    (record_digest,),
                ).fetchone()
                is None
            )
            conn.execute(
                "INSERT INTO ai_eval_starts (run_id, record_sha256, executor, started_at) "
                "VALUES (?,?,?,?)",
                (run_id, record_digest, executor.name, utc_now()),
            )
    finally:
        conn.close()
    done = run(store, task, subjects, executor, run_id=run_id)
    if done["stopped"] is not None:
        raise PrometheosError(
            "budget_exceeded" if done["stopped"]["reason"] == "budget_exceeded" else "io_error",
            "the measurement stopped before the golden set was done; nothing is trusted",
            {"stopped": done["stopped"]["reason"]},
        )
    answered = correct = 0
    per_outcome: dict[str, list[int]] = {}
    keys: list[str] = []
    served: set[str] = set()
    for item, golden in zip(done["items"], record.golden, strict=True):
        keys += item.get("samples", [])
        if item["outcome"] == "abstained":
            continue
        answered += 1
        if item["outcome"] == "accepted" and _is_set(item.get("proposal"), record) != (
            record.scoring == "statement_set"
        ):
            raise PrometheosError(
                "validation_error",
                f"the record scores {record.scoring} but the task answers otherwise",
                {"scoring": record.scoring},
            )
        predicted = _value(item.get("proposal"), record.outcome_field)
        good = item["outcome"] == "accepted" and _matches(predicted, golden.label)
        correct += int(good)
        # Precision per outcome is for an exact value; a set of statements has no classes.
        if item["outcome"] == "accepted" and record.scoring == "exact_value":
            tally = per_outcome.setdefault(str(predicted), [0, 0])
            tally[0] += int(good)
            tally[1] += 1
    requested: set[str] = set()
    served_models: set[str] = set()
    off_list = 0
    for key in keys:
        stored = results.lookup(store, key)
        if stored is None:
            continue
        requested.add(stored_model(store, key))
        if stored.served_model:
            served_models.add(stored.served_model)
        if stored.served_provider:
            served.add(stored.served_provider)
        off_list += int(stored.hint_only)
    total = len(record.golden)
    coverage = answered / total
    overall = clopper_pearson_lower(correct, answered, record.confidence)
    outcomes = {
        name: {
            "correct": c,
            "given": n,
            "lower_bound": clopper_pearson_lower(c, n, record.confidence),
        }
        for name, (c, n) in sorted(per_outcome.items())
    }
    from .replay import from_environment

    # Replayed replies (the walk's test switch) are not the gateway's receipts of real calls.
    bound = (
        done["executor"] == "gateway"
        and from_environment() is None
        and _bound_to_receipts(store, keys)
    )
    meets = (
        answered >= record.min_items
        and coverage >= record.targets.coverage
        and overall >= record.targets.precision_lower_bound
        and all(o["lower_bound"] >= record.targets.precision_lower_bound for o in outcomes.values())
    )
    models = sorted(requested)
    # Trust is for the model that answered: every reply served by the requested model from the
    # evaluated list (review cp22-a F11), counted per stored sample, never through a listing
    # (F12). A task that needs k agreeing samples from k different models (R4-F2) is trusted as
    # that exact set of k models (review cp22-a R5-F1): one model for k = 1, k for k >= 2.
    served_as_requested = off_list == 0 and served_models <= requested
    one_unit = len(models) == (1 if task.agreement < 2 else task.agreement)
    from .tasks import REGISTRY

    # A task kind that is not registered (a spike's) is never trusted (review cp22-a H4).
    registered = REGISTRY.get(task.kind) == task
    result = {
        "record_sha256": record_digest,
        "task_kind": task.kind,
        "task_version": task.version,
        "template_sha256": task.template_sha256,
        "wire_sha256": task.wire_sha256,
        "executor": done["executor"],
        "models": models,
        "served_providers": sorted(served),
        "items": total,
        "answered": answered,
        "correct": correct,
        "coverage": round(coverage, 6),
        "precision": round(correct / answered, 6) if answered else 0.0,
        "precision_lower_bound": round(overall, 6),
        "per_outcome": outcomes,
        "meets_targets": meets,
        "bound_to_gateway_receipts": bound,
        "served_models": sorted(served_models),
        "replies_outside_the_list": off_list,
        "purpose": record.purpose,
        "registered": registered,
        "first_measurement": first,
        "trusted": bool(
            meets
            and bound
            and one_unit
            and served_as_requested
            and record.purpose == "acceptance"
            and registered
            and first
        ),
        "trust_unit": model_set(models) if one_unit else None,
        "run_id": done["run_id"],
    }
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO ai_evaluations (evaluation_id, record_sha256, task_kind, "
                "task_version, template_sha256, wire_sha256, model, provider_set, executor, "
                "trusted, result, measured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("aieval_" + uuid.uuid4().hex, record_digest, task.kind, task.version,
                 task.template_sha256, task.wire_sha256, model_set(models) if one_unit else "",
                 json.dumps(sorted(served)), done["executor"], int(result["trusted"]),
                 json.dumps(result, sort_keys=True), utc_now()),
            )  # fmt: skip
    finally:
        conn.close()
    return result


def _fresh(store: ProjectStore, task: Task, subjects: list[Subject], executor: Executor) -> None:
    """An acceptance measurement rests only on replies made for it: no reply of these models for
    these items may be stored already, or the cache would answer for them (review cp22-a R9-F1,
    B8)."""
    for subject in subjects:
        for model in executor.models():
            params = executor.params_sha256(model)
            for index in range(task.agreement):
                key = results.result_key(task, model, params, subject.bundle.input_sha256, index)
                if results.lookup(store, key) is not None:
                    raise PrometheosError(
                        "validation_error",
                        "an acceptance measurement needs items these models have not answered "
                        "before; stored replies would answer for them",
                        {"subject": subject.bundle.subject[:200], "model": model},
                    )


def why_not(result: dict[str, Any], record: EvaluationRecord | None) -> str | None:
    """Why a measurement confers no trust, in the owner's words; None when it is trusted
    (decision 0104). The checks are the measurement's own, read back from its stored result."""
    if result.get("trusted"):
        return None
    reasons: list[str] = []
    if result.get("purpose") == "development":
        reasons.append("it is a development record: the task's prompts were tuned on these items, "
                       "so it measures but cannot confer trust")  # fmt: skip
    if result.get("purpose") == "acceptance" and result.get("first_measurement") is False:
        reasons.append("its items were seen by an earlier measurement of this record; new trust "
                       "needs new items")  # fmt: skip
    if result.get("registered") is False:
        reasons.append("the task is not a registered task (a spike's)")
    if not result.get("bound_to_gateway_receipts"):
        reasons.append("its replies are not the gateway's own receipts (recorded or replayed)")
    outside = int(result.get("replies_outside_the_list") or 0)
    if outside:
        reasons.append(f"{outside} replies came from outside the evaluated list")
    if record is not None:
        target = record.targets.precision_lower_bound
        if int(result.get("answered") or 0) < record.min_items:
            reasons.append(f"only {result.get('answered')} items answered, fewer than the "
                           f"{record.min_items} required")  # fmt: skip
        if float(result.get("coverage") or 0) < record.targets.coverage:
            reasons.append(f"coverage {float(result['coverage']):.0%} is below the "
                           f"{record.targets.coverage:.0%} target")  # fmt: skip
        if float(result.get("precision_lower_bound") or 0) < target:
            reasons.append(
                f"precision is at least {float(result['precision_lower_bound']):.0%} at 95% "
                f"confidence, below the {target:.0%} target ({result.get('correct')} of "
                f"{result.get('answered')} right: more items or fewer errors are needed)"
            )
        thin = sorted(
            f"{name.replace('_', ' ')} ({v['correct']}/{v['given']})"
            for name, v in (result.get("per_outcome") or {}).items()
            if float(v["lower_bound"]) < target
        )
        if thin:
            reasons.append(
                f"too few items to vouch for each answer it gave on its own: {', '.join(thin)} "
                f"(each needs its own bound above {target:.0%})"
            )
    if "trust_unit" in result and not result["trust_unit"]:
        reasons.append("its samples did not come from exactly the task's number of models")
    return "; ".join(reasons) or "the measurement did not meet its record"


def model_set(models: str | list[str]) -> str:
    """The stored name of a unit of trust: one model, or a k-sample task's models joined."""
    return models if isinstance(models, str) else "+".join(sorted(models))


def stored_model(store: ProjectStore, key: str) -> str:
    conn = store.connect()
    try:
        row = conn.execute("SELECT model FROM ai_results WHERE result_key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else ""


def trusted(store: ProjectStore, task: Task, model: str | list[str], providers: list[str]) -> bool:
    """Whether a measurement trusts exactly this task version, template, wire schema, model (or
    a k-sample task's exact set of models) and provider set; any change answers False until a
    new measurement (A07.7)."""
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT trusted FROM ai_evaluations WHERE task_kind = ? AND task_version = ? AND "
            "template_sha256 = ? AND wire_sha256 = ? AND model = ? AND provider_set = ? "
            "ORDER BY measured_at DESC, rowid DESC LIMIT 1",
            (task.kind, task.version, task.template_sha256, task.wire_sha256, model_set(model),
             json.dumps(sorted(providers))),
        ).fetchone()  # fmt: skip
    finally:
        conn.close()
    return bool(row and row[0])
