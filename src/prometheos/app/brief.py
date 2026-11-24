"""The brief on Now (decision 0109): what needs the researcher now, at most a few lines, in a
fixed and explainable order, each with one sentence saying why it is shown.

The order is spec/05's rule for next steps, not a score: decisions waiting for the owner
(approval requests, cards among them), then due work, then evidence gaps of the research map
(claims not to quote, or waiting on a decision), then results waiting for an interpretation.
Within a group the order is the group's own (oldest request first, nearest deadline first, worst
status first). Nothing here is computed by a model, and the brief changes no record.
"""

from __future__ import annotations

from typing import Any

from prometheos.app.research_map import _line, _reason

BRIEF_LIMIT = 5
GROUPS = ("decide", "due", "evidence", "interpret")
# Claim statuses the brief raises, worst first: not to quote, then waiting on a decision.
GAP_ORDER = {"stale": 0, "conflicting": 1, "settling": 2}
GAP_WHY = {
    "stale": "Do not quote it. {reason}",
    "conflicting": "Two records disagree. {reason}",
    "settling": "Waiting on your decision. {reason}",
}


def gaps(statuses: list[dict[str, Any]], claims: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The claims to raise, worst status first, then by statement (stable across stores)."""
    out = []
    for item in statuses:
        status = str(item["status"])
        record = claims.get(str(item["claim_id"]))
        if status not in GAP_ORDER or record is None:
            continue
        reason = _reason(item["causes"]) or "See its details."
        out.append(
            {
                "group": "evidence",
                "kind": "claim",
                "id": item["claim_id"],
                "title": _line(record["body"]),
                "status": status,
                "why": GAP_WHY[status].format(reason=reason),
            }
        )
    return sorted(out, key=lambda i: (GAP_ORDER[i["status"]], i["title"], i["id"]))


def build(
    view: dict[str, Any], claim_gaps: list[dict[str, Any]], limit: int = BRIEF_LIMIT
) -> dict[str, Any]:
    """The brief from a ``now`` view (its sections carry each item's reason) and the map's gaps."""

    def section(group: str, name: str) -> list[dict[str, Any]]:
        return [
            {
                "group": group,
                "kind": i["kind"],
                "id": i["id"],
                "title": i["title"],
                "why": i["reason"],
            }
            for i in view[name]["items"]
        ]

    counts = {
        "decide": int(view["approvals"]["count"]),
        "due": int(view["due"]["count"]),
        "evidence": len(claim_gaps),
        "interpret": int(view["awaiting_interpretation"]["count"]),
    }
    # A section may list fewer items than it counts (``now_get``'s limit). The brief stops after
    # such a group, so nothing from a later group is shown before the rest of an earlier one
    # (review map-ai-spread-a R1-F7).
    ordered: list[dict[str, Any]] = []
    for group, items in (
        ("decide", section("decide", "approvals")),
        ("due", section("due", "due")),
        ("evidence", claim_gaps),
        ("interpret", section("interpret", "awaiting_interpretation")),
    ):
        ordered += items
        if len(items) < counts[group] or len(ordered) >= limit:
            break
    return {
        "generation": "deterministic",
        "rule": "decisions waiting for you, then due work, then claims not to quote or waiting "
        "on you, then results to interpret",
        "items": ordered[:limit],
        "total": sum(counts.values()),
        "counts": counts,
    }


__all__ = ["BRIEF_LIMIT", "GROUPS", "build", "gaps"]
