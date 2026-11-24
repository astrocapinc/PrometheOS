"""The adoption outline (A06.1, CP20.4 part 6b; decision 0106): what a scanned folder says it is
for, proposed once and confirmed once by the owner.

The outline lists:

- **tracks**: the parallel contexts the folder forks by (the folders that make families
  ``variant`` or ``lineage``, such as ``track2``/``track5``);
- **deliverables**: LaTeX main files (a ``\\documentclass`` line), titled by their ``\\title``;
- **questions**: lines of notes that ask a research question (``RQ1:``, ``Q2.``, ``Question:``,
  or a list item or heading ending in ``?``);
- **results with their producers**: files a script writes (``generated_by`` evidence), with the
  script's line;
- **stale candidates**: results whose producer changed after they were written.

Every item cites where it comes from. A text item cites its file, the line and the exact words;
a track cites the folders it is read from (a folder is its own locator; there is no line). The
rules propose first. Proposals from the intelligence layer or the coding agent (the
``propose_outline`` assist kind) join as items whose quote was re-read from the bytes; they are
tagged ``proposed`` with the models that made them. Nothing here writes to the record or to the
adopted folder: :func:`confirm` (owner only) records what the owner keeps, in one transaction.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from prometheos.adoption import analyzers as az
from prometheos.adoption import classes, tables
from prometheos.adoption.contracts import MEMBER_SEPARATOR, AdoptionEvidence, AdoptionFile
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError, StrictModel, Title

ItemId = Annotated[str, StringConstraints(pattern=r"^oi_[0-9a-f]{16}$")]

OUTLINE_VERSION = 1
KINDS = ("track", "deliverable", "question", "result", "stale_candidate")
MAX_ITEMS = 400
MAX_TITLE = 200
TEXT_EXTENSIONS = (".md", ".markdown", ".txt", ".rst")
NOTE_ZONES = frozenset({"notes", "manuscript", "deliverable", "unknown", "literature"})
MAX_TEXT_BYTES = 512 * 1024
OUTLINE_ANALYZER = "ai.outline"

# A research question: "RQ1: ...", "Q2. ...", "Question 3 - ...", "Research question: ...", or a
# list item or heading whose text ends with a question mark.
_MARKED = re.compile(
    r"^\s*(?:[-*+>#]+\s*)?(?:\*\*)?(?:RQ\s*\d*|Q\d+|(?:research\s+)?question(?:\s*\d+)?)"
    r"(?:\*\*)?\s*[:.)\-"
    "\u2013\u2014"
    r"]\s*(?P<text>.{8,})$",
    re.IGNORECASE,
)
_ASKED = re.compile(r"^\s*(?:[-*+]|#{1,6}|\d+[.)])\s+(?P<text>[^?]{8,}\?)\s*$")
_DOCUMENTCLASS = re.compile(r"^\s*\\documentclass\b")
_TITLE = re.compile(r"^\s*\\title\s*(?:\[[^\]]*\])?\s*\{(?P<text>[^{}]{3,})\}")


def _cite(
    row: AdoptionFile | None, path: str, line: int | None, quote: str | None
) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": row.sha256 if row is not None else None,
        "line": line,
        "quote": quote,
    }


def _item(
    kind: str, key: str, title: str, cites: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    return {
        "item_id": "oi_" + canonical_sha256({"kind": kind, "key": key})[:16],
        "kind": kind,
        "key": key,
        "title": " ".join(title.split())[:MAX_TITLE],
        "cites": cites[:5],
        "source": "rule",
        "models": [],
        **extra,
    }


def _text_rows(rows: dict[str, AdoptionFile], extensions: tuple[str, ...]) -> list[AdoptionFile]:
    return [
        r
        for p, r in sorted(rows.items())
        if r.entry == "file"
        and r.status == "read"
        and r.container == "text"
        and MEMBER_SEPARATOR not in p
        and PurePosixPath(p).suffix.lower() in extensions
        and not any(classes._is_archive(f) for f in classes._folders(p))
        and (r.size or 0) <= MAX_TEXT_BYTES
    ]


def _lines(root: Path, row: AdoptionFile) -> list[str]:
    """The visible lines of a file whose bytes still match the inventory, or none."""
    from prometheos.adoption.assist import secret_name, secret_text, visible_lines

    if secret_name(row.path):
        return []
    data = az.read_bytes(root, row, MAX_TEXT_BYTES)
    if data is None or secret_text(data):
        return []
    return visible_lines(data)


def _tracks(
    families: list[Any], digests: dict[str, Any], decided: dict[str, str]
) -> list[dict[str, Any]]:
    """The context folders a folder forks by, from the families laid out per context."""
    from prometheos.adoption import plan

    folders: dict[str, set[str]] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    shown: dict[str, str] = {}
    for family in families:
        found = plan.family_class(family, digests, decided)
        paths = [m.path for m in family.members]
        # Variants and lineages fork by context; identical copies laid out the same way do too.
        forked = found["class"] in ("variant", "lineage") or (
            found["class"] == "identical" and classes._one_segment_apart(paths)
        )
        if not forked:
            continue
        split = [classes._parts(p) for p in paths]
        differing = [i for i in range(len(split[0]) - 1) if len({s[i].lower() for s in split}) > 1]
        if len(differing) != 1:
            continue
        i = differing[0]
        for s in split:
            name = s[i].lower()
            shown.setdefault(name, s[i])
            folders[name].add("/".join(s[: i + 1]))
            counts[name] += 1
    return [
        _item(
            "track",
            f"track:{name}",
            shown[name],
            [_cite(None, folder, None, None) for folder in sorted(folders[name])],
            files=counts[name],
        )
        for name in sorted(folders)
    ]


def _deliverables(root: Path, rows: dict[str, AdoptionFile]) -> list[dict[str, Any]]:
    out = []
    for row in _text_rows(rows, (".tex",)):
        lines = _lines(root, row)[:400]
        main = next((n for n, t in enumerate(lines, 1) if _DOCUMENTCLASS.match(t)), None)
        if main is None:
            continue
        titled = next(
            ((n, m) for n, t in enumerate(lines, 1) if (m := _TITLE.match(t)) is not None), None
        )
        if titled is not None:
            n, match = titled
            cite = _cite(row, row.path, n, lines[n - 1].strip()[:600])
            title = match.group("text")
        else:
            cite = _cite(row, row.path, main, lines[main - 1].strip()[:600])
            title = PurePosixPath(row.path).stem
        key = f"deliverable:{' '.join(title.lower().split())}"
        out.append(_item("deliverable", key, title, [cite], path=row.path))
    return out


def _questions(root: Path, rows: dict[str, AdoptionFile]) -> list[dict[str, Any]]:
    out = []
    for row in _text_rows(rows, TEXT_EXTENSIONS):
        if row.zone not in NOTE_ZONES:
            continue
        for n, text in enumerate(_lines(root, row)[:2000], start=1):
            match = _MARKED.match(text) or _ASKED.match(text)
            if match is None:
                continue
            asked = match.group("text").strip().strip("*").strip()
            out.append(
                _item(
                    "question",
                    f"question:{' '.join(asked.lower().split())}",
                    asked,
                    [_cite(row, row.path, n, text.strip()[:600])],
                )
            )
    return out


def _results(
    rows: dict[str, AdoptionFile], evidence: list[AdoptionEvidence]
) -> list[dict[str, Any]]:
    out = []
    seen: set[str] = set()
    for e in sorted(evidence, key=lambda e: (e.subject, e.object.path or "")):
        if e.predicate != "generated_by" or not e.object.path:
            continue
        result, producer = rows.get(e.subject), rows.get(e.object.path)
        if result is None or producer is None or result.status != "read" or e.subject in seen:
            continue
        seen.add(e.subject)
        line = e.locator.start if e.locator is not None else None
        cite = _cite(producer, producer.path, line, e.quote)
        common = {
            "path": result.path,
            "sha256": result.sha256,
            "producer": producer.path,
            "producer_sha256": producer.sha256,
        }
        out.append(_item("result", f"result:{result.path}", PurePosixPath(result.path).name,
                         [cite], **common))  # fmt: skip
        newer = (
            producer.mtime_us is not None
            and result.mtime_us is not None
            and producer.mtime_us > result.mtime_us
        )
        if newer:
            out.append(
                _item(
                    "stale_candidate",
                    f"stale:{result.path}",
                    PurePosixPath(result.path).name,
                    [cite],
                    reason="its producer changed after it was written",
                    **common,
                )
            )
    return out


def _proposed(rows: dict[str, AdoptionFile], evidence: list[AdoptionEvidence],
              models: dict[str, list[str]]) -> list[dict[str, Any]]:  # fmt: skip
    """Accepted ``propose_outline`` results, as evidence rows: each a verified quote (T4)."""
    out = []
    for e in evidence:
        if e.analyzer != OUTLINE_ANALYZER or e.predicate != "facet" or e.object.facet is None:
            continue
        kind = e.object.facet.removeprefix("outline.")
        literal = e.object.literal if isinstance(e.object.literal, dict) else {}
        title = str(literal.get("title") or "")
        if kind not in ("track", "deliverable", "question") or not title:
            continue
        line = e.locator.start if e.locator is not None else None
        key = f"{kind}:{' '.join(title.lower().split())}"
        item = _item(kind, key, title, [_cite(rows.get(e.subject), e.subject, line, e.quote)])
        item["source"] = "proposed"
        item["models"] = models.get(e.subject, [])
        out.append(item)
    return out


def _merge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One item per kind and key; a rule item and a proposal of the same thing merge their
    citations and read ``rule+proposed``."""
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        held = by_id.get(item["item_id"])
        if held is None:
            by_id[item["item_id"]] = item
            continue
        for cite in item["cites"]:
            if cite not in held["cites"] and len(held["cites"]) < 5:
                held["cites"].append(cite)
        if item["source"] != held["source"]:
            held["source"] = "rule+proposed"
        held["models"] = sorted(set(held["models"]) | set(item["models"]))
    order = {k: i for i, k in enumerate(KINDS)}
    return sorted(by_id.values(), key=lambda i: (order[i["kind"]], i["key"]))


