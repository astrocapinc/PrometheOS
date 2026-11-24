"""The research map (A06.1, CP20.2; decision 0083): the objective, its deliverables and their
stages, the questions each serves, the claims answering them and the tracks they draw on.

Everything here is derived from records and three descriptive relations (``answers``,
``addresses``, ``draws_on``) in one read. A deliverable's stage is never typed by hand: framed
(questions address the paper), evidence mapped (each question has a claim, and every claim
answering one cites retained evidence), verified (every such claim holds or is corrected, with
the agent-reported ones listed for the owner to confirm), then the paper's own status. Each step
lists what keeps the paper from it.

Only records the caller may see feed the map, as for claim status (review cp20-1-a F1). The read
changes nothing, promotes nothing and is not support.

A track the owner confirmed in an adoption outline names the folders it forks by. A claim whose
retained evidence lies inside one of those folders is shown as drawing on that track "by file
location" (decision 0108): derived at read time, kept apart from the stored ``draws_on`` links,
and never written.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from prometheos.app import claim_status
from prometheos.app.claim_status import _evidence_versions, _rows
from prometheos.app.reads import Caller, computed_availability, current_record
from prometheos.domain.models import PrometheosError

MAP_RELATIONS = ("answers", "addresses", "draws_on")
STEPS = (
    ("framed", "Framed"),
    ("evidence_mapped", "Evidence mapped"),
    ("verified", "Verified"),
)
# Claims out of use: shown under their question, but they neither block nor satisfy a stage
# (relations are not retired, so a superseded claim would otherwise block forever; 0083).
OUT_OF_USE = frozenset({"withdrawn", "superseded"})
VERIFIED = frozenset({"holds", "corrected"})
LINE_CHARS = 120


def _line(body: dict[str, Any]) -> str:
    """The claim's one line: its summary, or its statement cut to one line."""
    summary = body.get("summary")
    if summary:
        return str(summary)
    text = " ".join(str(body.get("statement", "")).split())
    return text if len(text) <= LINE_CHARS else text[: LINE_CHARS - 1].rstrip() + "…"


# Cause messages that may name a file; the map's one-line reason uses this wording instead, so a
# path never shows outside a claim's details (review cp20-2-a F9).
PLAIN_REASON = {
    "producer_newer": "A file that produced its evidence has a newer version.",
    "producer_changed": "The script that writes its evidence changed since the evidence was "
    "written.",
}


def _reason(causes: list[dict[str, Any]]) -> str:
    """The first cause other than plain support, worded without file names."""
    for cause in causes:
        if cause["code"] != "supported":
            return PLAIN_REASON.get(cause["code"], str(cause["message"]))
    return ""


