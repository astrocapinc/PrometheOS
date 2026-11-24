"""Planned checks and the owner's verification budget (A06.1, CP20.3; decision 0084).

PrometheOS's detector turns each uncertain claim (unverified, stale, conflicting, a missing
artefact) into one proposed verification action per claim and cause, with its kind and an
estimated cost. The plan lists the open checks cheapest first.

An agent may start a **budget-eligible** check (spec/04) without a per-item question:
- its kind is ``read_only`` and its estimate is set and within ``max_minutes_each``;
- either the detector created it and nobody has revised its ``verification`` since, or the
  owner adopted it (the protected ``proposed -> ready`` transition) after the last such revision;
- and the estimates of the checks started that way today, in the project's time zone, stay within
  ``max_minutes_per_day``.

Every other start of a verification action is a protected effect: ``approval_required`` for the
owner. Who created an action is read from its first audit row, whose actor is the detector's own
service actor (an agent writes through ``mcp``, ``http-agent`` or ``cli``); nothing a caller
supplies decides it. PrometheOS starts, schedules and wakes nothing: the detector only records
proposals, and an agent's start only records that it started.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prometheos.app import claim_status, outbox
from prometheos.app.reads import Caller, current_record
from prometheos.domain.bodies import DEFAULT_VERIFICATION_BUDGET, VerificationBudget
from prometheos.domain.canonical import dumps_canonical, loads_internal
from prometheos.domain.ids import new_id

DETECTOR_ACTOR = outbox.DETECTOR_ACTOR
BUDGET_START = "verification_budget_start"
OUTSIDE_BUDGET = "start a check outside the verification budget"
LABEL = "verification"
OPEN = ("proposed", "ready", "in_progress", "blocked")

# cause code -> (kind, estimated minutes or None for "from the recorded run", what to do).
# Read-only estimates are fixed and stated here; a rerun takes the producing run's time.
CHECKS: dict[str, tuple[str, int | None, str]] = {
    "no_assessment": (
        "read_only",
        10,
        "Re-read the cited evidence and record an assessment with expected, measured and "
        "tolerance.",
    ),
    "revised_since": (
        "read_only",
        10,
        "Re-check the revised claim against its evidence and record an assessment.",
    ),
    "inconclusive": (
        "read_only",
        10,
        "Re-read the evidence the inconclusive assessment used and settle what it shows.",
    ),
    "out_of_scope": (
        "read_only",
        10,
        "Check the evidence within the claim's stated scope and record an assessment.",
    ),
    "no_evidence": ("read_only", 10, "Find the artefact the claim rests on and cite it."),
    "evidence_missing": (
        "read_only",
        5,
        "Locate the cited artefact and check it against its recorded digest.",
    ),
    "same_quantity_two_values": (
        "read_only",
        5,
        "Compare the two bound values of the same quantity and their definitions.",
    ),
    "origin_changed": (
        "read_only",
        5,
        "Compare the changed file with the snapshot the claim cites.",
    ),
    "producer_newer": (
        "rerun",
        None,
        "Rerun the producing script on its newer version and compare the result.",
    ),
    "dependency_changed": (
        "rerun",
        None,
        "Redo the result under the revised assumption it depends on.",
    ),
    # Decision 0106: a script in the adopted folder changed after its output, and a run older
    # than a change of what the claim depends on. Both are settled only by running again.
    "producer_changed": (
        "rerun",
        None,
        "Rerun the changed script outside the adopted folder and compare its output with the "
        "one the claim cites.",
    ),
    "run_predates_change": (
        "rerun",
        None,
        "Rerun the result under the corrected or revised dependency and compare.",
    ),
}


def budget(conn: sqlite3.Connection) -> VerificationBudget:
    """The owner's budget, or the default (15 minutes each, 60 a day, no output folder)."""
    project = _project(conn)
    raw = project["body"].get("verification_budget") if project else None
    return DEFAULT_VERIFICATION_BUDGET if raw is None else VerificationBudget.model_validate(raw)