def _folder(item: dict[str, Any]) -> str:
    """The folder of an item's first cited file ("" at the root)."""
    if not item["cites"]:
        return ""
    parent = str(PurePosixPath(item["cites"][0]["path"]).parent)
    return "" if parent == "." else parent.lower()


def _inside(folder: str, holder: str) -> bool:
    return holder == "" or folder == holder or folder.startswith(holder + "/")


def _place(items: list[dict[str, Any]]) -> None:
    """A question's proposed placement: the deliverable in the nearest folder holding the
    question's file, else the objective; and the tracks its file's path or its words name. A
    proposal only: the owner keeps, changes or drops it."""
    deliverables = [i for i in items if i["kind"] == "deliverable"]
    tracks = [i for i in items if i["kind"] == "track"]
    for item in items:
        if item["kind"] != "question":
            continue
        folder = _folder(item)
        holders = sorted(
            (d for d in deliverables if _inside(folder, _folder(d))),
            key=lambda d: -len(_folder(d)),
        )
        nearest = [d for d in holders if _folder(d) == _folder(holders[0])] if holders else []
        item["addresses"] = nearest[0]["item_id"] if len(nearest) == 1 else "objective"
        path = "/".join(c["path"].lower() for c in item["cites"])
        words = item["title"].lower()
        item["draws_on"] = sorted(
            t["item_id"]
            for t in tracks
            if re.search(rf"(^|/){re.escape(t['title'])}(/|$)", path)
            or re.search(rf"\b{re.escape(t['title'])}\b", words)
        )