def _evidence(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    claim: dict[str, Any],
    runs: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """The claim's visible, active evidence versions with file, digest, size and computed
    availability (details only; review cp20-2-a F6), the locator the claim cites it at (a whole
    file is ``{"type": "file"}``; a version cited only by relation has none), and the recorded
    runs that wrote it with their producer files (CP20.4, gap audit #16)."""
    locators = {
        str(ev.get("version_id")): ev.get("locator") for ev in claim["body"].get("evidence", [])
    }
    out = []
    for version_id in _evidence_versions(conn, claim):
        version = current_record(conn, version_id)
        if (
            version is None
            or not caller.may_see(version["sensitivity"])
            or version.get("lifecycle", "active") != "active"
            or version["body"].get("purged")
        ):
            continue
        body = version["body"]
        source = current_record(conn, str(body.get("source_id")))
        visible = source is not None and caller.may_see(source["sensitivity"])
        binding = (source["body"].get("root_binding") or {}) if visible and source else {}
        out.append(
            {
                "version_id": version_id,
                "title": source["body"].get("title") if visible and source else None,
                "root_alias": binding.get("alias"),
                "path": binding.get("path"),
                "sha256": body.get("sha256"),
                "byte_length": body.get("byte_length"),
                "availability": computed_availability(conn, store_dir, body, version_id),
                "locator": locators.get(version_id),
                "runs": (runs or {}).get(version_id, []),
            }
        )
    return out


def _file(conn: sqlite3.Connection, caller: Caller, version_id: str) -> dict[str, Any] | None:
    """A visible, active version's file: its source title, folder binding and digest."""
    version = current_record(conn, version_id)
    if (
        version is None
        or not caller.may_see(version["sensitivity"])
        or version.get("lifecycle", "active") != "active"
        or version["body"].get("purged")
    ):
        return None
    source = current_record(conn, str(version["body"].get("source_id")))
    if source is None or not caller.may_see(source["sensitivity"]):
        return None
    binding = source["body"].get("root_binding") or {}
    return {
        "version_id": version_id,
        "title": source["body"].get("title"),
        "root_alias": binding.get("alias"),
        "path": binding.get("path"),
        "sha256": version["body"].get("sha256"),
    }


def _runs(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller
) -> dict[str, list[dict[str, Any]]]:
    """Output version -> the visible execution receipts that recorded writing it: the receipt,
    what was reported run, and the producer files (the receipt's visible inputs). A receipt
    records what was reported; it is not an observation (spec/03)."""
    from prometheos.app.apply import read_bytes
    from prometheos.app.receipts import parse

    out: dict[str, list[dict[str, Any]]] = {}
    for version in _rows(conn, "version", caller):
        body = version["body"]
        if body.get("payload_schema") != "execution_receipt_v1":
            continue
        receipt_file = _file(conn, caller, version["id"])
        data = read_bytes(conn, store_dir, body) if receipt_file is not None else None
        if receipt_file is None or data is None:
            continue
        try:
            receipt = parse(data)
        except (PrometheosError, ValueError):
            continue
        producers = [
            found
            for found in (_file(conn, caller, i.version_id) for i in receipt.inputs)
            if found is not None
        ]
        run = {
            "receipt_version_id": version["id"],
            "receipt_title": receipt_file["title"],
            "reporter": receipt.reporter,
            "command": receipt.command[:300],
            "outcome": receipt.outcome,
            "ended_at": receipt.ended_at,
            "producers": producers,
        }
        for artifact in receipt.outputs:
            listed = out.setdefault(artifact.version_id, [])
            if all(r["receipt_version_id"] != version["id"] for r in listed):
                listed.append(run)
    return out


def _assessment(rows: list[dict[str, Any]], claim_id: str) -> dict[str, Any] | None:
    """The claim's latest visible assessment with its checks: what each expected, what it
    measured and the tolerance it judged by (details only)."""
    found = [a for a in rows if (a["body"].get("claim_ref") or {}).get("id") == claim_id]
    if not found:
        return None
    latest = found[-1]
    body = latest["body"]
    return {
        "assessment_id": latest["id"],
        "outcome": body.get("outcome"),
        "method": body.get("method"),
        "basis": claim_status.basis_of(latest),
        "checks": [
            {
                "name": c.get("name"),
                "result": c.get("result"),
                "expected": c.get("expected"),
                "measured": c.get("measured"),
                "tolerance": c.get("tolerance"),
            }
            for c in body.get("checks", [])
        ],
    }


# Audit actors of the agent paths (decision 0083 F2): the MCP server and the agent token's HTTP
# service. The CLI and the hub session read as the owner's, though an agent can run the CLI.
AGENT_ACTORS = frozenset({"mcp", "http-agent"})


def _placed_by(actor: str | None) -> str | None:
    if actor is None:
        return None
    return "agent" if actor in AGENT_ACTORS else "owner_or_cli"


class _Map:
    def __init__(self, conn: sqlite3.Connection, store_dir: Path, caller: Caller) -> None:
        self.conn = conn
        self.store_dir = store_dir
        self.caller = caller
        self.entries = {e["id"]: e for e in _rows(conn, "entry", caller)}
        self.papers = {p["id"]: p for p in _rows(conn, "paper", caller)}
        self.claims = {c["id"]: c for c in _rows(conn, "claim", caller)}
        visible = self.entries.keys() | self.papers.keys() | self.claims.keys()
        self.links: dict[str, list[tuple[str, str]]] = {r: [] for r in MAP_RELATIONS}
        # Who placed each link (decision 0083 F2; CP20.4): a read-only join on the audit row
        # its transaction wrote, ``relation_add:<relation>`` for the source record.
        self.placed: dict[tuple[str, str, str], str | None] = {}
        marks = ",".join("?" for _ in MAP_RELATIONS)
        for source, target, relation, actor in conn.execute(
            "SELECT r.source_id, r.target_id, r.relation, (SELECT a.actor FROM audit a "  # noqa: S608
            "WHERE a.seq = r.created_seq AND a.operation = 'relation_add:' || r.relation "
            "AND a.record_id = r.source_id ORDER BY a.op_index LIMIT 1) FROM relations r "
            f"WHERE r.relation IN ({marks}) AND r.retired_seq IS NULL ORDER BY r.id",
            MAP_RELATIONS,
        ):
            if source in visible and target in visible:
                pair = (str(source), str(target))
                if pair not in self.links[relation]:
                    self.links[relation].append(pair)
                    self.placed[(relation, *pair)] = _placed_by(actor)
        self.status = {s["claim_id"]: s for s in claim_status.statuses(conn, store_dir, caller)}
        self.assessments = _rows(conn, "assessment", caller)
        self.runs = _runs(conn, store_dir, caller)
        self.track_folders = self._track_folders()
        self.by_path: dict[str, list[str]] = {
            claim_id: self._tracks_by_path(claim_id) for claim_id in self.claims
        }

    def _track_folders(self) -> dict[str, set[tuple[str, str]]]:
        """Visible track entry -> the (root alias, folder) pairs its latest confirmed outline
        cites (decision 0108). Only the latest confirmation of each root counts."""
        from prometheos.adoption import outline

        found = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'adoption_outlines'"
        ).fetchone()
        if found is None:
            return {}
        out: dict[str, set[tuple[str, str]]] = {}
        aliases = [
            r[0] for r in self.conn.execute("SELECT DISTINCT root_alias FROM adoption_outlines")
        ]
        for alias in sorted(str(a) for a in aliases):
            confirmed = outline.latest(self.conn, alias)
            if confirmed is None:
                continue
            for item in confirmed["outline"].get("items", []):
                record = (item.get("record") or {}).get("id")
                if item.get("kind") != "track" or self.category(str(record)) != "track":
                    continue
                for cite in item.get("cites", []):
                    if cite.get("sha256") is not None or cite.get("line") is not None:
                        continue  # a file cite, not a folder (review map-ai-spread-a R1-F10)
                    folder = str(cite.get("path") or "").strip("/")
                    if folder:
                        out.setdefault(str(record), set()).add((alias, folder))
        return out

    def _tracks_by_path(self, claim_id: str) -> list[str]:
        """Tracks a claim draws on by file location and not by a stored link: one of its
        visible, active evidence files lies inside a folder the track's outline cites."""
        if not self.track_folders:
            return []
        files = [
            f
            for f in (
                _file(self.conn, self.caller, v)
                for v in _evidence_versions(self.conn, self.claims[claim_id])
            )
            if f is not None and f["root_alias"] and f["path"]
        ]
        linked = set(self.tracks_of(claim_id))
        return sorted(
            track
            for track, folders in self.track_folders.items()
            if track not in linked
            and any(
                f["root_alias"] == alias and str(f["path"]).startswith(folder + "/")
                for f in files
                for alias, folder in folders
            )
        )

    def category(self, entry_id: str) -> str | None:
        entry = self.entries.get(entry_id)
        return entry["body"].get("category") if entry else None

    def tracks_of(self, record_id: str) -> list[str]:
        return [
            t for s, t in self.links["draws_on"] if s == record_id and self.category(t) == "track"
        ]

    def claim(self, claim_id: str, linked_by: str | None = None) -> dict[str, Any]:
        record = self.claims[claim_id]
        status = self.status.get(claim_id, {})
        return {
            "claim_id": claim_id,
            "rev": record["rev"],
            "line": _line(record["body"]),
            "status": status.get("status", "unverified"),
            "basis": status.get("basis"),
            "usable": status.get("usable", True),
            "causes": status.get("causes", []),
            "reason": _reason(status.get("causes", [])),
            "tracks": self.tracks_of(claim_id),
            # Tracks it draws on by its files' location only (decision 0108; derived, not stored).
            "tracks_by_path": self.by_path.get(claim_id, []),
            "replaces": None,
            # Who placed the claim under its question: "agent", "owner_or_cli", or None when
            # it is not under one or no audit row says.
            "linked_by": linked_by,
            "details": {
                "statement": record["body"].get("statement"),
                "state": record["body"].get("state"),
                "evidence": _evidence(self.conn, self.store_dir, self.caller, record, self.runs),
                "assessment": _assessment(self.assessments, claim_id),
            },
        }

    def question(self, entry_id: str, placed_in: str | None = None) -> dict[str, Any]:
        entry = self.entries[entry_id]
        claims = [
            self.claim(c, self.placed.get(("answers", c, q)))
            for c, q in self.links["answers"]
            if q == entry_id and c in self.claims
        ]
        # A superseded claim's visible replacement answers its questions too (review cp20-2-a
        # F3): the correction carries the membership, so nobody has to link it again.
        listed = {c["claim_id"] for c in claims}
        for item in list(claims):
            current = item["claim_id"]
            while True:
                nxt = (self.claims[current]["body"].get("superseded_by") or {}).get("id")
                if not nxt or nxt not in self.claims or nxt in listed:
                    break
                replacement = self.claim(nxt) | {"replaces": current}
                claims.append(replacement)
                listed.add(nxt)
                current = nxt
        return {
            "entry_id": entry_id,
            "rev": entry["rev"],
            "title": entry["body"].get("title"),
            "claims": claims,
            "tracks": self.tracks_of(entry_id),
            # Who placed the question where it is listed (its ``addresses`` link).
            "linked_by": self.placed.get(("addresses", entry_id, placed_in)) if placed_in else None,
        }

    def questions_addressing(self, target_id: str) -> list[dict[str, Any]]:
        return [
            self.question(q, target_id)
            for q, t in self.links["addresses"]
            if t == target_id and self.category(q) == "question"
        ]

    def deliverable(self, paper: dict[str, Any]) -> dict[str, Any]:
        questions = self.questions_addressing(paper["id"])
        answering: dict[str, dict[str, Any]] = {}
        for question in questions:
            for claim in question["claims"]:
                answering.setdefault(claim["claim_id"], claim)
        in_use = [c for c in answering.values() if c["status"] not in OUT_OF_USE]
        blockers: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in STEPS}
        if not questions:
            blockers["framed"].append(
                _blocker("no_question", "No question addresses this paper yet.")
            )
        for question in questions:
            if not any(c["status"] not in OUT_OF_USE for c in question["claims"]):
                blockers["evidence_mapped"].append(
                    _blocker(
                        "unanswered",
                        f"No claim answers “{question['title']}” yet.",
                        question=question["entry_id"],
                    )
                )
        for claim in in_use:
            codes = {c["code"] for c in claim["causes"]}
            for code in ("no_evidence", "evidence_missing"):
                if code in codes:
                    message = next(c["message"] for c in claim["causes"] if c["code"] == code)
                    blockers["evidence_mapped"].append(
                        _blocker(code, message, claim=claim["claim_id"], line=claim["line"])
                    )
            if claim["status"] not in VERIFIED:
                blockers["verified"].append(
                    _blocker(
                        claim["status"],
                        claim["reason"],
                        claim=claim["claim_id"],
                        line=claim["line"],
                    )
                )
        steps = []
        reached_so_far = True
        stage = None
        for key, label in STEPS:
            reached = reached_so_far and not blockers[key]
            reached_so_far = reached
            if reached:
                stage = key
            steps.append(
                {"key": key, "label": label, "reached": reached, "blockers": blockers[key]}
            )
        to_confirm = [
            {"claim_id": c["claim_id"], "line": c["line"]}
            for c in in_use
            if c["status"] in VERIFIED and c["basis"] == "agent_reported"
        ]
        refs = [r["id"] for r in paper["body"].get("claim_refs", [])]
        other = [self.claim(c) for c in refs if c in self.claims and c not in answering]
        health = {s: 0 for s in claim_status.PRECEDENCE}
        for claim in [*answering.values(), *other]:
            health[claim["status"]] += 1
        next_step = next((s for s in steps if not s["reached"]), None)
        # The paper's own claims outside every question still count for the headline (review
        # cp20-2-a F5): "verified" never hides a stale or unverified claim the paper cites.
        outside = [c for c in other if c["status"] not in VERIFIED | OUT_OF_USE]
        return {
            "paper_id": paper["id"],
            "rev": paper["rev"],
            "title": paper["body"].get("title"),
            "paper_status": paper["body"].get("status"),
            "stage": stage,
            "steps": steps,
            "next": next_step["key"] if next_step else None,
            "headline": _headline(next_step, len(questions), len(in_use), to_confirm, len(outside)),
            "questions": questions,
            "other_claims": other,
            "to_confirm": to_confirm,
            "health": health,
        }


