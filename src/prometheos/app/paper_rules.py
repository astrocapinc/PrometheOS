"""CP7 rules for papers and manuscript anchors (spec/03, spec/05; decision 0007).

Like :mod:`prometheos.app.research`, every function runs inside the shared write
transaction and reports *protected effects*; ``apply_request`` refuses the request
unless one owner decision bound to it covers them. Two hooks keep anchors honest
without anyone having to remember: applying a correction moves anchors that use a
corrected claim to ``needs_review``, and registering a new manuscript version
rebinds an anchor only when its exact lines occur once in the new version.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from prometheos.app.research import max_rank, require_current, require_rev
from prometheos.domain.bodies import AnchorBody, PaperBody
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.store.project import RecordState, Txn

PAPER_ORDER = (
    "planning",
    "drafting",
    "internal_review",
    "ready_for_submission",
    "submitted",
    "revision",
    "accepted",
)
CREATABLE_PAPER_STATUS = ("planning", "drafting", "internal_review", "ready_for_submission")
PROTECTED_PAPER_STATUS = ("submitted", "accepted")
CHANGE_REF_KINDS = {"version", "source", "entry", "claim", "assessment", "correction", "anchor"}
# Manuscript formats read as text even when the platform's MIME table does not call them
# text/* (Windows maps .tex to application/x-tex).
TEXT_LIKE_MIMES = frozenset({"application/x-tex", "application/x-latex", "application/x-bibtex"})


def is_text_version(body: dict[str, Any]) -> bool:
    mime = str(body.get("mime", "")).split(";")[0].strip().lower()
    return (
        body.get("payload_schema") == "text" or mime.startswith("text/") or mime in TEXT_LIKE_MIMES
    )


def _invalid(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


# ----- papers -------------------------------------------------------------------------------


def check_paper(
    txn: Txn, record_id: str, model: PaperBody, previous: RecordState | None
) -> tuple[list[str], int]:
    """Validate references and history rules; return (protected effects, sensitivity rank)."""
    protected: list[str] = []
    states: list[RecordState] = []
    if previous is None and model.status not in CREATABLE_PAPER_STATUS:
        raise PrometheosError(
            "policy_denied",
            "papers are created in planning, drafting, internal_review or ready_for_submission; "
            "later statuses are recorded by transition",
            {"status": model.status},
        )
    for ref in model.claim_refs:
        states.append(require_rev(txn, ref.id, ref.rev, {"claim"}))
    if model.manuscript_source_id is not None:
        states.append(require_current(txn, model.manuscript_source_id, {"source"}))
    for ref in model.venue_source_refs:
        states.append(require_rev(txn, ref.id, ref.rev, {"version"}))
    for related in model.related_papers:
        if related.paper_id == record_id:
            raise _invalid("a paper cannot be related to itself")
        # The explanation describes the related paper, so it inherits its sensitivity.
        states.append(require_current(txn, related.paper_id, {"paper"}))
    for review_round in model.review_rounds:
        for ref in review_round.source_refs:
            states.append(require_rev(txn, ref.id, ref.rev, {"version"}))
        for item in review_round.items:
            for ref in item.change_refs:
                states.append(require_rev(txn, ref.id, ref.rev, CHANGE_REF_KINDS))
    before = PaperBody.model_validate(previous.body) if previous is not None else None
    if before is not None:
        _preserve_rounds(before, model)
    already = (
        {
            (r.id, i.id)
            for r in before.review_rounds
            for i in r.items
            if i.status == "resolved" and not _resolution_linked(record_id, i)
        }
        if before is not None
        else set()
    )
    for review_round in model.review_rounds:
        for item in review_round.items:
            if (
                item.status == "resolved"
                and not _resolution_linked(record_id, item)
                and (review_round.id, item.id) not in already
            ):
                protected.append("review item resolved by rationale")
                break
    return protected, max_rank(*states)


def _resolution_linked(paper_id: str, item: Any) -> bool:
    """A change or evidence other than the paper itself and the comment's own location."""
    changes = [r for r in item.change_refs if r.id != paper_id]
    evidence = [e for e in item.resolution_evidence if e != item.comment_source_ref]
    return bool(changes or evidence)


def _preserve_rounds(before: PaperBody, after: PaperBody) -> None:
    """Revisions preserve earlier review rounds and the recorded comments in them."""
    new_rounds = {r.id: r for r in after.review_rounds}
    order = [r.id for r in after.review_rounds]
    for position, old in enumerate(before.review_rounds):
        new = new_rounds.get(old.id)
        if new is None or order[position] != old.id:
            raise PrometheosError(
                "policy_denied",
                "review rounds are kept in order; earlier rounds cannot be removed",
                {"round": old.id},
            )
        if (old.label, old.source_refs, old.received_at) != (
            new.label,
            new.source_refs,
            new.received_at,
        ):
            raise PrometheosError(
                "policy_denied", "a recorded review round's label and sources are fixed", {}
            )
        items = {i.id: i for i in new.items}
        item_order = [i.id for i in new.items]
        for index, item in enumerate(old.items):
            current = items.get(item.id)
            if current is None or item_order[index] != item.id:
                raise PrometheosError(
                    "policy_denied", "review comments cannot be removed or reordered", {}
                )
            if (current.comment_source_ref, current.text) != (item.comment_source_ref, item.text):
                raise PrometheosError(
                    "policy_denied", "a recorded review comment and its source are fixed", {}
                )


