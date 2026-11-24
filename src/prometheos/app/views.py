"""Pure research views: knowledge search/links, notebook, next steps, meeting agenda.

None of these write anything. All apply the caller's sensitivity policy and
report the project sequence they describe.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prometheos.app import continuity
from prometheos.app.context import Snapshot, coverage, load_snapshot
from prometheos.app.reads import Caller, project_seq
from prometheos.domain.models import PrometheosError
from prometheos.store.derived import search_mode

# ----- knowledge ---------------------------------------------------------------------------


def _fts_query(text: str) -> str:
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    if not tokens:
        raise PrometheosError("validation_error", "search needs at least one word", {})
    return " ".join('"' + t.replace('"', "") + '"' for t in tokens[:20])


def search(conn: sqlite3.Connection, caller: Caller, query: str, limit: int = 20) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise PrometheosError("validation_error", "limit must be 1..200", {})
    if search_mode(conn) == "fts5":
        rows = conn.execute(
            "SELECT s.record_id, s.kind, s.title, s.text FROM search_fts f JOIN search_text s "
            "ON s.rowid = f.rowid WHERE search_fts MATCH ? ORDER BY bm25(search_fts) LIMIT ?",
            (_fts_query(query), limit * 5),
        ).fetchall()
    else:
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT record_id, kind, title, text FROM search_text "
            "WHERE title LIKE ? OR text LIKE ? "
            "LIMIT ?",
            (like, like, limit * 5),
        ).fetchall()
    hits = []
    for row in rows:
        visible = conn.execute(
            "SELECT sensitivity, lifecycle FROM records WHERE id = ?", (row["record_id"],)
        ).fetchone()
        if visible is None or not caller.may_see(visible["sensitivity"]):
            continue
        if visible["lifecycle"] == "deleted":
            continue
        text = " ".join(str(row["text"]).split())
        hits.append(
            {
                "id": row["record_id"],
                "kind": row["kind"],
                "title": row["title"],
                "excerpt": text[:200],
                "lifecycle": visible["lifecycle"],
            }
        )
        if len(hits) == limit:
            break
    return {
        "project_seq": project_seq(conn),
        "mode": search_mode(conn),
        "query": query,
        "hits": hits,
    }


def links(conn: sqlite3.Connection, caller: Caller, record_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT sensitivity FROM records WHERE id = ?", (record_id,)).fetchone()
    if row is None or not caller.may_see(row["sensitivity"]):
        raise PrometheosError("not_found", "record not found", {"id": record_id})

    def visible(other: str) -> bool:
        r = conn.execute("SELECT sensitivity FROM records WHERE id = ?", (other,)).fetchone()
        return r is not None and caller.may_see(r["sensitivity"])

    outgoing = [
        {"id": r["to_id"], "rev": r["to_rev"], "via": r["field"]}
        for r in conn.execute(
            "SELECT * FROM refs WHERE from_id = ? ORDER BY field, to_id", (record_id,)
        )
        if visible(r["to_id"])
    ]
    incoming = [
        {"id": r["from_id"], "rev": r["from_rev"], "via": r["field"]}
        for r in conn.execute(
            "SELECT * FROM refs WHERE to_id = ? ORDER BY from_id, field", (record_id,)
        )
        if visible(r["from_id"])
    ]
    relations = [
        {"source": r["source_id"], "relation": r["relation"], "target": r["target_id"]}
        for r in conn.execute(
            "SELECT * FROM relations WHERE (source_id = ? OR target_id = ?) "
            "AND retired_seq IS NULL "
            "ORDER BY created_seq",
            (record_id, record_id),
        )
        if visible(r["source_id"]) and visible(r["target_id"])
    ]
    return {
        "id": record_id,
        "project_seq": project_seq(conn),
        "outgoing": outgoing,
        "incoming": incoming,
        "relations": relations,
        # A02, CP10.4: the attributed assumptions a claim or entry relies on (and, for an
        # assumption, what relies on it), with whether each is superseded or revised since.
        **continuity.assumption_links(conn, caller, record_id),
    }


# ----- notebook ------------------------------------------------------------------------------


def notebook(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    *,
    subject: str | None = None,
    at_seq: int | None = None,
) -> dict[str, Any]:
    snap = load_snapshot(conn, store_dir, caller, at_seq)
    flagged = {f["record_id"]: f for f in snap.flags}
    replaced = _replacements(snap)
    entries = []
    for entry in sorted(snap.of("entry"), key=lambda r: (snap.created[r["id"]], r["id"])):
        body = entry["body"]
        if subject and subject.lower() not in (body["title"] + " " + body["text"]).lower():
            continue
        sources = []
        for ev in body["evidence"]:
            version = snap.records.get(ev["version_id"])
            source = snap.records.get(ev["source_id"])
            # Dogfood F5: a receipt citation reads as the artifact it names, not as /outputs/0.
            cited = continuity.receipt_citation(conn, caller, version, ev["locator"])
            sources.append(
                {
                    "source_id": ev["source_id"],
                    "version_id": ev["version_id"],
                    "title": source["body"]["title"] if source else "(not visible)",
                    "availability": version["body"]["availability"] if version else "unknown",
                    "locator": ev["locator"],
                    "locator_label": cited["label"] if cited else None,
                    "artifact": cited,
                }
            )
        claims = []
        for ref in body["claim_refs"]:
            claim = snap.records.get(ref["id"])
            if claim:
                claims.append(
                    {
                        "id": ref["id"],
                        "state": claim["body"]["state"],
                        "superseded_by": claim["body"].get("superseded_by"),
                    }
                )
        entries.append(
            {
                "id": entry["id"],
                "rev": entry["rev"],
                "recorded_seq": snap.created[entry["id"]],
                "recorded_at": entry["recorded_at"],  # time of the shown revision
                "occurred_at": entry["occurred_at"],
                "title": body["title"],
                "category": body["category"],
                "origin": body["origin"],
                "review": body["review"],
                "text": body["text"],
                "correction_notice": replaced.get(entry["id"]),
                "needs_review": flagged.get(entry["id"]),
                "sources": sources,
                "claims": claims,
                "quantities": body["quantities"],
            }
        )
    return {
        "project_seq": snap.seq,
        "generation": "deterministic",
        "purged_records_omitted": snap.purged_omitted,
        "subject": subject,
        "entries": entries,
    }


def _replacements(snap: Snapshot) -> dict[str, str]:
    notes = {}
    for corr in snap.of("correction"):
        if corr["body"]["status"] != "applied":
            continue
        for effect in corr["body"]["effects"]:
            if effect["effect"] == "replace_text":
                notes[effect["target_ref"]["id"]] = (
                    f"Text replaced by correction {corr['id']} "
                    f"at seq {corr['body']['applied_seq']}; "
                    f"the original wording remains in revision {effect['target_ref']['rev']}."
                )
    return notes


def notebook_markdown(view: dict[str, Any]) -> str:
    lines = [f"# Notebook (project_seq {view['project_seq']}, deterministic)", ""]
    for e in view["entries"]:
        lines.append(f"## {e['title']}  ")
        lines.append(
            f"`{e['id']}` rev {e['rev']} · {e['category']} · origin {e['origin']} · {e['review']}"
        )
        if e["correction_notice"]:
            lines.append(f"> **Correction:** {e['correction_notice']}")
        if e["needs_review"]:
            lines.append(f"> **Needs review:** depends on {e['needs_review']['cause_id']}")
        lines.append("")
        lines.append(e["text"])
        for s in e["sources"]:
            where = s["locator_label"] or s["locator"]
            lines.append(
                f"- Source: {s['title']} (`{s['version_id']}`, {s['availability']}, {where})"
            )
        for c in e["claims"]:
            note = f" → superseded by `{c['superseded_by']['id']}`" if c["superseded_by"] else ""
            lines.append(f"- Claim `{c['id']}`: {c['state']}{note}")
        for q in e["quantities"]:
            lines.append(
                f"- Quantity: {q['decimal']} {q['unit']} "
                f"(bound to `{q['source_ref']['version_id']}` {q['source_pointer']})"
            )
        lines.append("")
    return "\n".join(lines)


# ----- next steps and agenda ------------------------------------------------------------------


def _candidate(kind: str, refs: list[str], **fields: Any) -> dict[str, Any]:
    digest = hashlib.sha256(("|".join([kind, *sorted(refs)])).encode()).hexdigest()[:16]
    return {"id": f"next_{digest}", "kind": kind, "source_refs": refs, **fields}


def next_steps(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, at_seq: int | None = None
) -> dict[str, Any]:
    snap = load_snapshot(conn, store_dir, caller, at_seq)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    blocked, due, evidence, explore = [], [], [], []
    for action in snap.of("action"):
        b = action["body"]
        if b["status"] == "blocked":
            blocked.append(
                _candidate(
                    "remove_blocker",
                    [action["id"]],
                    why_now=f"Adopted action is blocked: {b['blocker']}",
                    expected_output="Blocker resolved or action re-planned",
                    dependencies=b["dependencies"],
                    effort="unknown",
                    would_change="Blocker removed or action cancelled",
                    adopted_action_id=action["id"],
                )
            )
        elif b["status"] in ("ready", "in_progress"):
            overdue = b["due_at"] is not None and b["due_at"] < now
            if overdue or b["owner_ref"] is None:
                why = "Overdue adopted work" if overdue else "Adopted work has no owner recorded"
                due.append(
                    _candidate(
                        "due_adopted_work",
                        [action["id"]],
                        why_now=f"{why}: {b['title']}",
                        expected_output="Completion evidence attached or owner/date agreed",
                        dependencies=b["dependencies"],
                        effort="unknown",
                        would_change="Owner/date recorded or completion evidence attached",
                        adopted_action_id=action["id"],
                    )
                )
    challenged = {
        a["body"]["claim_ref"]["id"]: a["id"]
        for a in snap.of("assessment")
        if a["body"]["outcome"] == "challenges"
    }
    for claim in snap.of("claim"):
        state = claim["body"]["state"]
        if state == "contested" or (
            claim["id"] in challenged and state not in ("superseded", "withdrawn")
        ):
            refs = [claim["id"]] + ([challenged[claim["id"]]] if claim["id"] in challenged else [])
            evidence.append(
                _candidate(
                    "resolve_challenge",
                    refs,
                    why_now=(
                        f"Claim is {state} with unresolved challenge: "
                        f"{claim['body']['statement'][:120]}"
                    ),
                    expected_output="Scoped correction proposal or assessment narrowing the claim",
                    dependencies=[],
                    effort="medium",
                    would_change="Correction applied or challenge assessed as out of scope",
                    adopted_action_id=None,
                )
            )
    for flag in snap.flags:
        evidence.append(
            _candidate(
                "review_dependent",
                [flag["record_id"], flag["cause_id"]],
                why_now=f"Depends on applied correction {flag['cause_id']}",
                expected_output="Dependent record revised or confirmed",
                dependencies=[],
                effort="small",
                would_change="Record revised after the correction",
                adopted_action_id=None,
            )
        )
    flagged = {flag["record_id"] for flag in snap.flags}
    for anchor in snap.of("anchor"):
        # Anchors flagged by an applied correction already appear above.
        if anchor["body"]["status"] == "needs_review" and anchor["id"] not in flagged:
            evidence.append(
                _candidate(
                    "review_anchor",
                    [anchor["id"], anchor["body"]["paper_id"]],
                    why_now=f"Manuscript anchor needs review: {anchor['body']['reason'][:160]}",
                    expected_output="Anchor rebound, confirmed or resolved by the researcher",
                    dependencies=[],
                    effort="small",
                    would_change="Owner clears the review flag",
                    adopted_action_id=None,
                )
            )
    for paper in snap.of("paper"):
        b = paper["body"]
        if b["status"] in ("accepted", "withdrawn"):
            continue
        unchecked = [
            r["id"]
            for r in b["requirements"]
            if r["applicability"] == "unknown" or r["status"] in ("unknown", "todo")
        ]
        if unchecked:
            evidence.append(
                _candidate(
                    "check_paper_requirements",
                    [paper["id"]],
                    why_now=(
                        f"{len(unchecked)} venue requirement(s) of paper '{b['title'][:80]}' "
                        "are not yet checked"
                    ),
                    expected_output="Applicability and status recorded with evidence",
                    dependencies=[],
                    effort="small",
                    would_change="Each requirement checked against the venue source",
                    adopted_action_id=None,
                )
            )
    settings = next(r for r in snap.records.values() if r["kind"] == "project")["body"]
    for vid in coverage(snap, settings)["missing_versions"]:
        evidence.append(
            _candidate(
                "restore_source",
                [vid],
                why_now="Cited source bytes are unavailable",
                expected_output="Source located, re-imported or marked unavailable",
                dependencies=[],
                effort="small",
                would_change="Source becomes reachable",
                adopted_action_id=None,
            )
        )
    for item in snap.of("item"):
        b = item["body"]
        if b["status"] == "proposed":
            explore.append(
                _candidate(
                    "review_meeting_item",
                    [item["id"]],
                    why_now=(
                        f"Proposed {b['type']} awaits the researcher's decision: {b['text'][:120]}"
                    ),
                    expected_output="Adopted, dismissed or left proposed with a reason",
                    dependencies=[],
                    effort="small",
                    would_change="Researcher adopts or dismisses it",
                    adopted_action_id=None,
                )
            )
    ordered = blocked + due + evidence + explore
    return {
        "project_seq": snap.seq,
        "generation": "deterministic",
        "purged_records_omitted": snap.purged_omitted,
        "ordering": [
            "blocked commitments",
            "due adopted work",
            "unresolved evidence",
            "exploratory",
        ],
        "candidates": ordered,
    }


def agenda(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, meeting_id: str
) -> dict[str, Any]:
    snap = load_snapshot(conn, store_dir, caller, None)
    if meeting_id not in snap.records or snap.records[meeting_id]["kind"] != "meeting":
        raise PrometheosError("not_found", "meeting not found", {"id": meeting_id})
    commitments, questions, proposals = [], [], []
    for item in snap.of("item"):
        b = item["body"]
        if b["meeting_id"] == meeting_id:
            continue
        if b["status"] == "adopted" and b["action_id"] in snap.records:
            action = snap.records[b["action_id"]]["body"]
            commitments.append(
                {
                    "item_id": item["id"],
                    "text": b["text"],
                    "action_id": b["action_id"],
                    "action_status": action["status"],
                    "completion_evidence": action["completion_evidence"],
                }
            )
        elif b["status"] == "proposed" and b["type"] == "question":
            questions.append(
                {"item_id": item["id"], "text": b["text"], "proposed_owner": b["proposed_owner"]}
            )
        elif b["status"] == "proposed":
            proposals.append({"item_id": item["id"], "type": b["type"], "text": b["text"]})
    return {
        "project_seq": snap.seq,
        "meeting_id": meeting_id,
        "prior_commitments": commitments,
        "unresolved_questions": questions,
        "proposed_discussion": proposals,
    }
