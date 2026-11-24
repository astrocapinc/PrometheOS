"""Re-adoption: what changed between two scan generations, and since the accepted baseline
(A05, CP17.6; C17-INCREMENTAL; decision 0045).

Files are compared by path and content digest, never by modification time:

- ``unchanged``: the same path with the same digest;
- ``changed``: the same path with another digest (a placeholder that became readable is changed);
- ``moved``: a path only in the newer generation whose digest equals that of a path only in
  the older one (paired one to one, in path order);
- ``added`` / ``deleted``: the rest.

Against a baseline, each family the owner accepted also says what became of its current file
(kept, changed, moved, deleted). Re-analysis follows from the digests: the analysis cache keys
on content, so a rescan extracts only changed, moved-and-changed and added files again (the
scan's ``extracted`` and ``replayed`` counts show it).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from prometheos.adoption import tables
from prometheos.adoption.contracts import AdoptionFile
from prometheos.domain.models import PrometheosError, utc_now

CHANGES = ("added", "changed", "moved", "deleted")
MAX_ENTRIES = 500


def _files(rows: list[AdoptionFile]) -> dict[str, AdoptionFile]:
    return {r.path: r for r in rows if r.entry == "file"}


def compare(older: list[AdoptionFile], newer: list[AdoptionFile]) -> list[dict[str, Any]]:
    """Every difference between two generations' files (unchanged files are left out)."""
    before, after = _files(older), _files(newer)
    out: list[dict[str, Any]] = []
    for path in sorted(set(before) & set(after)):
        if before[path].sha256 != after[path].sha256:
            out.append(_entry("changed", path, None, before[path], after[path]))
    gone = sorted(set(before) - set(after))
    new = sorted(set(after) - set(before))
    by_digest: dict[str, list[str]] = defaultdict(list)
    for path in gone:
        old_digest = before[path].sha256
        if old_digest:
            by_digest[old_digest].append(path)
    moved_from: set[str] = set()
    for path in new:
        digest = after[path].sha256
        candidates = by_digest.get(digest or "", [])
        if digest and candidates:
            source = candidates.pop(0)
            moved_from.add(source)
            out.append(_entry("moved", path, source, before[source], after[path]))
        else:
            out.append(_entry("added", path, None, None, after[path]))
    for path in gone:
        if path not in moved_from:
            out.append(_entry("deleted", path, None, before[path], None))
    return sorted(out, key=lambda e: (CHANGES.index(e["change"]), e["path"]))


def _entry(
    change: str,
    path: str,
    source: str | None,
    before: AdoptionFile | None,
    after: AdoptionFile | None,
) -> dict[str, Any]:
    return {
        "change": change,
        "path": path,
        "from": source,
        "sha256_before": before.sha256 if before else None,
        "sha256_after": after.sha256 if after else None,
        "status_after": after.status if after else None,
    }