def paper_transition(state: RecordState, to: str, reason: str | None) -> list[str]:
    """Check a paper status change; return protected effects."""
    current = state.body["status"]
    has_reason = bool((reason or "").strip())
    if to == current:
        raise _invalid("paper is already in that status", id=state.id)
    if to == "withdrawn":
        if not has_reason:
            raise _invalid("withdrawing a paper needs a recorded reason", id=state.id)
        return []
    if to not in PAPER_ORDER:
        raise _invalid(f"unknown paper status {to}", id=state.id)
    target = PAPER_ORDER.index(to)
    if current == "withdrawn":
        forward = False
    else:
        position = PAPER_ORDER.index(current)
        forward = target > position
        if forward and not (target == position + 1 or (current, to) == ("submitted", "accepted")):
            raise PrometheosError(
                "validation_error",
                f"paper cannot move from {current} to {to}; statuses advance one step at a time",
                {"id": state.id, "from": current, "to": to},
            )
    if not forward and not has_reason:
        raise _invalid("moving a paper back needs a recorded reason", id=state.id)
    return [f"paper {current}->{to}"] if to in PROTECTED_PAPER_STATUS else []


# ----- anchors -------------------------------------------------------------------------------


def check_anchor(txn: Txn, store_dir: Path, model: AnchorBody, previous: RecordState | None) -> int:
    """Validate an anchor's paper, manuscript binding and locator; return sensitivity rank."""
    from prometheos.app.apply import read_version_bytes, text_lines

    if previous is None and (model.status != "current" or model.reason is not None):
        raise PrometheosError(
            "policy_denied", "anchors are created current; review states are transitions", {}
        )
    paper = require_current(txn, model.paper_id, {"paper"})
    if paper.lifecycle != "active":
        raise _invalid("anchors bind active papers", id=model.paper_id)
    manuscript = paper.body.get("manuscript_source_id")
    if manuscript is None:
        raise _invalid("the paper has no manuscript source to anchor in", id=model.paper_id)
    version = require_current(txn, model.manuscript_version_id, {"version"})
    if version.body["source_id"] != manuscript:
        raise _invalid(
            "manuscript_version_id is not a version of the paper's manuscript source",
            id=model.manuscript_version_id,
        )
    data = read_version_bytes(txn, store_dir, version.body)
    if data is None:
        raise PrometheosError(
            "source_unavailable",
            "anchors bind retained manuscript bytes; this version is not retained or is corrupt",
            {"id": model.manuscript_version_id},
        )
    if model.locator.type == "text":
        lines = text_lines(data) if is_text_version(version.body) else None
        if lines is None:
            raise _invalid("a text locator needs a UTF-8 text manuscript version")
        if model.locator.end_line > len(lines):
            raise _invalid("anchor lines lie outside the manuscript version", lines=len(lines))
    elif not data.startswith(b"%PDF-"):
        raise _invalid("a PDF locator needs a PDF manuscript version")
    states = [paper, version]
    for ref in model.claim_refs:
        states.append(require_rev(txn, ref.id, ref.rev, {"claim"}))
    return max_rank(*states)


def anchor_transition(state: RecordState, to: str, reason: str | None) -> list[str]:
    current = state.body["status"]
    allowed = {
        ("current", "needs_review"): "reason",
        ("resolved", "needs_review"): "reason",
        ("needs_review", "current"): "protected",
        ("needs_review", "resolved"): "protected",
        ("current", "resolved"): "protected",
    }
    rule = allowed.get((current, to))
    if rule is None:
        raise PrometheosError(
            "validation_error",
            f"anchor cannot move from {current} to {to}",
            {"id": state.id, "from": current, "to": to},
        )
    if not (reason or "").strip():
        raise _invalid("anchor review changes need a recorded reason", id=state.id)
    return ["anchor review cleared"] if rule == "protected" else []


def anchor_body_after(state: RecordState, to: str, reason: str | None) -> dict[str, Any]:
    body = dict(state.body, status=to)
    body["reason"] = None if to == "current" else reason
    return body


def _write_anchor(
    txn: Txn, anchor: RecordState, body: dict[str, Any], operation: str, sensitivity: str
) -> None:
    from prometheos.app.apply import validate_body

    clean = validate_body("anchor", body).model_dump(mode="json")
    body_id = txn.insert_body(clean)
    txn.write_revision(
        anchor.id,
        "anchor",
        anchor.rev + 1,
        body_id,
        sensitivity,
        anchor.lifecycle,
        None,
        operation,
        body=clean,
    )


