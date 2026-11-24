"""The library's derived tables (store schema 13; CP11 S11.1, decision 0060).

``library_pages`` holds the text of every page of every ``pdf_pages_v1`` version, with an FTS5
index (``library_fts``) when SQLite provides it; ``library_works`` maps each current work to its
extracted version, its PDF version and the PDF's digest. Both are written in the transaction
that commits the records they describe, and both are rebuilt from records and payloads by
:func:`rebuild` (``derived.rebuild``, a purge). ``library_plans`` is operational: persisted
import plans, like ``import_plans``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from prometheos.domain.canonical import loads_internal

V13_LIBRARY = r"""
CREATE TABLE library_plans (
    plan_id TEXT PRIMARY KEY,
    plan_sha256 TEXT NOT NULL,
    plan TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'committed')),
    result TEXT
);

CREATE TABLE library_works (
    work_id TEXT PRIMARY KEY,
    text_version_id TEXT NOT NULL,
    pdf_version_id TEXT NOT NULL,
    pdf_source_id TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    title TEXT NOT NULL,
    page_count INTEGER NOT NULL CHECK (page_count >= 1)
);
CREATE INDEX library_works_sha ON library_works(pdf_sha256);
CREATE INDEX library_works_source ON library_works(pdf_source_id);
CREATE INDEX library_works_text ON library_works(text_version_id);

CREATE TABLE library_pages (
    id INTEGER PRIMARY KEY,
    version_id TEXT NOT NULL,
    page INTEGER NOT NULL CHECK (page >= 1),
    state TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (version_id, page)
)
"""

FTS_SQL = (
    "CREATE VIRTUAL TABLE library_fts USING fts5(text, content='library_pages', "
    "content_rowid='id', tokenize='unicode61')"
)


# Store schema 14 (CP11 S11.2; decision 0062): ``library_works`` covers every work (bibliography
# works have no extracted text, so those columns become nullable) and says which works are
# merged; ``library_identifiers`` is the normalised identifier index; ``library_entries`` maps a
# bibliography file's entries (source, key) to their works. All three are derived and rebuilt
# from records. ``library_decisions`` is operational: the owner's dedup choices.
V14_LIBRARY = r"""
DROP TABLE library_works;
CREATE TABLE library_works (
    work_id TEXT PRIMARY KEY,
    text_version_id TEXT,
    pdf_version_id TEXT,
    pdf_source_id TEXT,
    pdf_sha256 TEXT,
    title TEXT NOT NULL,
    page_count INTEGER CHECK (page_count IS NULL OR page_count >= 1),
    merged_into TEXT
);
CREATE INDEX library_works_sha ON library_works(pdf_sha256);
CREATE INDEX library_works_source ON library_works(pdf_source_id);
CREATE INDEX library_works_text ON library_works(text_version_id);

CREATE TABLE library_identifiers (
    scheme TEXT NOT NULL,
    value TEXT NOT NULL,
    work_id TEXT NOT NULL,
    PRIMARY KEY (scheme, value, work_id)
) WITHOUT ROWID;
CREATE INDEX library_identifiers_work ON library_identifiers(work_id);

CREATE TABLE library_entries (
    source_id TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    work_id TEXT NOT NULL,
    PRIMARY KEY (source_id, entry_key)
) WITHOUT ROWID;
CREATE INDEX library_entries_work ON library_entries(work_id);

CREATE TABLE library_decisions (
    decision_id TEXT PRIMARY KEY,
    work_a TEXT NOT NULL,
    work_b TEXT NOT NULL,
    choice TEXT NOT NULL CHECK (choice IN ('merge', 'distinct', 'versions', 'unlinked')),
    note TEXT,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE (work_a, work_b),
    CHECK (work_a < work_b)
)
"""


def migrate_v14(conn: sqlite3.Connection) -> None:
    """Schema 14: every work in the work map, the identifier index, entry keys, decisions."""
    for statement in V14_LIBRARY.split(";"):
        if statement.strip():
            conn.execute(statement)
    rebuild(conn)


def schema14(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'library_identifiers'"
    ).fetchone()
    return row is not None


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'library_fts'"
    ).fetchone()
    return row is not None


def installed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'library_pages'"
    ).fetchone()
    return row is not None


def migrate_v13(conn: sqlite3.Connection) -> None:
    """Schema 13: the library's plans and derived page index, rebuilt from existing records."""
    for statement in V13_LIBRARY.split(";"):
        if statement.strip():
            conn.execute(statement)
    from prometheos.store.derived import search_mode

    if search_mode(conn) == "fts5":
        try:
            conn.execute(FTS_SQL)
        except sqlite3.OperationalError:  # pragma: no cover - fts5 was available at schema 2
            pass
    rebuild(conn)


# ----- writing ---------------------------------------------------------------------------------


def index_pages(conn: sqlite3.Connection, version_id: str, payload: dict[str, Any]) -> None:
    """Index every page of one extracted version (replacing what was indexed for it)."""
    remove_version(conn, version_id)
    fts = has_fts(conn)
    for page in payload["pages"]:
        cur = conn.execute(
            "INSERT INTO library_pages (version_id, page, state, text) VALUES (?,?,?,?)",
            (version_id, int(page["page"]), str(page["state"]), str(page["text"])),
        )
        if fts and page["text"]:
            conn.execute(
                "INSERT INTO library_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, page["text"])
            )


def remove_version(conn: sqlite3.Connection, version_id: str) -> None:
    fts = has_fts(conn)
    rows = conn.execute(
        "SELECT id, text FROM library_pages WHERE version_id = ?", (version_id,)
    ).fetchall()
    for row in rows:
        if fts and row[1]:
            conn.execute(
                "INSERT INTO library_fts (library_fts, rowid, text) VALUES ('delete', ?, ?)",
                (row[0], row[1]),
            )
    conn.execute("DELETE FROM library_pages WHERE version_id = ?", (version_id,))


