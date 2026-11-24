"""Decision cards and "apply all recommended" (A06.1, CP20.3; decision 0084).

A card travels in the request an agent submits, so an approval request keeps it in its plan:
``{what, why, recommendation, default_text, default_at}``. A card's plan is its recommendation.
The read lists the pending card-carrying approval requests the caller may see. Each card is shown
with the claims its plan names, and with whether its default date has passed.

A passed default changes only what views say. The claim stays ``settling``, and the card reads
"default in effect in views". It never changes a record, a manuscript workspace file or an anchor,
never grants, applies, transitions or changes support (spec/04).

"Apply all recommended" folds the listed plans into one request:
- the owner previews it and approves its digest once;
- it is applied in one transaction, which rechecks every operation's base revision and applies
  nothing if one changed;
- the folded requests are then closed as applied together.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from prometheos.app.reads import Caller, current_record
from prometheos.domain.canonical import dumps_canonical
from prometheos.domain.ids import derived_request_id
from prometheos.domain.models import PrometheosError

MAX_BUNDLE = 50


def _ids_named(value: Any, found: list[str]) -> None:
    """Every claim and correction ID anywhere in a plan's operations."""
    if isinstance(value, dict):
        for item in value.values():
            _ids_named(item, found)
    elif isinstance(value, list):
        for item in value:
            _ids_named(item, found)
    elif (
        isinstance(value, str)
        and value.startswith(("claim_", "correction_"))
        and value not in found
    ):
        found.append(value)


def _claims_named(conn: sqlite3.Connection, plan: dict[str, Any]) -> list[str]:
    """The claims a plan is about: named directly, or targeted by a correction it names."""
    named: list[str] = []
    _ids_named(plan.get("operations", []), named)
    claims: list[str] = []
    for rid in named:
        if rid.startswith("correction_"):
            correction = current_record(conn, rid)
            targets = (correction or {}).get("body", {}).get("target_refs", [])
            claims.extend(t["id"] for t in targets if str(t.get("id", "")).startswith("claim_"))
        else:
            claims.append(rid)
    return list(dict.fromkeys(claims))


def _effects(summary: str | None) -> list[str]:
    try:
        value = json.loads(summary) if summary else []
    except ValueError:
        return [str(summary)]
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _plan_current(conn: sqlite3.Connection, plan: dict[str, Any]) -> bool:
    """Whether every record the plan's operations revise, transition or apply is still at the
    revision the plan names (review cp20-3-a F9: a stale card says so instead of waiting)."""
    for op in plan.get("operations", []):
        if isinstance(op, dict) and "id" in op and int(op.get("base_rev") or 0) > 0:
            record = current_record(conn, str(op["id"]))
            if record is None or record["rev"] != op["base_rev"]:
                return False
            # Review cp20-3-a-r2 R2-1: applying a correction rechecks its targets' revisions,
            # so a claim revised since the card was made makes the card stale too.
            if op.get("op") == "correction_apply":
                body = record["body"]
                for target in [*body.get("target_refs", []), *body.get("replacement_refs", [])]:
                    current = current_record(conn, str(target.get("id")))
                    if current is None or current["rev"] != target.get("rev"):
                        return False
    return True


def pending(conn: sqlite3.Connection, rank: int, moment: datetime | None = None) -> list[Any]:
    """Pending, unexpired approval requests with a card, visible at ``rank``, oldest first."""
    now = (moment or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT id, plan, plan_sha256, summary, actor, created_at, expires_at, caller_rank FROM "
        "approval_requests WHERE state = 'pending' AND operation = 'apply' AND caller_rank <= ? "
        "AND expires_at > ? ORDER BY created_at, rowid",
        (rank, now),
    ).fetchall()
    out = []
    for row in rows:
        plan = json.loads(row[1])
        if isinstance(plan, dict) and plan.get("card"):
            out.append((row, plan))
    return out


def carded_claims(conn: sqlite3.Connection, rank: int) -> set[str]:
    """Claims a waiting card's plan names (claim status: settling, ``card_waiting``)."""
    found: set[str] = set()
    for _row, plan in pending(conn, rank):
        found.update(_claims_named(conn, plan))
    return found


def view(
    conn: sqlite3.Connection, caller: Caller, moment: datetime | None = None
) -> dict[str, Any]:
    from prometheos.app import card_layer
    from prometheos.app.research_map import _line

    now = moment or datetime.now(UTC)
    wordings = card_layer.wordings(conn, caller)
    cards = []
    # Expiry follows the real clock; ``moment`` only decides whether a default date passed.
    for row, plan in pending(conn, caller.rank):
        card = plan["card"]
        claims = []
        for cid in _claims_named(conn, plan):
            claim = current_record(conn, cid)
            if claim is not None and caller.may_see(claim["sensitivity"]):
                claims.append({"claim_id": cid, "line": _line(claim["body"])})
        due = card.get("default_at")
        passed = bool(due) and datetime.fromisoformat(str(due).replace("Z", "+00:00")) <= now
        cards.append(
            {
                "approval_request_id": row[0],
                "plan_sha256": row[2],
                "card": card,
                "protected_effects": _effects(row[3]),
                "requested_by": row[4],
                "created_at": row[5],
                "expires_at": row[6],
                "claims": claims,
                "default_state": "default_in_effect" if passed else "waiting",
                "plan_current": _plan_current(conn, plan),
                "generated": True,
                # Decision 0110 (L3): the layer's plain wording, placeholders filled by code; a
                # hint beside the agent's words. The card and its default are unchanged.
                "wording": wordings.get(row[0]),
            }
        )
    seq = conn.execute("SELECT project_seq FROM project_state").fetchone()
    return {
        "generation": "deterministic",
        "project_seq": int(seq[0]) if seq else 0,
        "cards": cards,
        # A card whose plan no longer applies is not offered in "apply all recommended".
        "recommended": [c["approval_request_id"] for c in cards if c["plan_current"]],
    }


def bundle(
    conn: sqlite3.Connection, project_id: str, rank: int, approval_ids: list[str]
) -> dict[str, Any]:
    """One request holding the listed plans' operations, in the listed order, at the current
    project sequence. The request ID is derived from the listed plans, so the same set gives the
    same request (and the digest excludes it anyway)."""
    if not approval_ids or len(approval_ids) > MAX_BUNDLE:
        raise PrometheosError("validation_error", f"list 1 to {MAX_BUNDLE} approval requests", {})
    if len(set(approval_ids)) != len(approval_ids):
        raise PrometheosError("validation_error", "an approval request is listed twice", {})
    by_id = {row[0]: (row, plan) for row, plan in pending(conn, rank)}
    operations: list[Any] = []
    digests = []
    for aid in approval_ids:
        if aid not in by_id:
            raise PrometheosError(
                "not_found",
                "not a pending approval request with a card",
                {"approval_request_id": aid[:80]},
            )
        row, plan = by_id[aid]
        operations.extend(plan["operations"])
        digests.append(row[2])
    seq = int(conn.execute("SELECT project_seq FROM project_state").fetchone()[0])
    request = {
        "schema_version": 1,
        "project_id": project_id,
        "request_id": derived_request_id("apply_recommended", project_id, *approval_ids, *digests),
        "expected_seq": seq,
        "operations": operations,
    }
    return {
        "approval_request_ids": approval_ids,
        "plan_sha256s": digests,
        "request": request,
        "raw": dumps_canonical(request),
    }


__all__ = ["bundle", "carded_claims", "pending", "view"]