def _active_anchors(txn: Txn) -> list[RecordState]:
    rows = txn.conn.execute(
        "SELECT id FROM records WHERE kind = 'anchor' AND lifecycle = 'active' ORDER BY id"
    ).fetchall()
    return [s for r in rows if (s := txn.get(r["id"])) is not None]


def flag_anchors_for_correction(
    txn: Txn,
    correction_id: str,
    effects: dict[str, str],
    closure: dict[str, dict[str, Any]] | None = None,
    prefix: str | None = None,
) -> list[str]:
    """Move anchors that use a corrected claim or assumption to needs_review (same transaction).

    An anchor is hit when it names a corrected claim (as before), when the correction's
    affected closure reaches it (an assumption an anchor is ``used_in``, or a claim that
    relies on a superseded assumption), or when it names a claim the closure reached
    (decision 0018). Resolved anchors are reopened too: a resolved anchor that still relies on
    the corrected record would otherwise keep using it unseen."""
    closure = closure or {}
    prefix = prefix or f"correction {correction_id} applied"
    reached_claims = {rid for rid in closure if rid.startswith("claim_")}
    flagged: list[str] = []
    for anchor in _active_anchors(txn):
        if anchor.body["status"] == "needs_review":
            continue
        claim_ids = [r["id"] for r in anchor.body["claim_refs"]]
        direct = next((cid for cid in claim_ids if cid in effects), None)
        if direct is not None:
            reason = f"{prefix}: claim {direct} {effects[direct]}"
        elif anchor.id in closure:
            hop = closure[anchor.id]
            reason = (
                f"{prefix}: relies on {hop['via']} "
                f"(affected by {hop['root']} {effects.get(hop['root'], 'corrected')})"
            )
        else:
            via = next((cid for cid in claim_ids if cid in reached_claims), None)
            if via is None:
                continue
            root = closure[via]["root"]
            reason = (
                f"{prefix}: relies on {via} (affected by {root} {effects.get(root, 'corrected')})"
            )
        body = dict(anchor.body, status="needs_review", reason=reason)
        _write_anchor(txn, anchor, body, "anchor_review:correction", anchor.sensitivity)
        flagged.append(anchor.id)
    return flagged


def exact_matches(old: list[str], new: list[str]) -> list[int]:
    """1-based start lines where ``old`` occurs contiguously in ``new``."""
    width = len(old)
    if width == 0:
        return []
    return [i + 1 for i in range(len(new) - width + 1) if new[i : i + width] == old]


def on_manuscript_version(txn: Txn, store_dir: Path, source_id: str, version_id: str) -> None:
    """A new manuscript version: rebind exact matches, flag the rest for review."""
    from prometheos.app.apply import read_version_bytes, text_lines

    papers = {
        row["id"]
        for row in txn.conn.execute(
            "SELECT id FROM records WHERE kind = 'paper' AND lifecycle = 'active'"
        ).fetchall()
        if (p := txn.get(row["id"])) is not None and p.body.get("manuscript_source_id") == source_id
    }
    if not papers:
        return
    new_version = txn.get(version_id)
    assert new_version is not None
    new_data = read_version_bytes(txn, store_dir, new_version.body)
    new_lines = (
        text_lines(new_data) if new_data is not None and is_text_version(new_version.body) else None
    )
    for anchor in _active_anchors(txn):
        body = anchor.body
        if (
            body["paper_id"] not in papers
            or body["manuscript_version_id"] == version_id
            or body["status"] == "resolved"
        ):
            continue
        old_version = txn.get(body["manuscript_version_id"])
        if old_version is None or old_version.body.get("source_id") != source_id:
            continue
        sensitivity = max(
            anchor.sensitivity, new_version.sensitivity, key=lambda s: SENSITIVITY_ORDER[s]
        )
        reason = None
        locator = body["locator"]
        if locator["type"] != "text":
            reason = "a PDF anchor cannot be matched exactly in a new manuscript version"
        else:
            old_data = read_version_bytes(txn, store_dir, old_version.body)
            old_lines = (
                text_lines(old_data)
                if old_data is not None and is_text_version(old_version.body)
                else None
            )
            if old_lines is None or new_lines is None:
                reason = "the anchored or the new manuscript text is not available as UTF-8 text"
            else:
                segment = old_lines[locator["start_line"] - 1 : locator["end_line"]]
                matches = exact_matches(segment, new_lines)
                if len(matches) == 1:
                    start = matches[0]
                    remapped = dict(
                        body,
                        manuscript_version_id=version_id,
                        locator={
                            "type": "text",
                            "start_line": start,
                            "end_line": start + len(segment) - 1,
                        },
                    )
                    _write_anchor(txn, anchor, remapped, "anchor_exact_remap", sensitivity)
                    continue
                reason = (
                    "the anchored lines occur more than once in the new manuscript version"
                    if matches
                    else "the anchored lines no longer occur exactly in the new manuscript version"
                )
        if body["status"] == "needs_review":
            continue
        flagged = dict(
            body,
            status="needs_review",
            reason=f"manuscript version {version_id} registered: {reason}",
        )
        _write_anchor(txn, anchor, flagged, "anchor_review:manuscript", anchor.sensitivity)