def index_work(conn: sqlite3.Connection, work_id: str, body: dict[str, Any] | None) -> None:
    """The derived rows of one work (its current body; ``None`` removes them): the work map,
    its identifiers and the bibliography entries it came from."""
    conn.execute("DELETE FROM library_works WHERE work_id = ?", (work_id,))
    v14 = schema14(conn)
    if v14:
        conn.execute("DELETE FROM library_identifiers WHERE work_id = ?", (work_id,))
        conn.execute("DELETE FROM library_entries WHERE work_id = ?", (work_id,))
    if body is None or not isinstance(body.get("title"), str):
        return
    text_id = pdf_id = pdf_source = pdf_sha = None
    pages = None
    refs = body.get("full_text_refs") or []
    if refs:
        text_body = _body_of(conn, str(refs[0]["id"]))
        payload = load_payload(conn, text_body) if text_body else None
        derived = (text_body or {}).get("derived_from") or {}
        pdf_body = _body_of(conn, str(derived.get("version_id")))
        if text_body is not None and payload is not None and pdf_body is not None:
            text_id, pdf_id = str(refs[0]["id"]), str(derived["version_id"])
            pdf_source, pdf_sha = str(pdf_body["source_id"]), str(pdf_body["sha256"])
            pages = int(payload["page_count"])
    merged = (body.get("merged_into") or {}).get("id")
    if not v14:
        if text_id is None:
            return  # schema 13 maps only works with extracted text
        conn.execute(
            "INSERT OR REPLACE INTO library_works (work_id, text_version_id, pdf_version_id, "
            "pdf_source_id, pdf_sha256, title, page_count) VALUES (?,?,?,?,?,?,?)",
            (work_id, text_id, pdf_id, pdf_source, pdf_sha, body["title"], pages),
        )
        return
    conn.execute(
        "INSERT OR REPLACE INTO library_works (work_id, text_version_id, pdf_version_id, "
        "pdf_source_id, pdf_sha256, title, page_count, merged_into) VALUES (?,?,?,?,?,?,?,?)",
        (work_id, text_id, pdf_id, pdf_source, pdf_sha, body["title"], pages, merged),
    )
    identifiers = body.get("identifiers") or {}
    rows = {
        (scheme, str(identifiers[scheme]))
        for scheme in ("doi", "arxiv", "pmid", "pmcid", "isbn")
        if identifiers.get(scheme)
    }
    for other in identifiers.get("other") or []:
        if other.get("scheme") in ("version_doi", "version_arxiv"):
            rows.add((str(other["scheme"]), str(other["value"])))
    for scheme, value in sorted(rows):
        conn.execute(
            "INSERT OR IGNORE INTO library_identifiers (scheme, value, work_id) VALUES (?,?,?)",
            (scheme, value, work_id),
        )
    for record in body.get("field_provenance") or []:
        entry = record.get("entry")
        if not entry:
            continue
        version = _body_of(conn, str(record["version_ref"]["id"]))
        if version is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO library_entries (source_id, entry_key, work_id) VALUES (?,?,?)",
            (str(version["source_id"]), str(entry), work_id),
        )


def _body_of(conn: sqlite3.Connection, record_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT b.encoded FROM records r JOIN revisions v ON v.record_id = r.id "
        "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.id = ? "
        "AND r.lifecycle != 'deleted' AND b.encoded IS NOT NULL",
        (record_id,),
    ).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    return dict(value) if isinstance(value, dict) else None


# ----- rebuilding ------------------------------------------------------------------------------


def _current_bodies(conn: sqlite3.Connection, kind: str) -> list[tuple[str, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT r.id, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id "
        "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = ? AND r.lifecycle != 'deleted' AND b.encoded IS NOT NULL "
        "ORDER BY r.created_seq, r.id",
        (kind,),
    ).fetchall()
    return [(str(row[0]), json.loads(row[1])) for row in rows]


def load_payload(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any] | None:
    """The stored ``pdf_pages_v1`` payload of an extracted version (inline payloads only)."""
    if body.get("payload_schema") != "pdf_pages_v1" or body.get("storage") != "body":
        return None
    row = conn.execute("SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)).fetchone()
    if row is None:
        return None
    value = loads_internal(bytes(row[0]))
    return dict(value) if isinstance(value, dict) else None


def rebuild(conn: sqlite3.Connection) -> None:
    """Recreate ``library_pages`` (and its FTS index) and ``library_works`` from current
    records and retained payloads. Purged records (no body) and missing payloads leave no row."""
    if not installed(conn):
        return
    fts = has_fts(conn)
    conn.execute("DELETE FROM library_pages")
    conn.execute("DELETE FROM library_works")
    versions = dict(_current_bodies(conn, "version"))
    for version_id, body in versions.items():
        payload = load_payload(conn, body)
        if payload is None:
            continue
        for page in payload.get("pages", []):
            conn.execute(
                "INSERT INTO library_pages (version_id, page, state, text) VALUES (?,?,?,?)",
                (version_id, int(page["page"]), str(page["state"]), str(page["text"])),
            )
    if fts:
        # The external-content index is rebuilt from library_pages, so no token of a removed
        # page survives in an old index segment.
        conn.execute("INSERT INTO library_fts (library_fts) VALUES ('rebuild')")
    if schema14(conn):
        conn.execute("DELETE FROM library_identifiers")
        conn.execute("DELETE FROM library_entries")
    for work_id, body in _current_bodies(conn, "work"):
        index_work(conn, work_id, body)