def families_since(
    payload: dict[str, Any], newer: list[AdoptionFile], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """What became of each accepted family's current file since the baseline."""
    after = _files(newer)
    moves = {e["from"]: e["path"] for e in entries if e["change"] == "moved"}
    out = []
    for family in payload.get("families", []):
        current, digest = family.get("current"), family.get("current_sha256")
        if current is None:
            change, now = "undecided", None
        elif current in after and after[current].sha256 == digest:
            change, now = "kept", current
        elif current in after:
            change, now = "changed", current
        elif current in moves:
            change, now = "moved", moves[current]
        else:
            change, now = "deleted", None
        out.append(
            {
                "family_id": family["family_id"],
                "key": family["key"],
                "current": current,
                "change": change,
                "now": now,
            }
        )
    return out


def view(
    conn: sqlite3.Connection,
    scan_gen: int,
    *,
    against: str = "previous",
    change: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Differences of one generation from the previous complete generation of the same root
    (``against="previous"``) or from an accepted baseline (``against`` a ``bl_`` ID)."""
    from prometheos.adoption import plan

    if change is not None and change not in CHANGES:
        raise PrometheosError("validation_error", "unknown change", {"allowed": list(CHANGES)})
    if not 1 <= limit <= MAX_ENTRIES:
        raise PrometheosError("validation_error", f"limit is 1..{MAX_ENTRIES}", {"limit": limit})
    scan = tables.require_scan(conn, scan_gen)
    if scan.state != "complete":
        raise PrometheosError("validation_error", "compare a complete scan", {})
    payload: dict[str, Any] | None = None
    if against == "previous":
        base_gen = scan.config.previous_gen
        base_kind = "previous"
        baseline_id = None
    elif against.startswith("bl_"):
        baseline = plan.baseline(conn, against)
        if baseline["payload"].get("root_alias") != scan.root_alias:
            raise PrometheosError("validation_error", "the baseline is of another folder", {})
        base_gen, base_kind, baseline_id = baseline["scan_gen"], "baseline", against
        payload = baseline["payload"]
    else:
        raise PrometheosError("validation_error", "against is 'previous' or a baseline ID", {})
    if base_gen is None:
        entries: list[dict[str, Any]] = []
    else:
        older = tables.require_scan(conn, base_gen)
        if older.state != "complete":
            raise PrometheosError("validation_error", "the generation compared with is gone", {})
        entries = compare(tables.all_files(conn, base_gen), tables.all_files(conn, scan_gen))
    counts = {c: sum(1 for e in entries if e["change"] == c) for c in CHANGES}
    shown = [e for e in entries if change is None or e["change"] == change]
    page = shown[offset : offset + limit]
    end = offset + len(page)
    result: dict[str, Any] = {
        "scan_gen": scan_gen,
        "root_alias": scan.root_alias,
        "against": {"kind": base_kind, "scan_gen": base_gen, "baseline_id": baseline_id},
        "counts": counts,
        "analysis": {
            "extracted": scan.analysis.extracted if scan.analysis else 0,
            "replayed": scan.analysis.replayed if scan.analysis else 0,
            # what this scan could not analyse here (decision 0053, review cp17-0053-a F3)
            "analyzers_unavailable": [
                u.model_dump(mode="json")
                for u in (scan.analysis.analyzers_unavailable if scan.analysis else [])
            ],
        },
        "total": len(shown),
        "offset": offset,
        "next_offset": end if end < len(shown) else None,
        "entries": page,
        "families": [],
    }
    if payload is not None:
        result["families"] = families_since(payload, tables.all_files(conn, scan_gen), entries)
    return result


# ----- retention (decision 0039 section 9, carried F11) ----------------------------------------

KEEP_GENERATIONS = 5


def prune_generations(
    conn: sqlite3.Connection, root_alias: str, keep: int = KEEP_GENERATIONS
) -> list[int]:
    """Inside the caller's transaction: drop the rows (inventory, evidence, families) of a
    folder's complete generations older than the newest ``keep``, except generations a baseline
    refers to. The scan row stays, in state ``pruned``, so generation numbers and runs still
    resolve. Returns the generations pruned. Only PrometheOS's derived data is removed; the
    folder is not touched and a rescan recreates everything."""
    gens = [
        int(r[0])
        for r in conn.execute(
            "SELECT scan_gen FROM adoption_scans WHERE root_alias = ? AND state = 'complete' "
            "AND scan_gen NOT IN (SELECT scan_gen FROM adoption_pruned) ORDER BY scan_gen DESC",
            (root_alias,),
        ).fetchall()
    ]
    protected = {int(r[0]) for r in conn.execute("SELECT scan_gen FROM adoption_baselines")}
    pruned = [g for g in gens[keep:] if g not in protected]
    for gen in pruned:
        for table in ("adoption_files", "adoption_evidence", "adoption_families"):
            conn.execute(f"DELETE FROM {table} WHERE scan_gen = ?", (gen,))  # noqa: S608
        conn.execute(
            "INSERT INTO adoption_pruned (scan_gen, pruned_at) VALUES (?, ?)", (gen, utc_now())
        )
    return pruned
