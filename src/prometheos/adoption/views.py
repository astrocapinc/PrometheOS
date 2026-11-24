"""Read views of adoption state (A05, CP17; decision 0039 section 5). Pure reads: nothing here
changes anything, and replies name files by root-relative path only."""

from __future__ import annotations

import sqlite3
from typing import Any

from prometheos.adoption import analyzers, tables
from prometheos.adoption.contracts import (
    CONTAINERS,
    FILE_STATUSES,
    PREDICATES,
    TIERS,
    ZONES,
)
from prometheos.domain.models import PrometheosError

BANDS = ("decided", "supported", "weak", "undecided")
MAX_FILES_PAGE = 500


def scans(conn: sqlite3.Connection, root_alias: str | None = None) -> dict[str, Any]:
    items = tables.list_scans(conn, root_alias)
    return {"scans": [s.model_dump(mode="json") for s in items], "total": len(items)}


def files(
    conn: sqlite3.Connection,
    scan_gen: int,
    *,
    status: str | None = None,
    zone: str | None = None,
    container: str | None = None,
    prefix: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    for name, value, allowed in (
        ("status", status, FILE_STATUSES),
        ("zone", zone, ZONES),
        ("container", container, CONTAINERS),
    ):
        if value is not None and value not in allowed:
            raise PrometheosError("validation_error", f"unknown {name}", {name: value[:40]})
    if not 1 <= limit <= MAX_FILES_PAGE:
        raise PrometheosError("validation_error", f"limit is 1..{MAX_FILES_PAGE}", {"limit": limit})
    scan = tables.require_scan(conn, scan_gen)
    rows, total = tables.list_files(
        conn,
        scan_gen,
        status=status,
        zone=zone,
        container=container,
        prefix=prefix,
        offset=offset,
        limit=limit,
    )
    end = offset + len(rows)
    return {
        "scan_gen": scan_gen,
        "root_alias": scan.root_alias,
        "state": scan.state,
        "total": total,
        "offset": offset,
        "next_offset": end if end < total else None,
        "files": [r.model_dump(mode="json") for r in rows],
    }


def evidence(
    conn: sqlite3.Connection,
    scan_gen: int,
    *,
    path: str | None = None,
    predicate: str | None = None,
    tier: str | None = None,
    analyzer: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Evidence of one generation; ``path`` selects everything said about that file, as
    subject or as object."""
    if predicate is not None and predicate not in PREDICATES:
        raise PrometheosError(
            "validation_error", "unknown predicate", {"predicate": predicate[:40]}
        )
    if tier is not None and tier not in TIERS:
        raise PrometheosError("validation_error", "unknown tier", {"tier": tier[:4]})
    if analyzer is not None and analyzer not in analyzers.ANALYZERS:
        raise PrometheosError("validation_error", "unknown analyzer", {"analyzer": analyzer[:60]})
    if not 1 <= limit <= MAX_FILES_PAGE:
        raise PrometheosError("validation_error", f"limit is 1..{MAX_FILES_PAGE}", {"limit": limit})
    scan = tables.require_scan(conn, scan_gen)
    rows, total = tables.list_evidence(
        conn,
        scan_gen,
        related=path,
        predicate=predicate,
        tier=tier,
        analyzer=analyzer,
        offset=offset,
        limit=limit,
    )
    end = offset + len(rows)
    return {
        "scan_gen": scan_gen,
        "root_alias": scan.root_alias,
        "total": total,
        "offset": offset,
        "next_offset": end if end < total else None,
        "evidence": [r.model_dump(mode="json") for r in rows],
    }


def analyzer_list() -> dict[str, Any]:
    """The declarations, and those this installation cannot run (decision 0053)."""
    return {
        "analyzers": [a.model_dump(mode="json") for a in analyzers.ANALYZERS.values()],
        "unavailable": [
            {"analyzer": a, "reason": why, "install": analyzers.INSTALL_HINT[a], "files": 0}
            for a, why in sorted(analyzers.unavailable().items())
        ],
    }


def families(
    conn: sqlite3.Connection,
    scan_gen: int,
    *,
    band: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Family summaries of one generation: key, kind, band, current member, member and conflict
    counts. Read one family for its members, ranks, reasons and conflicts."""
    if band is not None and band not in BANDS:
        raise PrometheosError("validation_error", "unknown band", {"band": band[:20]})
    if not 1 <= limit <= MAX_FILES_PAGE:
        raise PrometheosError("validation_error", f"limit is 1..{MAX_FILES_PAGE}", {"limit": limit})
    scan = tables.require_scan(conn, scan_gen)
    items = [f for f in tables.list_families(conn, scan_gen) if band is None or f.band == band]
    page = items[offset : offset + limit]
    end = offset + len(page)
    shown = _classes(conn, scan_gen, page)
    return {
        "scan_gen": scan_gen,
        "root_alias": scan.root_alias,
        "total": len(items),
        "offset": offset,
        "next_offset": end if end < len(items) else None,
        "families": [
            {
                "family_id": f.family_id,
                "key": f.key,
                "kind": f.kind,
                "band": f.band,
                "deciding_rule": f.deciding_rule,
                "current": f.current,
                "members": len(f.members),
                "conflicts": len(f.conflicts),
                **shown[f.family_id],
            }
            for f in page
        ],
    }


def family(conn: sqlite3.Connection, scan_gen: int, family_id: str) -> dict[str, Any]:
    tables.require_scan(conn, scan_gen)
    found = tables.get_family(conn, scan_gen, family_id)
    return found.model_dump(mode="json") | _classes(conn, scan_gen, [found])[family_id]


def _classes(
    conn: sqlite3.Connection, scan_gen: int, items: list[Any]
) -> dict[str, dict[str, Any]]:
    """A06.1 (CP20.4): each family's class by the rules, the owner's class decision when one
    applies, the class's default member and whether it asks the owner (files an item)."""
    from prometheos.adoption import assist, plan

    rows = {r.path: r for r in tables.all_files(conn, scan_gen)}
    digests = plan.digest_map(rows)
    decided = plan.class_decisions(conn, scan_gen)
    proposals = assist.family_proposals(conn)
    out = {}
    for f in items:
        found = plan.family_class(f, digests, decided)
        # A06.1 under A07 (decision 0107): an accepted proposal for this exact member set, shown
        # beside the class; it never sets the class or answers the family.
        proposal = proposals.get((f.key, assist.family_digest(f, rows)))
        out[f.family_id] = {
            "class": found["class"],
            "class_reason": found["reason"],
            "class_default": found["default"],
            "class_decision": found["decision"],
            # The same test the plan uses to file an owner step (plan.derive).
            "class_proposal": None
            if proposal is None or found["class"] != "compete"
            else {**proposal["output"], "models": proposal["models"]},
            "needs_owner": found["files_item"]
            and f.deciding_rule != "R1"
            and not (
                f.band in ("decided", "supported") and not f.conflicts and not found["off_default"]
            ),
        }
    return out
