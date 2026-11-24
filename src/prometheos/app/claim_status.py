"""Derived claim status (A06.1, CP20.1; decision 0082): what still holds, and why.

Each claim gets one status with all its causes, by the precedence of spec/03 (withdrawn,
superseded, stale, conflicting, settling, unverified, corrected, holds). ``holds`` and
``corrected`` name their basis, decided by who recorded the support: ``reviewed`` when the
owner promoted the claim or recorded a researcher assessment (an agent cannot: writing one is a
protected effect on create and revise), ``deterministic`` for a check PrometheOS itself produced
(the only writer of ``deterministic_check``), and ``agent_reported`` for every other assessment.

Only records the caller may see feed the status (review cp20-1-a F1), so a hidden assessment,
correction, entry or version changes nothing an agent reads.

The status is a pure, rebuildable read. It never changes a record, never promotes a claim
(R21) and is not scientific support.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from prometheos.app.apply import read_bytes
from prometheos.app.reads import Caller, computed_availability, current_record
from prometheos.domain.bodies import normalise_stored_body
from prometheos.domain.ids import kind_of
from prometheos.domain.models import PrometheosError

PRECEDENCE = (
    "withdrawn",
    "superseded",
    "stale",
    "conflicting",
    "settling",
    "unverified",
    "corrected",
    "holds",
)
STATUSES = frozenset(PRECEDENCE)
BASIS_ORDER = ("reviewed", "deterministic", "agent_reported")
NOT_USABLE = frozenset({"withdrawn", "superseded", "stale"})


def _rows(conn: sqlite3.Connection, kind: str, caller: Caller) -> list[dict[str, Any]]:
    """Active, unpurged records of one kind that the caller may see."""
    out = []
    for (record_id,) in conn.execute(
        "SELECT id FROM records WHERE kind = ? AND lifecycle = 'active' ORDER BY created_seq, id",
        (kind,),
    ):
        record = current_record(conn, record_id)
        if record is None or record["body"].get("purged"):
            continue
        if caller.may_see(record["sensitivity"]):
            out.append(record)
    return out


def _commit_seq(conn: sqlite3.Connection, record_id: str, rev: int) -> int:
    row = conn.execute(
        "SELECT commit_seq FROM revisions WHERE record_id = ? AND rev = ?", (record_id, rev)
    ).fetchone()
    return int(row[0]) if row else 0


def basis_of(assessment: dict[str, Any]) -> str:
    """Who recorded this support. ``researcher`` and ``deterministic_check`` cannot come from
    an agent (app/research.py refuses the second and makes the first a protected effect on create
    and on revise), so the stored value of the current revision records who wrote it."""
    method = assessment["body"].get("method")
    if method == "researcher":
        return "reviewed"
    if method == "deterministic_check":
        return "deterministic"
    return "agent_reported"


class _Index:
    """What one status read needs, decoded once (review cp20-1-a F2)."""

    def __init__(self, conn: sqlite3.Connection, store_dir: Path, caller: Caller) -> None:
        self.conn = conn
        self.versions = {v["id"]: v for v in _rows(conn, "version", caller)}
        self.seq = {
            str(r[0]): int(r[1])
            for r in conn.execute("SELECT id, created_seq FROM records WHERE kind = 'version'")
        }
        self.by_source: dict[str, list[dict[str, Any]]] = {}
        for version in self.versions.values():
            self.by_source.setdefault(str(version["body"].get("source_id")), []).append(version)
        self.sources = {s["id"]: s for s in _rows(conn, "source", caller)}
        self.assessments = _rows(conn, "assessment", caller)
        self.corrections = _rows(conn, "correction", caller)
        self.entries = _rows(conn, "entry", caller)
        self.store_dir = store_dir
        self.producers = self._receipt_producers(store_dir)
        # CP20.3: claims a waiting decision card names (visible to this caller's rank).
        from prometheos.app import cards

        self.carded = cards.carded_claims(conn, caller.rank)
        self._contents: dict[tuple[str, int], tuple[str, str, str, str] | None] = {}
        self.adopted = self._adoption_producers()

    def _adoption_producers(self) -> dict[str, list[dict[str, Any]]]:
        """Evidence version -> the adoption producer edges of its own file (CP20.4 6c; decisions
        0106 and 0108). A version is matched only to the file its source is bound to (alias and
        path), in the latest complete scan of that folder, and only while that file still has
        the version's bytes. An edge counts as changed only on byte evidence or the owner's word:
        the script's bytes differ from those recorded when the owner confirmed the outline, or
        the owner kept the file as a stale candidate in that outline. A modification time alone
        never makes a claim stale (review cp20-a-r1 R1-F1: a checkout writes files in path
        order)."""
        from prometheos.adoption import outline as adoption_outline

        latest = {
            str(alias): int(gen)
            for alias, gen in self.conn.execute(
                "SELECT root_alias, MAX(scan_gen) FROM adoption_scans WHERE state = 'complete' "
                "GROUP BY root_alias"
            )
        }
        out: dict[str, list[dict[str, Any]]] = {}
        if not latest:
            return out
        confirmed: dict[str, dict[str, Any] | None] = {}
        for version_id, version in self.versions.items():
            source = self.sources.get(str(version["body"].get("source_id")))
            binding = (source or {}).get("body", {}).get("root_binding") or {}
            alias, path = binding.get("alias"), binding.get("path")
            if not alias or not path or alias not in latest:
                continue
            gen = latest[alias]
            output = self.conn.execute(
                "SELECT sha256 FROM adoption_files WHERE scan_gen = ? AND path = ?", (gen, path)
            ).fetchone()
            if output is None or output["sha256"] != version["body"].get("sha256"):
                continue
            if alias not in confirmed:
                confirmed[alias] = adoption_outline.latest(self.conn, alias)
            items = (confirmed[alias] or {}).get("outline", {}).get("items", [])
            sha = version["body"].get("sha256")
            # Review cp20-a-r2 R2-F1: the outline speaks only about the output bytes it saw. A
            # claim on a rerun's new output is judged on that output, not on the old record.
            recorded = next(
                (i.get("producer_sha256") for i in items
                 if i.get("kind") == "result" and i.get("path") == path
                 and i.get("sha256") == sha),
                None,
            )  # fmt: skip
            kept_stale = any(
                i.get("kind") == "stale_candidate"
                and i.get("path") == path
                and i.get("sha256") == sha
                for i in items
            )
            for ev in self.conn.execute(
                "SELECT object FROM adoption_evidence WHERE scan_gen = ? AND subject = ? "
                "AND predicate = 'generated_by' ORDER BY evidence_id",
                (gen, path),
            ):
                producer_path = json.loads(ev["object"]).get("path")
                producer = self.conn.execute(
                    "SELECT sha256 FROM adoption_files WHERE scan_gen = ? AND path = ?",
                    (gen, producer_path),
                ).fetchone()
                if not producer_path or producer is None:
                    continue
                since = recorded is not None and producer["sha256"] not in (None, recorded)
                kept = kept_stale and (recorded is None or producer["sha256"] == recorded)
                out.setdefault(version_id, []).append(
                    {
                        "root_alias": alias,
                        "path": path,
                        "producer": str(producer_path),
                        "changed_after": kept,
                        "changed_since_outline": since,
                    }
                )
        return out

    def claim_content(self, claim_id: str, rev: int) -> tuple[str, str, str, str] | None:
        """The relied-on content of one revision of a claim, decoded once."""
        key = (claim_id, rev)
        if key not in self._contents:
            from prometheos.domain.canonical import loads_internal

            row = self.conn.execute(
                "SELECT b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
                "WHERE v.record_id = ? AND v.rev = ?",
                key,
            ).fetchone()
            # Read the old revision the way current bodies are read (review r3 F1: a claim
            # written before CP10 lacks scope.objective until normalised).
            self._contents[key] = (
                _content(normalise_stored_body("claim", loads_internal(row[0])))
                if row and row[0] is not None
                else None
            )
        return self._contents[key]

    def retained(self, version_id: str) -> bool:
        """A visible version whose bytes are available, by the rule promotion itself uses
        (``computed_availability``: digest and source checks included; review r3 F2)."""
        version = self.versions.get(version_id)
        if version is None:
            return False
        if version["body"].get("storage") == "external":
            # CP20.4 6c (decision 0106; C20-ADOPT): a reference-only version, such as a cited
            # baseline file, counts once a saved source check found its exact bytes at the
            # origin; until then it is unverified.
            from prometheos.app.sources import reference_state

            return bool(reference_state(self.conn, version_id, version["body"])["confirmed"])
        state = computed_availability(self.conn, self.store_dir, version["body"], version_id)
        return bool(state == "retained")

    def _receipt_producers(self, store_dir: Path) -> dict[str, list[dict[str, Any]]]:
        """Visible output version -> the visible input artifacts of each visible receipt."""
        from prometheos.app.receipts import parse

        out: dict[str, list[dict[str, Any]]] = {}
        for version in self.versions.values():
            body = version["body"]
            if body.get("payload_schema") != "execution_receipt_v1":
                continue
            data = read_bytes(self.conn, store_dir, body)
            if data is None:
                continue
            try:
                receipt = parse(data)
            except (PrometheosError, ValueError):
                continue
            minutes = _minutes(receipt.started_at, receipt.ended_at)
            for artifact in receipt.outputs:
                out.setdefault(artifact.version_id, []).extend(
                    {
                        "version_id": i.version_id,
                        "source_id": i.source_id,
                        "sha256": i.sha256,
                        # CP20.3: the recorded run time estimates a rerun.
                        "minutes": minutes,
                    }
                    for i in receipt.inputs
                    if i.version_id in self.versions
                )
        return out

    def newer_version(self, source_id: str, version_id: str, sha256: str) -> str | None:
        """The source's latest visible version, when it was recorded after ``version_id`` and its
        bytes differ: identical bytes re-imported, or a return to the same bytes (A, B, A), are
        not newer."""
        base = self.seq.get(version_id)
        versions = self.by_source.get(source_id, [])
        if base is None or not versions:
            return None
        latest = max(versions, key=lambda v: self.seq.get(v["id"], 0))
        if self.seq.get(latest["id"], 0) > base and latest["body"].get("sha256") != sha256:
            return str(latest["id"])
        return None


def _minutes(started: str | None, ended: str | None) -> int | None:
    """A run's recorded duration in whole minutes (at least one), if both ends are known."""
    from datetime import datetime

    if not started or not ended:
        return None
    seconds = (
        datetime.fromisoformat(ended.replace("Z", "+00:00"))
        - datetime.fromisoformat(started.replace("Z", "+00:00"))
    ).total_seconds()
    return max(1, -(-int(seconds) // 60)) if seconds >= 0 else None


def _origin_changed(conn: sqlite3.Connection, version_id: str, sha256: str | None) -> bool:
    row = conn.execute(
        "SELECT origin_current_digest FROM source_checks WHERE version_id = ? "
        "ORDER BY commit_seq DESC, id DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    return bool(row and row[0] and sha256 and row[0] != sha256)


def _evidence_versions(conn: sqlite3.Connection, claim: dict[str, Any]) -> list[str]:
    found = [ev["version_id"] for ev in claim["body"].get("evidence", [])]
    for (target,) in conn.execute(
        "SELECT target_id FROM relations WHERE source_id = ? AND relation = 'cites' "
        "AND retired_seq IS NULL ORDER BY id",
        (claim["id"],),
    ):
        if kind_of(str(target)) == "version":
            found.append(str(target))
    return list(dict.fromkeys(found))


def _changed_dependencies(
    conn: sqlite3.Connection, claim: dict[str, Any], caller: Caller
) -> list[dict[str, Any]]:
    """Visible records the claim ``depends_on`` that were revised past the bound revision or
    superseded since (review F5: derived from the relations, not from clearable view flags)."""
    out = []
    # The latest binding per target counts: re-linking to the revised assumption clears it
    # (review cp20-1-a-r2 F2; relations are appended, not retired).
    for target, bound in conn.execute(
        "SELECT target_id, MAX(target_rev) FROM relations WHERE source_id = ? "
        "AND relation = 'depends_on' AND retired_seq IS NULL GROUP BY target_id ORDER BY target_id",
        (claim["id"],),
    ):
        record = current_record(conn, str(target))
        if record is None or not caller.may_see(record["sensitivity"]):
            continue
        if record["rev"] > int(bound or 0) or record["body"].get("superseded_by") is not None:
            out.append(record)
    return out


def _body_at(conn: sqlite3.Connection, record_id: str, rev: int) -> dict[str, Any] | None:
    from prometheos.domain.canonical import loads_internal

    row = conn.execute(
        "SELECT b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
        "WHERE v.record_id = ? AND v.rev = ?",
        (record_id, rev),
    ).fetchone()
    return loads_internal(row[0]) if row and row[0] is not None else None


# Fields that record who checked a record, not what it says: a revision changing only these leaves
# what a run computed under it valid (review cp20-a-r1 R1-F2).
REVIEW_ONLY = frozenset({"review", "origin", "author_ref"})


def _dependency_changes(
    conn: sqlite3.Connection, claim: dict[str, Any], caller: Caller, index: _Index, run_seq: int
) -> list[tuple[str, int]]:
    """(target, sequence) of the latest change to each visible record the claim depends on, after
    the run recorded at ``run_seq``: a revision whose content differs from the revision in force
    at the run (review-only changes do not count), or an applied correction targeting it."""
    out = []
    for (target,) in conn.execute(
        "SELECT DISTINCT target_id FROM relations WHERE source_id = ? AND relation = 'depends_on' "
        "AND retired_seq IS NULL ORDER BY target_id",
        (claim["id"],),
    ):
        record = current_record(conn, str(target))
        if record is None or not caller.may_see(record["sensitivity"]):
            continue
        changes = []
        # The revision in force at the run; a record made after the run states what the run
        # assumed, so its first revision stands in for it.
        then = max(
            (
                r
                for r in range(1, record["rev"] + 1)
                if _commit_seq(conn, str(target), r) <= run_seq
            ),
            default=1,
        )
        if then < record["rev"]:
            old = _body_at(conn, str(target), then) or {}
            now = record["body"]
            if {k: v for k, v in old.items() if k not in REVIEW_ONLY} != {
                k: v for k, v in now.items() if k not in REVIEW_ONLY
            }:
                changes.append(_commit_seq(conn, str(target), record["rev"]))
        changes += [
            _commit_seq(conn, c["id"], c["rev"])
            for c in index.corrections
            if c["body"].get("status") == "applied"
            and str(target) in {e["target_ref"]["id"] for e in c["body"].get("effects", [])}
        ]
        if changes:
            out.append((str(target), max(changes)))
    return out


def _content(body: dict[str, Any]) -> tuple[str, str, str, str]:
    """What an assessment of a claim relies on."""
    from prometheos.domain.canonical import dumps_canonical

    return (
        str(body.get("statement")),
        dumps_canonical(body.get("scope")).decode(),
        dumps_canonical(body.get("evidence", [])).decode(),
        dumps_canonical(body.get("quantities", [])).decode(),
    )


def _bindings(claim: dict[str, Any], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(claim["body"].get("quantities", []))
    for entry in entries:
        if any(ref["id"] == claim["id"] for ref in entry["body"].get("claim_refs", [])):
            out.extend(entry["body"].get("quantities", []))
    return out


def _quantity_key(q: dict[str, Any]) -> tuple[str, str | None]:
    """Two bindings are the same quantity only with the same name and definition: two
    definitions under one name never conflict (A06.1)."""
    return (q["name"], q.get("definition"))


def _cause(code: str, message: str, ref: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "ref": ref}


def statuses(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    *,
    claim_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """The derived status of every claim the caller may see (or of ``claim_ids``)."""
    claims = _rows(conn, "claim", caller)
    if claim_ids is not None:
        claims = [c for c in claims if c["id"] in claim_ids]
    index = _Index(conn, store_dir, caller)
    return [_one(conn, caller, claim, index) for claim in claims]


def _one(
    conn: sqlite3.Connection, caller: Caller, claim: dict[str, Any], index: _Index
) -> dict[str, Any]:
    body = claim["body"]
    cid = claim["id"]
    found: dict[str, list[dict[str, Any]]] = {s: [] for s in PRECEDENCE}
    evidence = _evidence_versions(conn, claim)

    # 1. The claim's own state.
    if body["state"] in ("withdrawn", "superseded"):
        found[body["state"]].append(_cause(body["state"], f"The claim is {body['state']}."))

    # 2. Stale: a newer producer, a changed dependency, a changed origin.
    for version_id in evidence:
        version = index.versions.get(version_id)
        if version is None:
            continue
        for producer in index.producers.get(version_id, []):
            newer = index.newer_version(
                producer["source_id"], producer["version_id"], producer["sha256"]
            )
            if newer is not None:
                source = index.sources.get(producer["source_id"])
                title = source["body"].get("title") if source else "A producing file"
                found["stale"].append(
                    _cause(
                        "producer_newer",
                        f"{title} has a newer version than the one that produced the evidence.",
                        newer,
                    )
                )
        for edge in index.adopted.get(version_id, []):
            if edge["changed_after"] or edge["changed_since_outline"]:
                when = (
                    "since you confirmed the outline"
                    if edge["changed_since_outline"]
                    else "after it wrote the file (a stale candidate you kept in the outline)"
                )
                at = f"{edge['root_alias']}:{edge['producer']}"
                found["stale"].append(
                    _cause(
                        "producer_changed",
                        f"{at}, which writes {edge['path']}, changed {when}.",
                        at,
                    )
                )
        if _origin_changed(conn, version_id, version["body"].get("sha256")):
            found["stale"].append(
                _cause(
                    "origin_changed",
                    "A source check found the evidence's origin changed.",
                    version_id,
                )
            )
    for dependency in _changed_dependencies(conn, claim, caller):
        found["stale"].append(
            _cause(
                "dependency_changed",
                "Something the claim depends on was revised or superseded since.",
                dependency["id"],
            )
        )
    # A run older than a correction applied to, or a revision of, what the claim depends on:
    # its outputs were computed before the change (spec/03; C20-STATUS).
    runs = [index.seq[v] for v in evidence if v in index.producers and v in index.seq]
    if runs:
        for target, changed_at in _dependency_changes(conn, claim, caller, index, min(runs)):
            if changed_at > min(runs):
                found["stale"].append(
                    _cause(
                        "run_predates_change",
                        "The run behind its evidence predates a correction or revision of "
                        "something it depends on.",
                        target,
                    )
                )

    # 3. Conflicting: the same quantity (name and definition) bound with different values.
    seen: dict[tuple[str, str | None], tuple[str, str]] = {}
    for q in _bindings(claim, index.entries):
        if not q.get("name"):
            continue
        key = _quantity_key(q)
        value = (q["decimal"], q["unit"])
        if key in seen and seen[key] != value:
            found["conflicting"].append(
                _cause(
                    "same_quantity_two_values",
                    f"{q['name']} is bound as {' '.join(seen[key])} and as {' '.join(value)}.",
                )
            )
        seen.setdefault(key, value)

    # 4. Settling: a proposed correction, a contested state, a challenge the owner has not
    # settled (review F7: only the owner's support or a decided correction settles it).
    mine = [a for a in index.assessments if a["body"]["claim_ref"]["id"] == cid]
    touching = [
        c
        for c in index.corrections
        if cid in {e["target_ref"]["id"] for e in c["body"].get("effects", [])}
    ]
    for correction in touching:
        if correction["body"].get("status") == "proposed":
            found["settling"].append(
                _cause(
                    "correction_proposed",
                    "A correction is proposed and waits for your decision.",
                    correction["id"],
                )
            )
    if body["state"] == "contested":
        found["settling"].append(_cause("contested", "The claim is contested."))
    if cid in index.carded:
        found["settling"].append(
            _cause("card_waiting", "A decision card about it waits for you or its default date.")
        )
    settled_at = max(
        [
            _commit_seq(conn, a["id"], a["rev"])
            for a in mine
            if a["body"]["outcome"] == "supports" and basis_of(a) == "reviewed"
        ]
        + [
            _commit_seq(conn, c["id"], c["rev"])
            for c in touching
            if c["body"].get("status") in ("applied", "rejected")
        ]
        + (  # the owner's protected promotion settles an earlier challenge (r2 F3)
            [_commit_seq(conn, cid, claim["rev"])]
            if body["state"] == "supported_within_conditions"
            else []
        ),
        default=0,
    )
    for a in mine:
        if (
            a["body"]["outcome"] == "challenges"
            and _commit_seq(conn, a["id"], a["rev"]) > settled_at
        ):
            found["settling"].append(
                _cause(
                    "challenged",
                    "An assessment challenges the claim; a correction or your decision is needed.",
                    a["id"],
                )
            )

    # 5. Unverified: nothing current assesses it, or nothing it rests on is present.
    # An assessment checks the content it saw; a revision that changes nothing it relied on
    # (statement, scope, evidence, quantities), such as a promotion or linked assessment_refs,
    # keeps it current (review cp20-1-a-r2 F1).
    current = [
        a for a in mine if index.claim_content(cid, a["body"]["claim_ref"]["rev"]) == _content(body)
    ]
    supporting = [
        a for a in current if a["body"]["outcome"] == "supports" and a["body"]["scope_match"]
    ]
    if not mine:
        found["unverified"].append(_cause("no_assessment", "No assessment checks this claim yet."))
    elif not current:
        found["unverified"].append(
            _cause(
                "revised_since", "The claim was revised after its assessments; none checks it now."
            )
        )
    elif not supporting and not found["settling"]:
        code, text = (
            ("out_of_scope", "Its supporting assessments do not match its scope.")
            if any(a["body"]["outcome"] == "supports" for a in current)
            else ("inconclusive", "Its assessments are inconclusive; none supports it yet.")
        )
        found["unverified"].append(_cause(code, text))
    if not evidence:
        found["unverified"].append(_cause("no_evidence", "The claim cites no evidence."))
    elif not any(index.retained(v) for v in evidence):
        # Support needs accessible evidence (spec/03): a retained version whose bytes are present
        # (review cp20-1-a-r2 F4); reference-only artefacts alone do not suffice.
        found["unverified"].append(
            _cause("evidence_missing", "None of the evidence it cites is retained and intact.")
        )

    # 6-7. Corrected (an applied correction's replacement) and holds, with their basis.
    replaced = [
        c
        for c in index.corrections
        if c["body"].get("status") == "applied"
        and any(
            (e.get("replacement_ref") or {}).get("id") == cid for e in c["body"].get("effects", [])
        )
    ]
    bases = [basis_of(a) for a in supporting]
    if body["state"] == "supported_within_conditions":
        bases.append("reviewed")  # the owner's protected promotion (R21)
    basis = min(bases, key=BASIS_ORDER.index) if bases else None
    if basis is not None:
        if replaced:
            found["corrected"].append(
                _cause("replaced", "It replaces a corrected claim.", replaced[0]["id"])
            )
        found["holds"].append(_cause("supported", "A supporting assessment within its scope."))

    status = next((s for s in PRECEDENCE if found[s]), "unverified")
    causes: list[dict[str, Any]] = []
    for s in PRECEDENCE:
        for c in found[s]:
            if c not in causes:  # one cause per code, message and ref
                causes.append(c)
    return {
        "claim_id": cid,
        "rev": claim["rev"],
        "status": status,
        "basis": basis if status in ("holds", "corrected") else None,
        "usable": status not in NOT_USABLE,
        "summary": body.get("summary"),
        "causes": causes,
    }


def view(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    *,
    claim_ids: list[str] | None = None,
    paper_id: str | None = None,
) -> dict[str, Any]:
    """The ``claim_status`` read: statuses of the given claims, of one paper's claims, or of all."""
    wanted: set[str] | None = set(claim_ids) if claim_ids else None
    if paper_id is not None:
        paper = current_record(conn, paper_id)
        if paper is None or paper["kind"] != "paper" or not caller.may_see(paper["sensitivity"]):
            raise PrometheosError("not_found", "paper not found", {"id": paper_id[:80]})
        refs = {r["id"] for r in paper["body"].get("claim_refs", [])}
        wanted = refs if wanted is None else wanted & refs
    found = statuses(conn, store_dir, caller, claim_ids=wanted)
    counts = {s: 0 for s in PRECEDENCE}
    for item in found:
        counts[item["status"]] += 1
    seq = conn.execute("SELECT project_seq FROM project_state").fetchone()
    return {
        "generation": "deterministic",
        "project_seq": int(seq[0]) if seq else 0,
        "counts": counts,
        "claims": found,
    }


__all__ = ["NOT_USABLE", "PRECEDENCE", "STATUSES", "basis_of", "statuses", "view"]