def _blocker(code: str, message: str, **refs: str) -> dict[str, Any]:
    return {"code": code, "message": message, **refs}


def _headline(
    next_step: dict[str, Any] | None,
    questions: int,
    claims: int,
    to_confirm: list[Any],
    outside: int = 0,
) -> str:
    """One deterministic sentence on what moves the paper forward."""
    if next_step is None:
        if outside:
            return (
                f"Verified under its questions; {outside} of the paper's claims "
                f"{'are' if outside != 1 else 'is'} outside them and not yet verified."
            )
        if to_confirm:
            n = len(to_confirm)
            rest = "claims rest" if n != 1 else "claim rests"
            return f"Verified; {n} {rest} on your agent's check alone."
        return "Verified: every claim under its questions holds."
    n = len(next_step["blockers"])
    if next_step["key"] == "framed":
        return "Not framed: no question addresses this paper yet."
    if next_step["key"] == "evidence_mapped":
        return f"{n} item{'s' if n != 1 else ''} to map before the evidence is complete."
    return (
        f"{n} of {claims} claim{'s' if claims != 1 else ''} under {questions} "
        f"question{'s' if questions != 1 else ''} still to verify."
    )


def view(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, *, paper_id: str | None = None
) -> dict[str, Any]:
    """The ``research_map`` read: the whole map, or one deliverable."""
    built = _Map(conn, store_dir, caller)
    if paper_id is not None and paper_id not in built.papers:
        raise PrometheosError("not_found", "paper not found", {"id": paper_id[:80]})
    project = next(iter(_rows(conn, "project", caller)), None)
    settings = project["body"] if project else {}
    objective_ref = (settings.get("objective_entry_ref") or {}).get("id")
    objective_entry = built.entries.get(objective_ref) if objective_ref else None
    papers = [built.papers[paper_id]] if paper_id else list(built.papers.values())
    placed = {q for q, t in built.links["addresses"] if t in built.papers or t == objective_ref}
    tracks = []
    for entry_id, entry in built.entries.items():
        if entry["body"].get("category") != "track":
            continue
        drawing = [s for s, t in built.links["draws_on"] if t == entry_id]
        by_path = [c for c, found in built.by_path.items() if entry_id in found]
        tracks.append(
            {
                "entry_id": entry_id,
                "title": entry["body"].get("title"),
                # Claims in use only: a withdrawn or superseded claim no longer draws on it.
                "claims": sum(
                    1
                    for s in drawing
                    if s in built.claims and built.status.get(s, {}).get("status") not in OUT_OF_USE
                ),
                "questions": sum(1 for s in drawing if built.category(s) == "question"),
                # In-use claims whose files lie in the track's folders, with no stored link (0108).
                "claims_by_path": sum(
                    1 for c in by_path if built.status.get(c, {}).get("status") not in OUT_OF_USE
                ),
                "folders": sorted(f for _, f in built.track_folders.get(entry_id, set())),
            }
        )
    seq = conn.execute("SELECT project_seq FROM project_state").fetchone()
    return {
        "generation": "deterministic",
        "project_seq": int(seq[0]) if seq else 0,
        "objective": {
            "text": settings.get("current_objective"),
            "entry_id": objective_entry["id"] if objective_entry else None,
            "questions": built.questions_addressing(str(objective_entry["id"]))
            if objective_entry and paper_id is None
            else [],
        },
        "deliverables": [built.deliverable(p) for p in papers],
        # Records created in one request share a sequence and have random IDs, so these lists
        # are ordered by title (then ID), the same in every store (C20-WALK).
        "unplaced_questions": [
            built.question(e)
            for e, entry in sorted(
                built.entries.items(), key=lambda kv: (str(kv[1]["body"].get("title")), kv[0])
            )
            if entry["body"].get("category") == "question" and e not in placed
        ]
        if paper_id is None
        else [],
        "tracks": sorted(tracks, key=lambda t: (str(t["title"]), t["entry_id"]))
        if paper_id is None
        else [],
        # Claims in use that answer no question, each with the layer's agreeing proposal when
        # there is one for its current bundle (decision 0109; the owner places it).
        "unplaced_claims": _unplaced_claims(built) if paper_id is None else [],
    }


def _unplaced_claims(built: _Map) -> list[dict[str, Any]]:
    from prometheos.app import map_layer

    statuses = {c: str(s.get("status")) for c, s in built.status.items()}
    claims, _questions = map_layer.candidates(built.conn, built.caller, statuses)
    proposals = map_layer.suggestions(built.conn, built.caller, statuses)
    return [built.claim(c["id"]) | {"suggestion": proposals.get(c["id"])} for c in claims]


__all__ = ["MAP_RELATIONS", "STEPS", "view"]