def propose(conn: sqlite3.Connection, root: Path, scan_gen: int) -> dict[str, Any]:
    """The outline of one complete scan. Read-only; the same scan and the same accepted
    proposals give the same items and digest."""
    from prometheos.adoption import assist, plan

    scan = tables.require_scan(conn, scan_gen)
    if scan.state != "complete":
        raise PrometheosError("validation_error", "an outline needs a complete scan", {})
    rows = {r.path: r for r in tables.all_files(conn, scan_gen)}
    families = tables.list_families(conn, scan_gen)
    evidence = tables.all_evidence(conn, scan_gen)
    digests = plan.digest_map(rows)
    decided = plan.class_decisions(conn, scan_gen)
    models: dict[str, list[str]] = {}
    for found in assist.accepted_results(conn):
        if found["kind"] == "propose_outline":
            models[found["subject"]] = found.get("models", [])
    items = _merge(
        _tracks(families, digests, decided)
        + _deliverables(root, rows)
        + _questions(root, rows)
        + _results(rows, evidence)
        + _proposed(rows, evidence, models)
    )
    _place(items)
    truncated = len(items) > MAX_ITEMS
    items = items[:MAX_ITEMS]
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}
    body = {"version": OUTLINE_VERSION, "scan_gen": scan_gen, "items": items}
    return {
        "scan_gen": scan_gen,
        "root_alias": scan.root_alias,
        "version": OUTLINE_VERSION,
        "items": items,
        "counts": counts,
        "truncated": truncated,
        "sha256": canonical_sha256(body),
        "confirmed": latest(conn, scan.root_alias),
    }


