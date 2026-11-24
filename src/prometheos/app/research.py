"""CP2 research rules: scoped claims, assessments, corrections, people, meetings, actions.

Each function runs inside the shared write transaction. Functions return the
names of *protected effects* they encountered; ``apply_request`` refuses the
whole request unless one owner decision capability bound to this exact request
covers them. Agent-supplied fields such as ``author_ref`` or
``method=researcher`` never grant authority by themselves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from prometheos.domain.bodies import (
    ActionBody,
    AssessmentBody,
    ClaimBody,
    CorrectionBody,
    EntryBody,
    ItemBody,
    MeetingBody,
    PersonBody,
    RoleBody,
)
from prometheos.domain.ids import kind_of
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.store.project import RecordState, Txn

# Entry categories fixed at creation: relations and derived views depend on them.
FIXED_CATEGORIES = frozenset({"assumption", "question", "track"})

# ----- helpers -------------------------------------------------------------------------


def _invalid(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


def require_rev(txn: Txn, ref_id: str, rev: int, kinds: set[str]) -> RecordState:
    if kind_of(ref_id) not in kinds:
        raise _invalid(f"reference to {kind_of(ref_id)} is not allowed here", id=ref_id)
    if not txn.rev_exists(ref_id, rev):
        raise PrometheosError(
            "not_found", "referenced revision does not exist", {"id": ref_id, "rev": rev}
        )
    state = txn.get(ref_id)
    assert state is not None
    return state


def require_current(txn: Txn, ref_id: str, kinds: set[str]) -> RecordState:
    if kind_of(ref_id) not in kinds:
        raise _invalid(f"reference to {kind_of(ref_id)} is not allowed here", id=ref_id)
    state = txn.get(ref_id)
    if state is None:
        raise PrometheosError("not_found", "referenced record does not exist", {"id": ref_id})
    return state


def max_rank(*states: RecordState) -> int:
    return max((SENSITIVITY_ORDER[s.sensitivity] for s in states), default=0)


def _action_cycle(txn: Txn, start: str, deps: list[str]) -> bool:
    """Would ``start`` depending on ``deps`` close a cycle?"""
    stack, seen = list(deps), set()
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node in seen:
            continue
        seen.add(node)
        state = txn.get(node)
        if state is not None and state.kind == "action":
            stack.extend(state.body.get("dependencies", []))
    return False


def _supersede_cycle(txn: Txn, target: str, replacement: str) -> bool:
    node: str | None = replacement
    seen: set[str] = set()
    while node and node not in seen:
        if node == target:
            return True
        seen.add(node)
        state = txn.get(node)
        ref = state.body.get("superseded_by") if state else None
        node = ref["id"] if ref else None
    return False


# ----- create ----------------------------------------------------------------------------


def check_create(
    txn: Txn, model: BaseModel, record_id: str, sensitivity: str, evidence_rank: int
) -> tuple[list[str], int]:
    """Validate a CP2 create; return (protected effects, highest cited sensitivity rank)."""
    protected: list[str] = []
    rank = evidence_rank
    if isinstance(model, ClaimBody):
        if model.state != "proposed" or model.superseded_by is not None:
            raise PrometheosError(
                "policy_denied", "claims are created as proposed; state changes are transitions", {}
            )
        for ref in model.assessment_refs:
            require_rev(txn, ref.id, ref.rev, {"assessment"})
    elif isinstance(model, AssessmentBody):
        claim = require_rev(txn, model.claim_ref.id, model.claim_ref.rev, {"claim"})
        rank = max(rank, max_rank(claim))
        if model.method == "deterministic_check":
            raise PrometheosError(
                "policy_denied", "deterministic_check assessments are produced by PrometheOS", {}
            )
        if model.method == "researcher":
            protected.append("researcher-attributed assessment")
        if model.author_ref is not None:
            require_rev(txn, model.author_ref.id, model.author_ref.rev, {"person"})
    elif isinstance(model, CorrectionBody):
        if model.status != "proposed" or model.applied_seq is not None:
            raise PrometheosError(
                "policy_denied", "corrections are created as proposals; apply is separate", {}
            )
        for ref in model.target_refs:
            rank = max(
                rank, max_rank(require_rev(txn, ref.id, ref.rev, {"claim", "entry", "item"}))
            )
        for ref in model.replacement_refs:
            rank = max(rank, max_rank(require_rev(txn, ref.id, ref.rev, {"claim", "entry"})))
        for effect in model.effects:
            if effect.effect == "supersede" and kind_of(effect.target_ref.id) == "entry":
                _check_assumption_supersession(txn, effect.target_ref.id, effect.replacement_ref)
            if effect.effect == "replace_text":
                _refuse_superseded_text(txn, effect.target_ref.id)
            if effect.replacement_ref and _supersede_cycle(
                txn, effect.target_ref.id, effect.replacement_ref.id
            ):
                raise _invalid("correction would create a supersession cycle")
    elif isinstance(model, EntryBody):
        if model.superseded_by is not None:
            raise PrometheosError(
                "policy_denied",
                "superseded_by is set only by applying a correction that supersedes the entry",
                {},
            )
    elif isinstance(model, PersonBody):
        if model.contact is not None and SENSITIVITY_ORDER[sensitivity] == 0:
            raise PrometheosError(
                "policy_denied", "people with contact details are restricted by default", {}
            )
    elif isinstance(model, RoleBody):
        require_current(txn, model.person_id, {"person"})
        require_current(txn, model.scope_id, {"project", "paper"})
    elif isinstance(model, MeetingBody):
        for ref in model.participant_refs:
            require_rev(txn, ref.id, ref.rev, {"person"})
        for ref in model.source_version_refs:
            rank = max(rank, max_rank(require_rev(txn, ref.id, ref.rev, {"version"})))
        if model.summary_entry_ref:
            require_rev(txn, model.summary_entry_ref.id, model.summary_entry_ref.rev, {"entry"})
    elif isinstance(model, ItemBody):
        if model.status != "proposed" or model.action_id is not None:
            raise PrometheosError(
                "policy_denied", "meeting items are created as proposals; adoption is separate", {}
            )
        if model.meeting_id:
            rank = max(rank, max_rank(require_current(txn, model.meeting_id, {"meeting"})))
        if model.proposed_owner:
            require_rev(txn, model.proposed_owner.id, model.proposed_owner.rev, {"person"})
        _reject_duplicate_item(txn, record_id, model)
    elif isinstance(model, ActionBody):
        if model.status != "proposed" or model.completion_evidence:
            raise PrometheosError(
                "policy_denied", "actions are created as proposals; adoption is a transition", {}
            )
        _check_action_links(txn, record_id, model)
    return protected, rank


def _check_assumption_supersession(txn: Txn, target_id: str, replacement: Any) -> None:
    """A02 (CP10): only an assumption entry is superseded, only by another assumption entry
    that is not itself superseded (decision 0011 round 2 item 3, decision 0015)."""
    target = txn.get(target_id)
    if target is None or target.body.get("category") != "assumption":
        raise _invalid("only assumption entries can be superseded", id=target_id)
    if target.body.get("superseded_by") is not None:
        raise _invalid("this assumption is already superseded", id=target_id)
    other = txn.get(replacement.id) if replacement is not None else None
    if other is None or other.kind != "entry" or other.body.get("category") != "assumption":
        raise _invalid("the replacement must be another assumption entry", id=target_id)
    if other.body.get("superseded_by") is not None:
        raise _invalid("the replacement assumption is itself superseded", id=other.id)


def _refuse_superseded_text(txn: Txn, target_id: str) -> None:
    """A superseded assumption is immutable, also to replace_text (decision 0015 round 2)."""
    target = txn.get(target_id)
    if target is not None and target.body.get("superseded_by") is not None:
        raise _invalid("a superseded assumption's text cannot be replaced", id=target_id)


def _reject_duplicate_item(txn: Txn, record_id: str, model: ItemBody) -> None:
    """Exact meeting + source locator is the ingestion identity of an item."""
    if not model.evidence:
        return

    def locator_identity(evidence: list[Any]) -> list[tuple[str, str, str]]:
        # Source, version and exact locator; an optional quote digest is not identity.
        return sorted(
            (e["source_id"], e["version_id"], json.dumps(e["locator"], sort_keys=True))
            for e in evidence
        )

    identity = locator_identity([e.model_dump(mode="json") for e in model.evidence])
    rows = txn.conn.execute(
        "SELECT r.id FROM records r WHERE r.kind = 'item' AND r.id != ?", (record_id,)
    ).fetchall()
    for row in rows:
        other = txn.get(row["id"])
        if other is None:
            continue
        if (
            other.body.get("meeting_id") == model.meeting_id
            and locator_identity(other.body.get("evidence", [])) == identity
        ):
            raise PrometheosError(
                "revision_conflict",
                "an item with the same meeting and exact source locator already exists",
                {"existing_id": row["id"]},
            )


def _check_action_links(txn: Txn, record_id: str, model: ActionBody) -> None:
    for dep in model.dependencies:
        require_current(txn, dep, {"action"})
    if _action_cycle(txn, record_id, model.dependencies):
        raise _invalid("action dependencies would form a cycle")
    if model.owner_ref:
        require_rev(txn, model.owner_ref.id, model.owner_ref.rev, {"person"})
    for ref in model.source_refs:
        if not txn.rev_exists(ref.id, ref.rev):
            raise PrometheosError("not_found", "referenced revision does not exist", {"id": ref.id})
    if model.commitment_item_id:
        require_current(txn, model.commitment_item_id, {"item"})
        others = txn.conn.execute(
            "SELECT r.id FROM records r WHERE r.kind = 'action' AND r.id != ?", (record_id,)
        ).fetchall()
        for row in others:
            other = txn.get(row["id"])
            if other and other.body.get("commitment_item_id") == model.commitment_item_id:
                raise PrometheosError(
                    "revision_conflict", "this item already has an action", {"action_id": row["id"]}
                )


# ----- revise ---------------------------------------------------------------------------


IMMUTABLE_ON_REVISE: dict[str, tuple[str, ...]] = {
    "claim": ("state", "superseded_by"),
    "entry": ("superseded_by",),
    "assessment": ("claim_ref", "method"),
    "correction": ("status", "applied_seq"),
    "item": ("status", "action_id", "meeting_id"),
    "action": ("status", "completion_evidence", "commitment_item_id", "blocker"),
    "role": ("person_id", "scope_id"),
    "paper": ("status",),
    "anchor": ("paper_id", "status", "reason"),
}


def check_revise(txn: Txn, state: RecordState, model: BaseModel, sensitivity: str) -> list[str]:
    protected: list[str] = []
    new = model.model_dump(mode="json")
    for field in IMMUTABLE_ON_REVISE.get(state.kind, ()):
        if new.get(field) != state.body.get(field):
            raise PrometheosError(
                "policy_denied",
                f"{state.kind}.{field} changes only through its transition or correction",
                {"id": state.id},
            )
    if state.kind == "claim" and state.body["state"] != "proposed":
        raise PrometheosError(
            "policy_denied",
            "only proposed claims can be revised; correct others with a replacement claim",
            {"id": state.id},
        )
    if state.kind == "entry" and state.body.get("superseded_by") is not None:
        raise PrometheosError(
            "policy_denied",
            "a superseded assumption is immutable; its replacement carries the change",
            {"id": state.id},
        )
    if isinstance(model, EntryBody):
        # Decision 0015 round 2 (R1): the assumption category is fixed at creation. Leaving
        # it would break depends_on targets and supersession replacements; entering it would
        # make an ordinary entry a depends_on target or a supersede target after the fact.
        # CP20.2 (review cp20-2-a F1, decision 0083): question and track decide the research
        # map's relations and stages the same way, so they are fixed too.
        old = str(state.body.get("category"))
        fixed = {old, str(model.category)} & FIXED_CATEGORIES
        if old != model.category and fixed:
            raise PrometheosError(
                "policy_denied",
                f"an entry's {sorted(fixed)[0]} category is fixed at creation; record a new entry",
                {"id": state.id},
            )
    if state.kind == "execution":
        _check_execution_revise(state, new)
    if state.kind == "correction" and state.body["status"] != "proposed":
        raise PrometheosError("policy_denied", "decided corrections are immutable", {})
    if (
        isinstance(model, PersonBody)
        and model.contact is not None
        and SENSITIVITY_ORDER[sensitivity] == 0
    ):
        raise PrometheosError("policy_denied", "people with contact details are restricted", {})
    if isinstance(model, ActionBody):
        _check_action_links(txn, state.id, model)
    if isinstance(model, AssessmentBody) and model.method == "researcher":
        protected.append("researcher-attributed assessment")
    return protected


EXECUTION_FINAL = ("reported_completed", "reported_failed", "reported_cancelled")


def _check_execution_revise(state: RecordState, new: dict[str, Any]) -> None:
    """A02, CP10.4 (decision 0018): a report is a full revision, but a run reported completed,
    failed or cancelled stays so (a restart is a new registration), a reported run is never
    ``registered`` again, and receipt references only grow."""
    old = state.body
    if old["state"] in EXECUTION_FINAL and new["state"] != old["state"]:
        raise _invalid(
            "an execution reported completed, failed or cancelled is final; register a new one",
            id=state.id,
            state=old["state"],
        )
    if new["state"] == "registered" and old["state"] != "registered":
        raise _invalid("a reported execution is not registered again", id=state.id)
    dropped = {r["id"] for r in old["receipt_refs"]} - {r["id"] for r in new["receipt_refs"]}
    if dropped:
        raise _invalid("receipt references only grow", id=state.id, dropped=sorted(dropped))


# ----- transitions ------------------------------------------------------------------------

# (kind, from, to) -> "normal" | "protected" | "reason" (normal, needs a reason)
TRANSITIONS: dict[tuple[str, str, str], str] = {
    ("claim", "proposed", "supported_within_conditions"): "protected",
    ("claim", "proposed", "inconclusive"): "normal",
    ("claim", "inconclusive", "proposed"): "reason",
    ("claim", "inconclusive", "supported_within_conditions"): "protected",
    ("claim", "contested", "supported_within_conditions"): "protected",
    ("claim", "contested", "inconclusive"): "reason",
    ("item", "proposed", "adopted"): "protected",
    ("item", "proposed", "dismissed"): "reason",
    ("item", "proposed", "superseded"): "reason",
    ("item", "dismissed", "proposed"): "reason",
    ("action", "proposed", "ready"): "protected",
    ("action", "proposed", "cancelled"): "normal",
    # A06.1, CP20.3: only a planned check may start straight from proposed, under the budget
    # rule in _action_transition (decision 0084).
    ("action", "proposed", "in_progress"): "normal",
    ("action", "ready", "in_progress"): "normal",
    ("action", "ready", "blocked"): "reason",
    ("action", "ready", "cancelled"): "normal",
    ("action", "in_progress", "done"): "normal",
    ("action", "in_progress", "blocked"): "reason",
    ("action", "in_progress", "ready"): "normal",
    ("action", "in_progress", "cancelled"): "normal",
    ("action", "blocked", "ready"): "normal",
    ("action", "blocked", "cancelled"): "normal",
    ("action", "done", "ready"): "reason",
    ("action", "cancelled", "proposed"): "reason",
    ("correction", "proposed", "rejected"): "protected",
}

STATE_FIELD = {
    "claim": "state",
    "item": "status",
    "action": "status",
    "correction": "status",
    "paper": "status",
    "anchor": "status",
}


def transition(
    txn: Txn, store_dir: Path, state: RecordState, to: str, reason: str | None, evidence: list[Any]
) -> tuple[dict[str, Any], list[str]]:
    """Return (new body, protected effects) for a transition."""
    field = STATE_FIELD.get(state.kind)
    if field is None:
        raise PrometheosError("unsupported", f"{state.kind} records have no transitions", {})
    current = state.body[field]
    rule = TRANSITIONS.get((state.kind, current, to))
    if rule is None:
        raise PrometheosError(
            "validation_error",
            f"{state.kind} cannot move from {current} to {to}",
            {"id": state.id, "from": current, "to": to},
        )
    if rule == "reason" and not (reason or "").strip():
        raise _invalid("this transition needs a recorded reason", id=state.id)
    protected = [f"{state.kind} {current}->{to}"] if rule == "protected" else []
    body = dict(state.body)
    body[field] = to
    if state.kind == "claim" and to == "supported_within_conditions":
        _require_support(txn, store_dir, state)
    elif state.kind == "item" and to == "adopted":
        rows = txn.conn.execute("SELECT id FROM records WHERE kind = 'action'").fetchall()
        linked = [
            r["id"]
            for r in rows
            if (a := txn.get(r["id"])) is not None and a.body.get("commitment_item_id") == state.id
        ]
        if len(linked) != 1:
            raise _invalid(
                "adoption needs exactly one action whose commitment_item_id is this item"
            )
        body["action_id"] = linked[0]
    elif state.kind == "action":
        body = _action_transition(txn, state, body, current, to, reason, evidence, protected)
    return body, protected


def _require_support(txn: Txn, store_dir: Path, claim: RecordState) -> None:
    from prometheos.app.reads import computed_availability

    rows = txn.conn.execute("SELECT id FROM records WHERE kind = 'assessment'").fetchall()
    for row in rows:
        assessment = txn.get(row["id"])
        if assessment is None or assessment.lifecycle != "active":
            continue
        body = assessment.body
        if (
            body["claim_ref"]["id"] != claim.id
            or body["outcome"] != "supports"
            or not body["scope_match"]
        ):
            continue
        cited = list(claim.body.get("evidence", []))
        for check in body.get("checks", []):
            cited.extend(check.get("evidence", []))
        for ev in cited:
            version = txn.get(ev["version_id"])
            if version is None:
                continue
            if computed_availability(txn.conn, store_dir, version.body, version.id) == "retained":
                return
    raise PrometheosError(
        "validation_error",
        "support needs a supporting, scope-matched assessment with accessible retained evidence",
        {"id": claim.id},
    )


def _check_start(txn: Txn, state: RecordState, current: str, protected: list[str]) -> None:
    """A06.1 (CP20.3): starting a verification action. Inside the owner's budget an agent's
    start needs no approval and is recorded for today's total; every other start is a protected
    effect. Ordinary work items start only from ready, as before."""
    from prometheos.app import verification

    if not state.body.get("verification"):
        if current == "proposed":
            raise _invalid("only a planned check starts straight from proposed", id=state.id)
        return
    eligible, _code, _why = verification.eligibility(txn.conn, state.id, state.body, state.rev)
    if eligible:
        txn.audit(verification.BUDGET_START, state.id, state.rev + 1)
    else:
        protected.append(verification.OUTSIDE_BUDGET)


def _action_transition(
    txn: Txn,
    state: RecordState,
    body: dict[str, Any],
    current: str,
    to: str,
    reason: str | None,
    evidence: list[Any],
    protected: list[str],
) -> dict[str, Any]:
    if to == "ready":
        pending = [
            d
            for d in state.body.get("dependencies", [])
            if (dep := txn.get(d)) is None or dep.body.get("status") != "done"
        ]
        if pending:
            if not (reason or "").strip():
                raise _invalid(
                    "dependencies are not done; a waiver needs a reason", pending=pending
                )
            protected.append("dependency waiver")
    if to == "in_progress":
        _check_start(txn, state, current, protected)
    if current == "cancelled" and to == "proposed" and state.body.get("verification"):
        # Review cp20-3-a F1: a cancelled check comes back only by the owner's decision.
        protected.append("revive a cancelled check")
    if to == "blocked":
        body["blocker"] = reason
    elif current == "blocked":
        body["blocker"] = None
    if to == "done":
        if evidence:
            body["completion_evidence"] = [e.model_dump(mode="json") for e in evidence]
        elif (reason or "").strip():
            protected.append("completion without evidence")
        else:
            raise _invalid("done needs completion evidence or an owner-approved note")
    if current == "done" and to == "ready":
        body["completion_evidence"] = []
    return body


# ----- corrections -------------------------------------------------------------------------


def apply_correction(txn: Txn, correction: RecordState) -> list[tuple[RecordState, dict[str, Any]]]:
    """Validate a proposed correction against current revisions; return target updates."""
    if correction.body["status"] != "proposed":
        raise PrometheosError("validation_error", "only proposed corrections can be applied", {})
    model = CorrectionBody.model_validate(correction.body)
    updates: list[tuple[RecordState, dict[str, Any]]] = []
    for ref in model.replacement_refs:
        replacement = txn.get(ref.id)
        if replacement is None or replacement.rev != ref.rev:
            raise PrometheosError(
                "revision_conflict",
                "a replacement changed since the proposal; refresh and review it again",
                {"id": ref.id},
            )
        if replacement.body.get("state") in ("superseded", "withdrawn"):
            raise _invalid("replacement claim is itself superseded or withdrawn", id=ref.id)
        if replacement.body.get("superseded_by") is not None:
            raise _invalid("replacement assumption is itself superseded", id=ref.id)
    for effect in model.effects:
        target = txn.get(effect.target_ref.id)
        if target is None or target.rev != effect.target_ref.rev:
            raise PrometheosError(
                "revision_conflict",
                "a target changed since the proposal; refresh and review the whole correction",
                {"id": effect.target_ref.id},
            )
        body = dict(target.body)
        if effect.effect == "withdraw":
            body["state"] = "withdrawn"
        elif effect.effect == "contest":
            body["state"] = "contested"
        elif effect.effect == "supersede":
            assert effect.replacement_ref is not None
            if _supersede_cycle(txn, target.id, effect.replacement_ref.id):
                raise _invalid("supersession cycle")
            if target.kind == "entry":
                # A02 (CP10): an assumption entry has no state; the protected pointer is the
                # supersession, and dependents are flagged by the caller's closure.
                _check_assumption_supersession(txn, target.id, effect.replacement_ref)
            else:
                body["state"] = "superseded"
            body["superseded_by"] = effect.replacement_ref.model_dump(mode="json")
        else:
            _refuse_superseded_text(txn, target.id)
            body["text"] = effect.replacement_text
        updates.append((target, body))
    return updates
