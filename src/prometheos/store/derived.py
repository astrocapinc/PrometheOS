"""Rebuildable derived indexes kept inside the write transaction.

``refs``: every record reference found in a record's current body (links and
backlinks, dependency closure for corrections and purge). ``search_text``:
title/text for keyword search, using FTS5 when SQLite provides it and a
substring fallback otherwise. ``view_flags``: dependents marked
``needs_review`` by an applied correction until they are revised.
All three can be dropped and rebuilt from records/revisions.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from prometheos.domain.ids import RECORD_ID_PATTERN

_ID_IN_TEXT = re.compile(RECORD_ID_PATTERN.strip("^$"))

V2_SQL = """
CREATE TABLE capabilities (
    id TEXT PRIMARY KEY,
    secret_sha256 TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    digest TEXT,
    max_uses INTEGER NOT NULL,
    uses INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    label TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE refs (
    from_id TEXT NOT NULL,
    from_rev INTEGER NOT NULL,
    to_id TEXT NOT NULL,
    to_rev INTEGER,
    field TEXT NOT NULL
);
CREATE INDEX refs_to ON refs(to_id);
CREATE INDEX refs_from ON refs(from_id);

CREATE TABLE view_flags (
    id INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    cause_id TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    cleared_seq INTEGER
);
CREATE INDEX view_flags_record ON view_flags(record_id, cleared_seq);

CREATE TABLE search_text (
    record_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE purge_plans (
    plan_id TEXT PRIMARY KEY,
    plan_sha256 TEXT NOT NULL,
    plan TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','committed'))
);

CREATE TABLE purges (
    id INTEGER PRIMARY KEY,
    plan_sha256 TEXT NOT NULL,
    request_id TEXT NOT NULL,
    commit_seq INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    payload_count INTEGER NOT NULL,
    media_count INTEGER NOT NULL
);

CREATE TABLE store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FTS_SQL = (
    "CREATE VIRTUAL TABLE search_fts USING fts5(title, text, content='search_text', "
    "content_rowid='rowid', tokenize='unicode61')"
)


TRANSITION_NOTES_SQL = """
CREATE TABLE transition_notes (
    id INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL,
    rev INTEGER NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT,
    evidence TEXT NOT NULL,
    protected TEXT NOT NULL,
    commit_seq INTEGER NOT NULL
);
CREATE INDEX transition_notes_record ON transition_notes(record_id, rev);
"""


def migrate_v3(conn: sqlite3.Connection) -> None:
    """Schema 3: recorded transition reasons, evidence and protected-effect labels."""
    for statement in TRANSITION_NOTES_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)


def migrate_v2(conn: sqlite3.Connection) -> None:
    for statement in V2_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
    try:
        conn.execute(FTS_SQL)
        mode = "fts5"
    except sqlite3.OperationalError:
        mode = "substring"
    conn.execute("INSERT INTO store_meta (key, value) VALUES ('search_mode', ?)", (mode,))
    rebuild(conn)


def search_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM store_meta WHERE key = 'search_mode'").fetchone()
    return str(row[0]) if row else "substring"


def body_refs(body: Any, field: str = "") -> list[tuple[str, int | None, str]]:
    """All record references in a body: ({id, rev} objects and bare ID strings)."""
    found: list[tuple[str, int | None, str]] = []
    if isinstance(body, dict):
        if isinstance(body.get("id"), str) and isinstance(body.get("rev"), int) and len(body) == 2:
            if _ID_IN_TEXT.fullmatch(body["id"]):
                found.append((body["id"], body["rev"], field))
            return found
        for key, value in body.items():
            found.extend(body_refs(value, f"{field}/{key}" if field else key))
    elif isinstance(body, list):
        for value in body:
            found.extend(body_refs(value, field))
    elif isinstance(body, str) and _ID_IN_TEXT.fullmatch(body):
        found.append((body, None, field))
    return found


SEARCH_FIELDS = {
    "entry": ("title", "text"),
    "claim": ("statement", None),
    "assessment": (None, "rationale"),
    "correction": (None, "reason"),
    "person": ("display_name", "notes"),
    "meeting": ("title", None),
    "item": (None, "text"),
    "action": ("title", "reason"),
    "source": ("title", "description"),
    "project": ("title", "current_objective"),
    "paper": ("title", "objective"),
    "execution": ("title", None),
    "work": ("title", None),  # CP11 S11.1: works are found by title (page text: library search)
}


def index_record(
    conn: sqlite3.Connection, record_id: str, kind: str, rev: int, body: dict[str, Any] | None
) -> None:
    conn.execute("DELETE FROM refs WHERE from_id = ?", (record_id,))
    _unindex_search(conn, record_id)
    if body is None:
        return
    for to_id, to_rev, field in body_refs(body):
        if to_id != record_id:
            conn.execute(
                "INSERT INTO refs (from_id, from_rev, to_id, to_rev, field) VALUES (?,?,?,?,?)",
                (record_id, rev, to_id, to_rev, field),
            )
    title_field, text_field = SEARCH_FIELDS.get(kind, (None, None))
    title = str(body.get(title_field, "")) if title_field else ""
    text = str(body.get(text_field, "")) if text_field else ""
    if kind == "claim":
        text = " ".join(str(v) for v in body.get("scope", {}).values() if v is not None)
    if title or text:
        index_text(conn, record_id, kind, title, text)


def index_text(conn: sqlite3.Connection, record_id: str, kind: str, title: str, text: str) -> None:
    _unindex_search(conn, record_id)
    cur = conn.execute(
        "INSERT INTO search_text (record_id, kind, title, text) VALUES (?,?,?,?)",
        (record_id, kind, title, text),
    )
    if search_mode(conn) == "fts5":
        conn.execute(
            "INSERT INTO search_fts (rowid, title, text) VALUES (?,?,?)",
            (cur.lastrowid, title, text),
        )


def _unindex_search(conn: sqlite3.Connection, record_id: str) -> None:
    row = conn.execute(
        "SELECT rowid, title, text FROM search_text WHERE record_id = ?", (record_id,)
    ).fetchone()
    if row is None:
        return
    if search_mode(conn) == "fts5":
        conn.execute(
            "INSERT INTO search_fts (search_fts, rowid, title, text) VALUES ('delete', ?, ?, ?)",
            (row[0], row[1], row[2]),
        )
    conn.execute("DELETE FROM search_text WHERE record_id = ?", (record_id,))


def rebuild(conn: sqlite3.Connection) -> None:
    """Recreate refs and search from current revisions (and retained text payloads)."""
    conn.execute("DELETE FROM refs")
    if search_mode(conn) == "fts5":
        conn.execute("INSERT INTO search_fts (search_fts) VALUES ('delete-all')")
    conn.execute("DELETE FROM search_text")
    rows = conn.execute(
        "SELECT r.id, r.kind, r.current_rev, b.encoded FROM records r JOIN revisions v ON "
        "v.record_id = r.id AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id"
    ).fetchall()
    for row in rows:
        body = json.loads(row[3]) if row[3] is not None else None
        index_record(conn, row[0], row[1], row[2], body)
        if (
            row[1] == "version"
            and body
            and body.get("storage") == "body"
            and body.get("payload_schema") == "text"
        ):
            payload = conn.execute(
                "SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)
            ).fetchone()
            if payload is not None:
                index_text(
                    conn, row[0], "version", "", bytes(payload[0]).decode("utf-8", "replace")
                )
    # CP11 S11.1: the library's page index and work map (store schema 13 and later).
    from prometheos.library import index as library_index

    library_index.rebuild(conn)


# The research-map relations (A06.1, CP20.2) are descriptive: a correction to a question or a
# track flags nothing that answers or draws on it (review cp20-2-a F4, decision 0083).
MAP_RELATIONS = "('answers', 'addresses', 'draws_on')"


def flag_dependents(
    conn: sqlite3.Connection, target_ids: list[str], reason: str, cause_id: str, seq: int
) -> list[str]:
    """Mark records that reference any target as needing review; return their IDs."""
    if not target_ids:
        return []
    marks = ",".join("?" for _ in target_ids)
    rows = conn.execute(
        f"SELECT DISTINCT from_id FROM refs WHERE to_id IN ({marks})",  # noqa: S608
        target_ids,
    ).fetchall()
    rel_rows = conn.execute(
        f"SELECT DISTINCT source_id FROM relations WHERE retired_seq IS NULL "  # noqa: S608
        f"AND relation NOT IN {MAP_RELATIONS} AND target_id IN ({marks})",
        target_ids,
    ).fetchall()
    dependents = sorted(
        ({r[0] for r in rows} | {r[0] for r in rel_rows}) - set(target_ids) - {cause_id}
    )
    for record_id in dependents:
        conn.execute(
            "INSERT INTO view_flags (record_id, reason, cause_id, created_seq) VALUES (?,?,?,?)",
            (record_id, reason, cause_id, seq),
        )
    return dependents


# ----- the affected closure of an applied correction (A02, CP10.4; decision 0018) -----------


# Kinds a hop beyond the first may flag: records that rely on what they point to. Assessments,
# meeting items, sources and versions only record history, so they are flagged when they are
# a *direct* dependent (as before CP10.4) but never carry a flag further.
RELIANCE_KINDS = frozenset({"claim", "entry", "action", "paper", "anchor", "extraction"})
# Kinds the closure expands from: a claim (its anchors, papers, actions, notes), an entry (a
# finding entry or assumption that actions, anchors and other entries rely on, decision 0018
# round 2), and an action (the actions that depend on it). Papers and anchors are terminal (an
# anchor's paper is containment, not reliance).
EXPANDING_KINDS = frozenset({"claim", "entry", "action", "extraction"})
# CP11 S11.4 (decision 0064; spec/03 RP-00): a matrix row (``extraction``) relies on what it
# cites and expands to the generated briefs it is ``used_in``: a brief version is flagged when
# it is reached from a row, never through any other edge.


def _kind_of_record(conn: sqlite3.Connection, record_id: str) -> tuple[str, str] | None:
    """(kind, lifecycle) of a record, None when unknown."""
    row = conn.execute("SELECT kind, lifecycle FROM records WHERE id = ?", (record_id,)).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def _direct_dependents(conn: sqlite3.Connection, record_id: str) -> list[str]:
    """Records that reference or relate to ``record_id`` (the pre-CP10.4 first hop), plus the
    records it is ``used_in`` (manuscript uses). Every existing reference is to an earlier
    revision than the one being written, so none is skipped as "already up to date" (a
    dependent cannot be bound to a revision that does not exist yet)."""
    found: set[str] = set()
    for row in conn.execute("SELECT from_id FROM refs WHERE to_id = ?", (record_id,)):
        found.add(row[0])
    for row in conn.execute(
        f"SELECT source_id FROM relations WHERE retired_seq IS NULL AND target_id = ? "  # noqa: S608
        f"AND relation NOT IN {MAP_RELATIONS}",
        (record_id,),
    ):
        found.add(row[0])
    for row in conn.execute(
        "SELECT target_id FROM relations WHERE retired_seq IS NULL AND source_id = ? "
        "AND relation = 'used_in'",
        (record_id,),
    ):
        found.add(row[0])
    return sorted(found)


def _brief_of(conn: sqlite3.Connection, row_id: str, version_id: str) -> bool:
    """Whether ``version_id`` is a generated brief that the matrix row ``row_id`` is used in."""
    if not row_id.startswith("extraction_"):
        return False
    return (
        conn.execute(
            "SELECT 1 FROM relations WHERE source_id = ? AND target_id = ? AND relation = "
            "'used_in' AND retired_seq IS NULL",
            (row_id, version_id),
        ).fetchone()
        is not None
    )


def affected_closure(
    conn: sqlite3.Connection, roots: list[str], exclude: set[str]
) -> dict[str, dict[str, Any]]:
    """Every record that depends, directly or through reliance, on any of ``roots``.

    The first hop is exactly what ``flag_dependents`` always flagged, plus what an assumption
    or claim is ``used_in``. Each further hop follows the same edges from claims, entries and
    actions only, flagging only :data:`RELIANCE_KINDS`, so an assumption reaches the claims and
    finding entries that rely on it, and through them the anchors, papers, actions and notes
    that rely on those.
    Returns ``{dependent: {via, root, depth}}``; ``exclude`` (the correction, its targets and
    replacements) is never flagged. Deterministic and cycle-safe."""
    found: dict[str, dict[str, Any]] = {}
    frontier = [(root, root, 0) for root in sorted(roots)]
    while frontier:
        current, root, depth = frontier.pop(0)
        for dep in _direct_dependents(conn, current):
            if dep in exclude or dep in found or dep in roots:
                continue
            info = _kind_of_record(conn, dep)
            if info is None or info[1] == "deleted":
                continue
            kind, lifecycle = info
            if depth > 0 and (kind not in RELIANCE_KINDS or lifecycle != "active"):
                if not (kind == "version" and _brief_of(conn, current, dep)):
                    continue
            found[dep] = {"via": current, "root": root, "depth": depth + 1}
            if kind in EXPANDING_KINDS:
                frontier.append((dep, root, depth + 1))
    return found


def flag_closure(
    conn: sqlite3.Connection,
    closure: dict[str, dict[str, Any]],
    reason: str,
    cause_id: str,
    seq: int,
) -> list[str]:
    """Record a review flag (same transaction) for every record of the closure. A record
    reached through another one says so in the reason."""
    flagged = sorted(closure, key=lambda rid: (closure[rid]["depth"], rid))
    for record_id in flagged:
        info = closure[record_id]
        why = reason if info["depth"] == 1 else f"{reason}; relies on {info['via']}"
        conn.execute(
            "INSERT INTO view_flags (record_id, reason, cause_id, created_seq) VALUES (?,?,?,?)",
            (record_id, why, cause_id, seq),
        )
    return flagged


def clear_flags(conn: sqlite3.Connection, record_id: str, seq: int) -> None:
    conn.execute(
        "UPDATE view_flags SET cleared_seq = ? WHERE record_id = ? AND cleared_seq IS NULL",
        (seq, record_id),
    )