# ----- the owner's one confirmation ----------------------------------------------------------


def latest(conn: sqlite3.Connection, root_alias: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM adoption_outlines WHERE root_alias = ? ORDER BY confirmed_at DESC, "
        "outline_id DESC LIMIT 1",
        (root_alias,),
    ).fetchone()
    if row is None:
        return None
    return {
        "outline_id": row["outline_id"],
        "scan_gen": row["scan_gen"],
        "proposal_sha256": row["proposal_sha256"],
        "sha256": row["confirmed_sha256"],
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
        "request_id": row["request_id"],
        "outline": json.loads(row["confirmed"]),
    }


class Edits(StrictModel):
    """The owner's corrections before confirming: items left out, new titles, and where a
    question belongs (a kept deliverable's item, ``objective``, or nowhere) and which kept tracks
    it draws on. A question not named keeps its proposed placement."""

    drop: list[ItemId] = Field(default_factory=list, max_length=MAX_ITEMS)
    titles: dict[ItemId, Title] = Field(default_factory=dict, max_length=MAX_ITEMS)
    addresses: dict[ItemId, ItemId | Literal["objective"] | None] = Field(
        default_factory=dict, max_length=MAX_ITEMS
    )
    draws_on: dict[ItemId, list[ItemId]] = Field(default_factory=dict, max_length=MAX_ITEMS)


def _fold(title: str) -> str:
    return " ".join(title.casefold().split())


