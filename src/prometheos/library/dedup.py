"""Similarity candidates and the owner's dedup choices (CP11 S11.2; decision 0062 §2).

Exact duplicates (a shared DOI, arXiv identifier, PMID or PMCID) are merged at import. Works
that only look alike are *proposed* here and never changed without an explicit choice:

- a **duplicate** candidate: the same normalised title (case, punctuation, accents, LaTeX
  commands and stop words ignored), the same first-author family name and years at most one
  apart, without conflicting identifiers;
- a **version** candidate: the same, where one work has only an arXiv identifier and the other
  only a DOI (a preprint and its published version).

``resolve`` records the owner's choice: ``merge`` sets ``merged_into`` on the newer work (the
older one stands for both; nothing else moves), ``distinct`` is remembered so the pair is not
proposed again, ``versions`` adds a ``version_of`` relation (preprint -> published). Resolving
is protected: an agent's request becomes an approval request.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
import uuid
from typing import Any

from prometheos.app.reads import Caller, current_record, project_seq
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError, utc_now
from prometheos.library import bibimport
from prometheos.library import index as library_index
from prometheos.store.project import Txn, live

STOP_WORDS = frozenset(
    "a an the of on in for and to with by at from as or via its into is are".split()
)
CHOICES = ("merge", "distinct", "versions")
MAX_CANDIDATES = 500


def title_key(title: str) -> str:
    """Case-folded, accents removed, LaTeX commands and punctuation removed, stop words
    dropped: two titles with the same key are a similarity candidate."""
    text = title.replace("{", "").replace("}", "")
    text = re.sub(r"\\[^A-Za-z\s]", "", text)  # accent commands: \'e reads as e
    text = re.sub(r"\\[A-Za-z]+", " ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    words = re.findall(r"[0-9a-z]+", text)
    return " ".join(w for w in words if w not in STOP_WORDS)


def family(contributors: list[dict[str, Any]]) -> str | None:
    """The first author's family name, normalised (``Family, Given`` or ``Given Family``)."""
    authors = [c for c in contributors if c.get("role", "author") == "author"] or contributors
    if not authors:
        return None
    name = str(authors[0]["name"])
    surname = name.split(",")[0] if "," in name else (name.split() or [""])[-1]
    key = title_key(surname)
    return key or None


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def candidate_id(a: str, b: str) -> str:
    x, y = pair_key(a, b)
    return "cand_" + hashlib.sha256(f"{x}|{y}".encode()).hexdigest()[:16]