def _project(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT id FROM records WHERE kind = 'project' LIMIT 1").fetchone()
    return current_record(conn, str(row[0])) if row else None


def _zone(conn: sqlite3.Connection) -> ZoneInfo:
    project = _project(conn)
    name = project["body"].get("timezone") if project else None
    return ZoneInfo(str(name)) if name else ZoneInfo("UTC")


def _revision_body(conn: sqlite3.Connection, record_id: str, rev: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
        "WHERE v.record_id = ? AND v.rev = ?",
        (record_id, rev),
    ).fetchone()
    return loads_internal(row[0]) if row and row[0] is not None else None


def used_today(conn: sqlite3.Connection, moment: datetime | None = None) -> int:
    """Minutes of the checks started within the budget today, in the project's time zone."""
    zone = _zone(conn)
    today = (moment or datetime.now(UTC)).astimezone(zone).date()
    total = 0
    for record_id, rev, at in conn.execute(
        "SELECT record_id, rev, recorded_at FROM audit WHERE operation = ? ORDER BY seq",
        (BUDGET_START,),
    ):
        started = datetime.fromisoformat(str(at).replace("Z", "+00:00")).astimezone(zone)
        if started.date() != today:
            continue
        body = _revision_body(conn, str(record_id), int(rev)) or {}
        total += int((body.get("verification") or {}).get("estimated_minutes") or 0)
    return total


def _created_by_detector(conn: sqlite3.Connection, action_id: str) -> bool:
    row = conn.execute(
        "SELECT actor FROM audit WHERE record_id = ? AND rev = 1 ORDER BY seq, op_index LIMIT 1",
        (action_id,),
    ).fetchone()
    return bool(row and row[0] == DETECTOR_ACTOR)


def _last_verification_change(conn: sqlite3.Connection, action_id: str, current_rev: int) -> int:
    """The last revision whose ``verification`` differs from the one before it (1 if never)."""
    last, previous = 1, None
    for rev in range(1, current_rev + 1):
        body = _revision_body(conn, action_id, rev) or {}
        value = dumps_canonical(body.get("verification"))
        if previous is not None and value != previous:
            last = rev
        previous = value
    return last


def _adopted_after(conn: sqlite3.Connection, action_id: str, rev: int) -> bool:
    """The owner's adoption (the protected proposed -> ready transition) after ``rev``."""
    row = conn.execute(
        "SELECT 1 FROM transition_notes WHERE record_id = ? AND from_state = 'proposed' "
        "AND to_state = 'ready' AND rev > ? LIMIT 1",
        (action_id, rev),
    ).fetchone()
    return row is not None


def eligibility(
    conn: sqlite3.Connection,
    action_id: str,
    body: dict[str, Any],
    rev: int,
    moment: datetime | None = None,
) -> tuple[bool, str, str]:
    """(eligible, code, plain reason) for an agent starting this check now."""
    check = body.get("verification") or {}
    limits = budget(conn)
    if check.get("kind") != "read_only":
        return False, "not_read_only", f"A {check.get('kind', 'non-read-only')} check needs you."
    minutes = check.get("estimated_minutes")
    if minutes is None:
        return False, "no_estimate", "It has no time estimate."
    if int(minutes) > limits.max_minutes_each:
        return (
            False,
            "over_each",
            f"Its {minutes} min estimate exceeds {limits.max_minutes_each} min per check.",
        )
    changed = _last_verification_change(conn, action_id, rev)
    detector = _created_by_detector(conn, action_id) and changed == 1
    if not detector and not _adopted_after(conn, action_id, changed):
        return False, "not_adopted", "It was not planned by PrometheOS or adopted by you."
    used = used_today(conn, moment)
    if used + int(minutes) > limits.max_minutes_per_day:
        return (
            False,
            "over_day",
            f"Today's {used} of {limits.max_minutes_per_day} min would be exceeded.",
        )
    return True, "eligible", "Within your verification budget."


# ----- the detector --------------------------------------------------------------------


def proposals(conn: sqlite3.Connection, store_dir: Path, caller: Caller) -> list[dict[str, Any]]:
    """The create operations for each (claim, cause) that has no check yet (any status: a
    check the owner cancelled is not proposed again). Only claims the caller may see are
    considered, so an agent's pass never writes or names a check of a hidden claim; existing
    checks are counted at full visibility, so nothing is proposed twice."""
    from prometheos.app.research_map import _line

    owner = Caller(actor=DETECTOR_ACTOR, scope="owner")
    index = claim_status._Index(conn, store_dir, caller)
    taken: set[tuple[str, str]] = set()
    for row in claim_status._rows(conn, "action", owner):
        check = row["body"].get("verification")
        if not check or not _created_by_detector(conn, row["id"]):
            continue
        if check.get("claim_ref") is None:
            continue  # an adoption step's check (CP20.4 6c)
        for label in row["body"].get("labels", []):
            if label.startswith("cause:"):
                taken.add((check["claim_ref"]["id"], label.removeprefix("cause:")))
    ops: list[dict[str, Any]] = []
    for item in claim_status.statuses(conn, store_dir, caller):
        cid = item["claim_id"]
        claim = current_record(conn, cid)
        if claim is None or item["status"] in ("withdrawn", "superseded", "holds", "corrected"):
            continue
        for cause in item["causes"]:
            code = cause["code"]
            if code not in CHECKS or (cid, code) in taken:
                continue
            taken.add((cid, code))
            kind, minutes, what = CHECKS[code]
            if minutes is None:
                runs = [
                    p["minutes"]
                    for v in claim_status._evidence_versions(conn, claim)
                    for p in index.producers.get(v, [])
                    if p.get("minutes") is not None
                ]
                minutes = max(runs) if runs else None
            ops.append(
                {
                    "op": "create",
                    "kind": "action",
                    "id": new_id("action"),
                    "base_rev": 0,
                    "sensitivity": claim["sensitivity"],
                    "occurred_at": None,
                    "body": {
                        "title": f"Check: {_line(claim['body'])}"[:300],
                        "reason": claim_status_reason(cause),
                        "source_refs": [{"id": cid, "rev": claim["rev"]}],
                        "owner_ref": None,
                        "due_at": None,
                        "dependencies": [],
                        "status": "proposed",
                        "blocker": None,
                        "completion_evidence": [],
                        "commitment_item_id": None,
                        "description": what,
                        "labels": [LABEL, f"cause:{code}"],
                        "verification": {
                            "claim_ref": {"id": cid, "rev": claim["rev"]},
                            "kind": kind,
                            "estimated_minutes": minutes,
                            "expected": None,
                        },
                    },
                }
            )
    return ops


def claim_status_reason(cause: dict[str, Any]) -> str:
    from prometheos.app.research_map import PLAIN_REASON

    return PLAIN_REASON.get(cause["code"], str(cause["message"]))


# ----- the plan ------------------------------------------------------------------------


def plan(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, moment: datetime | None = None
) -> dict[str, Any]:
    """The open checks the caller may see, cheapest first, with whether an agent may start each
    within the budget now, and the budget with today's use."""
    from prometheos.app.research_map import _line

    limits = budget(conn)
    used = used_today(conn, moment)
    checks = []
    current_causes: dict[str, set[str]] | None = None
    for row in claim_status._rows(conn, "action", caller):
        body = row["body"]
        check = body.get("verification")
        if not check or body.get("status") not in OPEN:
            continue
        claim_id = (check.get("claim_ref") or {}).get("id")
        claim = current_record(conn, claim_id) if claim_id else None
        visible = claim is not None and caller.may_see(claim["sensitivity"])
        eligible, code, why = eligibility(conn, row["id"], body, row["rev"], moment)
        cause = next(
            (lb.removeprefix("cause:") for lb in body.get("labels", []) if lb.startswith("cause:")),
            None,
        )
        # A check whose cause the claim no longer has can be closed (gap audit #10).
        resolved = False
        if visible and cause is not None:
            if current_causes is None:
                current_causes = {
                    s["claim_id"]: {c["code"] for c in s["causes"]}
                    for s in claim_status.statuses(conn, store_dir, caller)
                }
            resolved = cause not in current_causes.get(str(claim_id), set())
        checks.append(
            {
                "action_id": row["id"],
                "rev": row["rev"],
                "title": body["title"],
                "status": body["status"],
                "kind": check["kind"],
                "estimated_minutes": check.get("estimated_minutes"),
                "claim_id": claim_id if visible else None,
                "claim_line": _line(claim["body"]) if visible and claim else None,
                "adoption_step": check.get("adoption_step"),
                "cause": cause,
                "resolved": resolved,
                "what": body.get("description"),
                "eligible": eligible,
                "eligibility": code,
                "why": why,
                "planned_by": "prometheos"
                if _created_by_detector(conn, row["id"])
                else "agent_or_owner",
            }
        )
    checks.sort(
        key=lambda c: (
            c["estimated_minutes"] is None,
            c["estimated_minutes"] or 0,
            c["kind"] != "read_only",
            c["title"],
        )
    )
    seq = conn.execute("SELECT project_seq FROM project_state").fetchone()
    return {
        "generation": "deterministic",
        "project_seq": int(seq[0]) if seq else 0,
        "budget": {
            "max_minutes_each": limits.max_minutes_each,
            "max_minutes_per_day": limits.max_minutes_per_day,
            "output_root_alias": limits.output_root_alias,
            "output_path": limits.output_path,
            "used_today": used,
            "remaining_today": max(0, limits.max_minutes_per_day - used),
            "timezone": str(_zone(conn)),
            "default": _project(conn) is None
            or (_project(conn) or {}).get("body", {}).get("verification_budget") is None,
        },
        "checks": checks,
    }


__all__ = [
    "BUDGET_START",
    "CHECKS",
    "DETECTOR_ACTOR",
    "OUTSIDE_BUDGET",
    "budget",
    "eligibility",
    "plan",
    "proposals",
    "used_today",
]