def _existing(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """Active tracks, questions and papers by (kind, folded title): a re-adoption reuses them
    instead of recording them twice."""
    from prometheos.app.claim_status import _rows
    from prometheos.app.reads import Caller

    owner = Caller("adoption-outline", "owner")
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for record in _rows(conn, "entry", owner):
        category = record["body"].get("category")
        if category in ("track", "question"):
            found.setdefault((category, _fold(record["body"]["title"])), record)
    for record in _rows(conn, "paper", owner):
        if record["body"].get("status") != "withdrawn":
            found.setdefault(("deliverable", _fold(record["body"]["title"])), record)
    return found


def _where(item: dict[str, Any], alias: str) -> str:
    parts = []
    for cite in item["cites"]:
        at = f"{alias}:{cite['path']}"
        if cite["line"] is not None:
            at += f" line {cite['line']}"
        parts.append(at)
    return "From the adoption outline: " + "; ".join(parts)


def confirmation_plan(
    conn: sqlite3.Connection, root: Path, scan_gen: int, proposal_sha256: str, edits: Edits
) -> dict[str, Any]:
    """What confirming records, without writing: the kept items with their final titles and
    placements, the records to create (or reuse, by kind and title) and their operations."""
    from prometheos.domain.ids import new_id

    proposal = propose(conn, root, scan_gen)
    if proposal["sha256"] != proposal_sha256:
        raise PrometheosError(
            "revision_conflict",
            "the outline changed since it was shown (a new scan or new proposals); review it "
            "again before confirming",
            {"sha256": proposal["sha256"]},
        )
    if proposal["truncated"]:
        raise PrometheosError(
            "validation_error", f"the outline has more than {MAX_ITEMS} items", {}
        )
    by_id = {i["item_id"]: i for i in proposal["items"]}
    named = set(edits.drop) | set(edits.titles) | set(edits.addresses) | set(edits.draws_on)
    for values in edits.draws_on.values():
        named |= set(values)
    named |= {v for v in edits.addresses.values() if v not in (None, "objective")}
    unknown = sorted(named - by_id.keys())
    if unknown:
        raise PrometheosError("validation_error", "the edits name items not in this outline",
                              {"items": unknown[:20]})  # fmt: skip
    dropped = set(edits.drop)
    kept = [dict(i) for i in proposal["items"] if i["item_id"] not in dropped]
    kept_ids = {i["item_id"] for i in kept}
    problems = []
    for item_id in edits.titles:
        if by_id[item_id]["kind"] not in ("track", "deliverable", "question"):
            problems.append(f"{item_id}: only tracks, deliverables and questions have titles")
    for item_id in set(edits.addresses) | set(edits.draws_on):
        if by_id[item_id]["kind"] != "question":
            problems.append(f"{item_id}: only a question is placed")
    for item in kept:
        if item["item_id"] in edits.titles:
            item["title"] = " ".join(edits.titles[item["item_id"]].split())[:MAX_TITLE]
        if item["kind"] != "question":
            continue
        if item["item_id"] in edits.addresses:
            target = edits.addresses[item["item_id"]]
            if target not in (None, "objective") and (
                target not in kept_ids or by_id[target]["kind"] != "deliverable"
            ):
                problems.append(f"{item['item_id']}: placed under an item that is not a kept "
                                "deliverable")  # fmt: skip
            item["addresses"] = target
        elif item.get("addresses") not in (None, "objective") and item["addresses"] in dropped:
            item["addresses"] = "objective"  # its proposed deliverable was left out
        if item["item_id"] in edits.draws_on:
            chosen = sorted(set(edits.draws_on[item["item_id"]]))
            bad = [t for t in chosen if t not in kept_ids or by_id[t]["kind"] != "track"]
            if bad:
                problems.append(f"{item['item_id']}: draws on items that are not kept tracks")
            item["draws_on"] = chosen
        else:
            item["draws_on"] = [t for t in item.get("draws_on", []) if t in kept_ids]
    if problems:
        raise PrometheosError("validation_error", "the edits do not fit the outline",
                              {"problems": problems[:20]})  # fmt: skip
    existing = _existing(conn)
    records: dict[str, dict[str, Any]] = {}
    operations: list[dict[str, Any]] = []
    alias = proposal["root_alias"]
    for item in kept:
        if item["kind"] not in ("track", "deliverable", "question"):
            continue
        found = existing.get((item["kind"], _fold(item["title"])))
        if found is not None:
            records[item["item_id"]] = {"id": found["id"], "rev": found["rev"], "created": False}
            continue
        if item["kind"] == "deliverable":
            rid, kind = new_id("paper"), "paper"
            body: dict[str, Any] = {
                "title": item["title"], "objective": _where(item, alias), "claim_refs": [],
                "manuscript_source_id": None, "venue_source_refs": [], "status": "planning",
                "deadline": None, "requirements": [], "contribution": "", "related_papers": [],
                "review_rounds": [],
            }  # fmt: skip
        else:
            rid, kind = new_id("entry"), "entry"
            body = {
                "title": item["title"], "text": _where(item, alias), "category": item["kind"],
                "evidence": [], "claim_refs": [], "author_ref": None, "origin": "imported",
                "quantities": [], "review": "reviewed",
            }  # fmt: skip
        records[item["item_id"]] = {"id": rid, "rev": 1, "created": True}
        operations.append({"op": "create", "kind": kind, "id": rid, "base_rev": 0,
                           "sensitivity": "normal", "occurred_at": None, "body": body})  # fmt: skip
    return {
        "proposal": proposal,
        "kept": kept,
        "dropped": sorted(dropped),
        "records": records,
        "operations": operations,
    }


def relations_for(
    conn: sqlite3.Connection, plan: dict[str, Any], objective: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """``addresses`` (a question -> its deliverable, or the objective entry when the project has
    one) and ``draws_on`` (a question -> a track), leaving out links that already exist."""
    have = {
        (s, t, r)
        for s, t, r in conn.execute(
            "SELECT source_id, target_id, relation FROM relations WHERE retired_seq IS NULL "
            "AND relation IN ('addresses', 'draws_on')"
        )
    }
    out = []
    records = plan["records"]

    def add(source: dict[str, Any], end: dict[str, Any], relation: str) -> None:
        if (source["id"], end["id"], relation) in have:
            return
        have.add((source["id"], end["id"], relation))
        out.append({"op": "relation_add", "relation": relation,
                    "source_ref": {"id": source["id"], "rev": source["rev"]},
                    "target_ref": {"id": end["id"], "rev": end["rev"]}})  # fmt: skip

    for item in plan["kept"]:
        if item["kind"] != "question" or item["item_id"] not in records:
            continue
        source = records[item["item_id"]]
        target = item.get("addresses")
        end = objective if target == "objective" else records.get(target) if target else None
        if end is not None:
            add(source, end, "addresses")
        for track in item.get("draws_on", []):
            if track in records:
                add(source, records[track], "draws_on")
    return out


def _objective(conn: sqlite3.Connection) -> dict[str, Any] | None:
    from prometheos.app.claim_status import _rows
    from prometheos.app.reads import Caller

    project = next(iter(_rows(conn, "project", Caller("adoption-outline", "owner"))), None)
    ref = ((project or {}).get("body") or {}).get("objective_entry_ref") or {}
    if not ref.get("id"):
        return None
    row = conn.execute(
        "SELECT current_rev FROM records WHERE id = ? AND lifecycle = 'active'", (ref["id"],)
    ).fetchone()
    return None if row is None else {"id": ref["id"], "rev": row[0]}


def preview(
    conn: sqlite3.Connection, root: Path, scan_gen: int, proposal_sha256: str, edits: Edits
) -> dict[str, Any]:
    """What one confirmation would record, for the owner to read before confirming."""
    plan = confirmation_plan(conn, root, scan_gen, proposal_sha256, edits)
    relations = relations_for(conn, plan, _objective(conn))
    kept = plan["kept"]
    created = [r for r in plan["records"].values() if r["created"]]
    return {
        "scan_gen": scan_gen,
        "proposal_sha256": proposal_sha256,
        "kept": {k: sum(1 for i in kept if i["kind"] == k) for k in KINDS},
        "dropped": len(plan["dropped"]),
        "records_created": len(created),
        "records_reused": len(plan["records"]) - len(created),
        "relations": len(relations),
        "unplaced_questions": sum(
            1 for i in kept if i["kind"] == "question" and i.get("addresses") is None
        ),
        "objective_set": _objective(conn) is not None,
        "edits_sha256": canonical_sha256(edits.model_dump(mode="json")),
    }


def confirm(
    store: Any,
    root: Path,
    scan_gen: int,
    proposal_sha256: str,
    edits: Edits,
    note: str | None,
    actor: str,
    *,
    grant: Any = None,
    grant_digest: str | None = None,
) -> dict[str, Any]:
    """The owner's one confirmation (C20-ADOPT): the kept tracks, questions and deliverables
    become records, the questions' placements become ``addresses`` and ``draws_on`` links, and
    the confirmed outline (with its results, their producers and the stale candidates) is kept,
    all in one transaction. Nothing is written into the adopted folder. Owner only: the caller
    checks the owner's act."""
    from prometheos.app import apply
    from prometheos.domain.canonical import dumps_canonical
    from prometheos.domain.ids import new_request_id
    from prometheos.domain.models import Request, utc_now

    if note is not None and not 1 <= len(note.strip()) <= 2000:
        raise PrometheosError("validation_error", "a note is 1..2000 characters", {})
    conn = store.connect(readonly=True)
    try:
        plan = confirmation_plan(conn, root, scan_gen, proposal_sha256, edits)
        relations = relations_for(conn, plan, _objective(conn))
    finally:
        conn.close()
    operations = plan["operations"] + relations
    request_id = new_request_id()
    confirmed = {
        "version": OUTLINE_VERSION,
        "scan_gen": scan_gen,
        "root_alias": plan["proposal"]["root_alias"],
        "proposal_sha256": proposal_sha256,
        "edits": edits.model_dump(mode="json"),
        "items": [{**i, "record": plan["records"].get(i["item_id"])} for i in plan["kept"]],
        "dropped": plan["dropped"],
        "note": note.strip() if note else None,
    }
    digest = canonical_sha256(confirmed)
    outline_id = "ol_" + digest[:24]
    request = (
        Request.model_validate(
            {"schema_version": 1, "project_id": store.project_id, "request_id": request_id,
             "expected_seq": 0, "operations": operations}
        )
        if operations
        else None
    )  # fmt: skip

    def handler(txn: Any) -> None:
        if grant is not None:  # an agent's request the owner granted, spent here
            from prometheos.store import capabilities

            capabilities.consume(txn.conn, secret=grant, project_id=store.project_id,
                                 operation="exact_request", digest=grant_digest)  # fmt: skip
        if request is not None:
            # Review cp20-a-r1 A1: the plan was read on another connection, so a confirmation
            # committed meanwhile may already hold these titles; refuse rather than duplicate.
            now = _existing(txn.conn)
            for item in plan["kept"]:
                record = plan["records"].get(item["item_id"])
                if record and record["created"] and (item["kind"], _fold(item["title"])) in now:
                    raise PrometheosError(
                        "revision_conflict",
                        "the outline was confirmed meanwhile; review it again",
                        {},
                    )
            apply._handler(request, store, None, "", None)(txn)
        txn.conn.execute(
            "INSERT INTO adoption_outlines (outline_id, scan_gen, root_alias, proposal_sha256, "
            "confirmed, confirmed_sha256, confirmed_by, confirmed_at, request_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (outline_id, scan_gen, confirmed["root_alias"], proposal_sha256,
             dumps_canonical(confirmed).decode(), digest, actor[:100], utc_now(), request_id),
        )  # fmt: skip
        txn.audit("adoption_outline_confirm", None, None)

    committed = store.commit(
        request_id=request_id,
        request_sha256=digest,
        expected_seq=None,
        actor=actor,
        handler=handler,
    )
    return {
        "outline_id": outline_id,
        "sha256": digest,
        "scan_gen": scan_gen,
        "seq": committed.get("project_seq"),
        "records_created": sum(1 for r in plan["records"].values() if r["created"]),
        "relations_added": len(relations),
        "records": {i: r["id"] for i, r in plan["records"].items()},
    }


__all__ = ["KINDS", "OUTLINE_ANALYZER", "Edits", "confirm", "confirmation_plan", "latest",
           "preview", "propose", "relations_for"]  # fmt: skip