def _decided(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    if not library_index.schema14(conn):
        return set()
    return {
        (str(r[0]), str(r[1])) for r in conn.execute("SELECT work_a, work_b FROM library_decisions")
    }


def _linked(conn: sqlite3.Connection, a: str, b: str) -> bool:
    return bibimport.has_link(conn, a, b)


def candidates(conn: sqlite3.Connection, caller: Caller) -> dict[str, Any]:
    """Every proposed pair among the works the caller may see, deterministic order."""
    rows = conn.execute(
        "SELECT w.work_id FROM library_works w JOIN records r ON r.id = w.work_id "
        "WHERE r.lifecycle = 'active' AND w.merged_into IS NULL ORDER BY w.work_id"
    ).fetchall()
    works: list[dict[str, Any]] = []
    for row in rows:
        record = current_record(conn, str(row[0]))
        if record is None or not caller.may_see(record["sensitivity"]):
            continue
        works.append(record)
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in works:
        key = title_key(record["body"]["title"])
        if key:
            groups.setdefault(key, []).append(record)
    decided = _decided(conn)
    out = []
    for key, group in sorted(groups.items()):
        for i, first in enumerate(group):
            for second in group[i + 1 :]:
                found = _candidate(conn, key, first, second, decided)
                if found is not None:
                    out.append(found)
                if len(out) >= MAX_CANDIDATES:
                    break
    out.sort(key=lambda c: (c["kind"], c["title_key"], c["candidate_id"]))
    return {
        "project_seq": project_seq(conn),
        "candidates": out,
        "total": len(out),
        "content_trust": "untrusted_data",
        "notice": (
            "Candidates are proposals from titles, first authors and years, never decisions: "
            "merge, keep separate or relate them as versions explicitly (an owner decision)."
        ),
    }


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    body = record["body"]
    ids = body.get("identifiers") or {}
    return {
        "work_id": record["id"],
        "rev": record["rev"],
        "title": body["title"],
        "first_author": (body.get("contributors") or [{}])[0].get("name"),
        "year": body.get("year"),
        "venue": body.get("venue"),
        "identifiers": {k: v for k, v in ids.items() if k != "other" and v},
    }


def _candidate(
    conn: sqlite3.Connection,
    key: str,
    a: dict[str, Any],
    b: dict[str, Any],
    decided: set[tuple[str, str]],
) -> dict[str, Any] | None:
    if pair_key(a["id"], b["id"]) in decided or _linked(conn, a["id"], b["id"]):
        return None
    fa, fb = family(a["body"]["contributors"]), family(b["body"]["contributors"])
    if fa is None or fa != fb:
        return None
    ya, yb = a["body"].get("year"), b["body"].get("year")
    if ya is not None and yb is not None and abs(int(ya) - int(yb)) > 1:
        return None
    ia, ib = a["body"]["identifiers"], b["body"]["identifiers"]
    for scheme in ("doi", "arxiv", "pmid", "pmcid", "isbn"):
        if ia.get(scheme) and ib.get(scheme) and ia[scheme] != ib[scheme]:
            return None  # different identifiers: different works
    arxiv_only = [w for w in (ia, ib) if w.get("arxiv") and not w.get("doi")]
    doi_only = [w for w in (ia, ib) if w.get("doi") and not w.get("arxiv")]
    kind = "version" if len(arxiv_only) == 1 and len(doi_only) == 1 else "duplicate"
    # The older work first (it would stand for both after a merge).
    x, y = sorted((a, b), key=lambda r: (_created_seq(conn, r["id"]), r["id"]))
    return {
        "candidate_id": candidate_id(a["id"], b["id"]),
        "kind": kind,
        "title_key": key,
        "evidence": {
            "same_title_key": key,
            "first_author_family": fa,
            "years": [x["body"].get("year"), y["body"].get("year")],
        },
        "works": [_summary(x), _summary(y)],
        "suggested": "versions" if kind == "version" else "merge or distinct",
    }


def links(conn: sqlite3.Connection, caller: Caller) -> list[dict[str, Any]]:
    """The live ``version_of`` links between works the caller may see, each with its origin:
    ``decision`` (the owner's ``versions`` choice, undone from the decisions) or ``import``
    (an import's link, which the owner may undo here; review round 2, R2-F2)."""
    if not library_index.schema14(conn):
        return []
    out = []
    rows = conn.execute(
        "SELECT source_id, target_id FROM relations WHERE relation = 'version_of' AND "
        "retired_seq IS NULL ORDER BY id"
    ).fetchall()
    for source, target in rows:
        records = [current_record(conn, str(source)), current_record(conn, str(target))]
        if not all(
            r is not None
            and r["kind"] == "work"
            and r["lifecycle"] != "deleted"
            and caller.may_see(r["sensitivity"])
            for r in records
        ):
            continue
        preprint, published = records
        assert preprint is not None and published is not None
        origin = "decision" if _choice(conn, *pair_key(str(source), str(target))) else "import"
        out.append(
            {
                "preprint": preprint["id"],
                "preprint_title": str(preprint["body"]["title"])[:300],
                "published": published["id"],
                "published_title": str(published["body"]["title"])[:300],
                "origin": origin,
            }
        )
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def decisions(conn: sqlite3.Connection, caller: Caller) -> list[dict[str, Any]]:
    if not library_index.schema14(conn):
        return []
    out = []
    for row in conn.execute("SELECT * FROM library_decisions ORDER BY decided_at, decision_id"):
        visible = all(
            (r := current_record(conn, str(row[k]))) is not None
            and r["lifecycle"] != "deleted"
            and caller.may_see(r["sensitivity"])
            for k in ("work_a", "work_b")
        )
        if visible:
            out.append({k: row[k] for k in row.keys()})
    return out


# ----- the owner's choice -------------------------------------------------------------------------


def plan_of(
    conn: sqlite3.Connection, caller: Caller, a: str, b: str, choice: str
) -> dict[str, Any]:
    """What resolving the pair would do, bound to both works' current revisions (the exact
    plan an approval request stores)."""
    if choice not in CHOICES:
        raise PrometheosError("validation_error", "choice is merge, distinct or versions", {})
    if a == b:
        raise PrometheosError("validation_error", "a pair needs two different works", {})
    records = []
    for work_id in (a, b):
        record = current_record(conn, work_id)
        if (
            record is None
            or record["kind"] != "work"
            or record["lifecycle"] == "deleted"
            or not caller.may_see(record["sensitivity"])
        ):
            raise PrometheosError("not_found", "work not found", {"id": work_id})
        if (record["body"].get("merged_into") or {}).get("id"):
            raise PrometheosError(
                "validation_error", "this work is already merged", {"id": work_id}
            )
        records.append(record)
    x, y = pair_key(a, b)
    earlier = _choice(conn, x, y)
    if earlier is not None and earlier != "unlinked":
        raise PrometheosError("validation_error", "this pair was already decided", {})
    if bibimport.has_link(conn, a, b):
        raise PrometheosError("validation_error", "these works are already related versions", {})
    first, second = records
    if choice == "merge":
        ia, ib = first["body"]["identifiers"], second["body"]["identifiers"]
        for scheme in ("doi", "arxiv", "pmid", "pmcid", "isbn"):
            if ia.get(scheme) and ib.get(scheme) and ia[scheme] != ib[scheme]:
                raise PrometheosError(
                    "validation_error",
                    f"these works have different {scheme} identifiers: they are not one work",
                    {},
                )
    # The older work stands for both when merging; the preprint is the source of version_of.
    seq = {r["id"]: _created_seq(conn, r["id"]) for r in records}
    older, newer = sorted(records, key=lambda r: (seq[r["id"]], r["id"]))
    preprint, published = _version_order(first, second, seq)
    return {
        "op": "library_dedup_resolve",
        "work_a": x,
        "work_b": y,
        "revs": {first["id"]: first["rev"], second["id"]: second["rev"]},
        "choice": choice,
        "merged": newer["id"] if choice == "merge" else None,
        "into": older["id"] if choice == "merge" else None,
        "preprint": preprint if choice == "versions" else None,
        "published": published if choice == "versions" else None,
        # An undone import link (review round 2, R2-F2): the owner's new choice replaces it.
        "replaces": earlier,
    }


def _choice(conn: sqlite3.Connection, x: str, y: str) -> str | None:
    if not library_index.schema14(conn):
        return None
    row = conn.execute(
        "SELECT choice FROM library_decisions WHERE work_a = ? AND work_b = ?", (x, y)
    ).fetchone()
    return str(row[0]) if row is not None else None


def _created_seq(conn: sqlite3.Connection, record_id: str) -> tuple[int, int]:
    """When a record was created: its commit and its place in that commit."""
    row = conn.execute(
        "SELECT commit_seq, op_index FROM revisions WHERE record_id = ? AND rev = 1",
        (record_id,),
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def _version_order(
    a: dict[str, Any], b: dict[str, Any], seq: dict[str, tuple[int, int]]
) -> tuple[str, str]:
    """(preprint, published): an arXiv-only work is the preprint; otherwise the earlier year,
    then the earlier record."""

    def is_preprint(r: dict[str, Any]) -> bool:
        ids = r["body"]["identifiers"]
        return bool(ids.get("arxiv")) and not ids.get("doi")

    if is_preprint(a) != is_preprint(b):
        return (a["id"], b["id"]) if is_preprint(a) else (b["id"], a["id"])
    ordered = sorted((a, b), key=lambda r: (r["body"].get("year") or 9999, seq[r["id"]], r["id"]))
    return ordered[0]["id"], ordered[1]["id"]


def plan_digest(plan: dict[str, Any]) -> str:
    return canonical_sha256(plan)


def execute(
    store: Any,
    plan: dict[str, Any],
    note: str | None,
    actor: str,
    *,
    grant: Any = None,
    grant_digest: str | None = None,
    request_id: str,
) -> dict[str, Any]:
    """Carry out a resolve plan (the owner's, or an approved request's) in one commit: the
    works must still be at the planned revisions."""
    from prometheos.store import capabilities
    from prometheos.store.project import ProjectStore

    if note is not None and not 1 <= len(note.strip()) <= 2000:
        raise PrometheosError("validation_error", "a note is 1..2000 characters", {})
    request_sha = canonical_sha256({"op": "library_dedup_resolve", "plan": plan, "note": note})

    def handler(txn: Txn) -> None:
        if grant is not None:
            capabilities.consume(
                txn.conn,
                secret=grant,
                project_id=ProjectStore.project_id_in(txn.conn),
                operation="exact_request",
                digest=grant_digest,
            )
        for work_id, rev in plan["revs"].items():
            state = live(txn.get(work_id))
            if state is None or state.rev != rev:
                raise PrometheosError(
                    "revision_conflict", "a work changed since the choice was planned", {}
                )
        if _choice(txn.conn, plan["work_a"], plan["work_b"]) != plan.get("replaces"):
            raise PrometheosError("validation_error", "this pair was already decided", {})
        if plan.get("replaces") is not None:
            txn.conn.execute(
                "DELETE FROM library_decisions WHERE work_a = ? AND work_b = ? AND choice = ?",
                (plan["work_a"], plan["work_b"], plan["replaces"]),
            )
        if plan["choice"] == "merge":
            target = txn.get(plan["into"])
            merged = txn.get(plan["merged"])
            assert target is not None and merged is not None
            body = dict(merged.body)
            body["merged_into"] = {"id": target.id, "rev": target.rev}
            txn.op_index = 0
            body_id = txn.insert_body(body)
            txn.write_revision(
                merged.id, "work", merged.rev + 1, body_id, merged.sensitivity, "active", None,
                "library_dedup_merge", body=body,
            )  # fmt: skip
            library_index.index_work(txn.conn, merged.id, body)
            txn.result(merged.id, merged.rev + 1)
        elif plan["choice"] == "versions":
            bibimport.add_link(txn, plan["preprint"], plan["published"])
            txn.result(None, None)
        else:
            txn.audit("library_dedup_distinct", plan["work_a"], None)
            txn.result(None, None)
        txn.conn.execute(
            "INSERT INTO library_decisions (decision_id, work_a, work_b, choice, note, "
            "decided_by, decided_at) VALUES (?,?,?,?,?,?,?)",
            (
                f"dec_{uuid.uuid4().hex[:24]}",
                plan["work_a"],
                plan["work_b"],
                plan["choice"],
                note,
                actor[:100],
                utc_now(),
            ),
        )

    response = store.commit(
        request_id=request_id,
        request_sha256=request_sha,
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=2,
    )
    return {
        "choice": plan["choice"],
        "work_a": plan["work_a"],
        "work_b": plan["work_b"],
        "merged": plan["merged"],
        "into": plan["into"],
        "preprint": plan["preprint"],
        "published": plan["published"],
        "commit": response,
    }


# ----- undoing a choice or an import's version link (review round 1, F2/F9) ----------------------


def plan_undo(conn: sqlite3.Connection, caller: Caller, a: str, b: str) -> dict[str, Any]:
    """What undoing the pair's decision (or the import's version link) would do, bound to the
    current revisions: a merge is unmerged, a version link is retired, a distinct choice is
    forgotten. An import's link that is undone stays undone (the pair is marked unlinked);
    undoing that marker forgets it, so a later import may link the pair again, and the owner
    may relate the pair as versions at once (``resolve``, review round 2, R2-F2)."""
    if a == b:
        raise PrometheosError("validation_error", "a pair needs two different works", {})
    records = []
    for work_id in (a, b):
        record = current_record(conn, work_id)
        if (
            record is None
            or record["kind"] != "work"
            or record["lifecycle"] == "deleted"
            or not caller.may_see(record["sensitivity"])
        ):
            raise PrometheosError("not_found", "work not found", {"id": work_id})
        records.append(record)
    x, y = pair_key(a, b)
    row = conn.execute(
        "SELECT choice FROM library_decisions WHERE work_a = ? AND work_b = ?", (x, y)
    ).fetchone()
    link = conn.execute(
        "SELECT id, source_id, target_id FROM relations WHERE relation = 'version_of' AND "
        "retired_seq IS NULL AND ((source_id = ? AND target_id = ?) OR (source_id = ? AND "
        "target_id = ?)) ORDER BY id LIMIT 1",
        (a, b, b, a),
    ).fetchone()
    if row is None and link is None:
        raise PrometheosError("not_found", "nothing to undo for this pair", {})
    undo = str(row[0]) if row is not None else "link"
    merged = None
    if undo == "merge":
        merged = next(
            r["id"] for r in records if (r["body"].get("merged_into") or {}).get("id") in (a, b)
        )
        _refuse_shared_identifier(conn, merged)
    return {
        "op": "library_dedup_undo",
        "work_a": x,
        "work_b": y,
        "revs": {r["id"]: r["rev"] for r in records},
        "undo": undo,
        "merged": merged,
        "relation_id": int(link[0]) if link is not None and undo in ("versions", "link") else None,
    }


def _refuse_shared_identifier(conn: sqlite3.Connection, merged: str) -> None:
    """Unmerging ``merged`` must not leave two works with one identifier (review round 2,
    R2-F3): while it was merged, a later import may have given the work that stood for it the
    same DOI, arXiv identifier, PMID or PMCID. Such an undo is refused with the reason."""
    rows = conn.execute(
        "SELECT i.scheme, i.value, o.work_id FROM library_identifiers i "
        "JOIN library_identifiers o ON o.scheme = i.scheme AND o.value = i.value "
        "JOIN library_works w ON w.work_id = o.work_id "
        "JOIN records r ON r.id = o.work_id "
        "WHERE i.work_id = ? AND o.work_id <> i.work_id AND w.merged_into IS NULL "
        "AND r.lifecycle <> 'deleted' AND i.scheme IN ('doi', 'arxiv', 'pmid', 'pmcid') "
        "ORDER BY i.scheme, i.value, o.work_id LIMIT 1",
        (merged,),
    ).fetchall()
    if rows:
        scheme, value, other = (str(v) for v in rows[0])
        raise PrometheosError(
            "validation_error",
            f"cannot undo this merge: work {other} now has the same {scheme} ({value}), from an "
            "import made after the merge; undoing it would leave two works with one identifier",
            {"id": other},
        )


def execute_undo(
    store: Any,
    plan: dict[str, Any],
    note: str | None,
    actor: str,
    *,
    grant: Any = None,
    grant_digest: str | None = None,
    request_id: str,
) -> dict[str, Any]:
    from prometheos.store import capabilities
    from prometheos.store.project import ProjectStore

    request_sha = canonical_sha256({"op": "library_dedup_undo", "plan": plan, "note": note})

    def handler(txn: Txn) -> None:
        if grant is not None:
            capabilities.consume(
                txn.conn,
                secret=grant,
                project_id=ProjectStore.project_id_in(txn.conn),
                operation="exact_request",
                digest=grant_digest,
            )
        for work_id, rev in plan["revs"].items():
            state = live(txn.get(work_id))
            if state is None or state.rev != rev:
                raise PrometheosError(
                    "revision_conflict", "a work changed since the undo was planned", {}
                )
        txn.op_index = 0
        if plan["undo"] == "merge":
            merged = txn.get(plan["merged"])
            assert merged is not None
            _refuse_shared_identifier(txn.conn, merged.id)
            body = dict(merged.body)
            body["merged_into"] = None
            body_id = txn.insert_body(body)
            txn.write_revision(
                merged.id, "work", merged.rev + 1, body_id, merged.sensitivity, "active", None,
                "library_dedup_unmerge", body=body,
            )  # fmt: skip
            library_index.index_work(txn.conn, merged.id, body)
            txn.result(merged.id, merged.rev + 1)
        else:
            if plan["relation_id"] is not None:
                done = txn.conn.execute(
                    "UPDATE relations SET retired_seq = ? WHERE id = ? AND retired_seq IS NULL",
                    (txn.seq, plan["relation_id"]),
                )
                if done.rowcount != 1:
                    raise PrometheosError("revision_conflict", "the link changed", {})
            txn.audit("library_dedup_undo", plan["work_a"], None)
            txn.result(None, None)
        txn.conn.execute(
            "DELETE FROM library_decisions WHERE work_a = ? AND work_b = ?",
            (plan["work_a"], plan["work_b"]),
        )
        if plan["undo"] == "link":
            # An import's link stays undone: the next import of the same files does not relink.
            # Undoing this 'unlinked' marker in turn forgets it (review round 2, R2-F2).
            txn.conn.execute(
                "INSERT INTO library_decisions (decision_id, work_a, work_b, choice, note, "
                "decided_by, decided_at) VALUES (?,?,?,?,?,?,?)",
                (f"dec_{uuid.uuid4().hex[:24]}", plan["work_a"], plan["work_b"], "unlinked",
                 note, actor[:100], utc_now()),
            )  # fmt: skip

    response = store.commit(
        request_id=request_id,
        request_sha256=request_sha,
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=2,
    )
    return {
        "undone": plan["undo"],
        "work_a": plan["work_a"],
        "work_b": plan["work_b"],
        "commit": response,
    }
