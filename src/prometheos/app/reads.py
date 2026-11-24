"""Pure read paths: record get/list, as-of views and the caller sensitivity policy.

Reads never write. Agent-scope callers (the default for every transport) do
not see restricted/private records; such records are concealed as
``not_found`` on direct lookup and omitted from lists. Current privacy
restrictions take precedence over historical revisions. Version availability
is computed from retained bytes and saved observations, never trusted from
the stored capture-time field alone.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from prometheos.domain.bodies import normalise_stored_body
from prometheos.domain.canonical import (
    CanonicalJSONError,
    canonical_sha256,
    dumps_canonical,
    loads_internal,
    loads_strict,
)
from prometheos.domain.ids import KINDS, RECORD_ID_PATTERN
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError

Scope = Literal["agent", "owner"]
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class Caller:
    """Who is reading/writing. Agent scope is the default for every transport.

    Owner scope comes from a ``read_sensitive`` capability on transports (or is
    constructed directly by trusted in-process callers such as tests); ``full_data``
    additionally permits whole-store exports (backups) and needs ``full_data_export``.
    """

    actor: str
    scope: Scope = "agent"
    full_data: bool = False

    def may_see(self, sensitivity: str) -> bool:
        return self.scope == "owner" or SENSITIVITY_ORDER[sensitivity] == 0

    @property
    def rank(self) -> int:
        """Highest sensitivity rank this caller may see or write at (agent 0, owner 2). Every
        commit made for a caller passes it as ``max_rank`` so a replayed response is bound to
        the rank that created it."""
        return max(SENSITIVITY_ORDER.values()) if self.scope == "owner" else 0


def current_record(conn: sqlite3.Connection, record_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT r.id, r.kind, r.current_rev, r.sensitivity, r.lifecycle, v.recorded_at, "
        "v.occurred_at, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id AND "
        "v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.id = ?",
        (record_id,),
    ).fetchone()
    return _envelope(row) if row else None


def _envelope(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "rev": row["current_rev"] if "current_rev" in row.keys() else row["rev"],
        "recorded_at": row["recorded_at"],
        "occurred_at": row["occurred_at"],
        "sensitivity": row["sensitivity"],
        "lifecycle": row["lifecycle"],
        # A purged revision keeps no content; say so instead of returning an empty body.
        "body": (
            normalise_stored_body(row["kind"], loads_internal(row["encoded"]))
            if row["encoded"] is not None
            else {"purged": True}
        ),
    }


def project_seq(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT project_seq FROM project_state").fetchone()[0])


def project_seq_of(store: Any) -> int:
    conn = store.connect(readonly=True)
    try:
        return project_seq(conn)
    finally:
        conn.close()


def record_as_of(conn: sqlite3.Connection, record_id: str, at_seq: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT r.id, r.kind, v.rev, v.sensitivity, v.lifecycle, v.recorded_at, v.occurred_at, "
        "b.encoded FROM records r JOIN revisions v ON v.record_id = r.id JOIN bodies b ON "
        "b.id = v.body_id WHERE r.id = ? AND v.commit_seq <= ? ORDER BY v.rev DESC LIMIT 1",
        (record_id, at_seq),
    ).fetchone()
    return _envelope(row) if row else None


def _media_verified(store_dir: Path, key: str, sha256: str, byte_length: int) -> bool:
    """Digest-verify a media blob, memoised in a disposable cache keyed by size/mtime.

    Reads may update this cache (spec/02: queries are pure except disposable cache
    updates). Deleting the cache only costs re-hashing.
    """
    path = store_dir / "media" / key
    try:
        info = path.stat()
    except OSError:
        return False
    if info.st_size != byte_length:
        return False
    cache_path = store_dir / "cache" / "media-verify.json"
    stamp = f"{info.st_size}:{info.st_mtime_ns}"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    if cache.get(key) == {"stamp": stamp, "sha256": sha256}:
        return True
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return False
    if digest.hexdigest() != sha256:
        return False
    cache[key] = {"stamp": stamp, "sha256": sha256}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError:
        pass
    return True


def computed_availability(
    conn: sqlite3.Connection, store_dir: Path, body: dict[str, Any], version_id: str
) -> str:
    """Current availability of a version's bytes; never trusts the stored field.

    Retained bytes must be present and match their digest (payloads are hashed in the
    database; media through the verification cache), and the latest saved observation
    must not have found them corrupt. Reference-only versions are ``external`` unless
    the latest saved observation found the origin unreachable.
    """
    latest = conn.execute(
        "SELECT origin_reachable, retained_digest_ok FROM source_checks WHERE version_id = ? "
        "ORDER BY commit_seq DESC, id DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    if body["storage"] != "external" and latest is not None and latest["retained_digest_ok"] == 0:
        # The most recent saved full-digest check found the retained bytes wrong; a later
        # passing check (after repair) supersedes it.
        return "missing"
    if body["storage"] == "body":
        row = conn.execute(
            "SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)
        ).fetchone()
        ok = row is not None and hashlib.sha256(bytes(row["data"])).hexdigest() == body["sha256"]
        return "retained" if ok else "missing"
    if body["storage"] == "media":
        ok = _media_verified(
            store_dir, str(body["storage_key"]), body["sha256"], int(body["byte_length"])
        )
        return "retained" if ok else "missing"
    if latest is not None and not latest["origin_reachable"]:
        return "missing"
    return "external"


def present(conn: sqlite3.Connection, store_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Apply computed fields to a record envelope on every read path."""
    body = record["body"]
    if record["kind"] == "version" and record["lifecycle"] != "deleted" and "storage" in body:
        record["body"]["availability"] = computed_availability(
            conn, store_dir, record["body"], record["id"]
        )
    return record


def _visible(conn: sqlite3.Connection, caller: Caller, record_id: str) -> bool:
    row = conn.execute("SELECT sensitivity FROM records WHERE id = ?", (record_id,)).fetchone()
    return row is not None and caller.may_see(row["sensitivity"])


def get_record(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    record_id: str,
    at_seq: int | None = None,
) -> dict[str, Any]:
    # Current restrictions apply even to historical reads, and concealment does not
    # reveal whether a hidden record exists.
    if not _visible(conn, caller, record_id):
        raise PrometheosError("not_found", "record not found", {"id": record_id})
    record = (
        current_record(conn, record_id) if at_seq is None else record_as_of(conn, record_id, at_seq)
    )
    if record is None or not caller.may_see(record["sensitivity"]):
        raise PrometheosError("not_found", "record not found", {"id": record_id})
    return present(conn, store_dir, record)


def _encode_cursor(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(dumps_canonical(payload)).decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        value = loads_strict(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (binascii.Error, CanonicalJSONError, UnicodeEncodeError, ValueError) as exc:
        raise PrometheosError("validation_error", "invalid cursor", {}) from exc
    if not isinstance(value, dict):
        raise PrometheosError("validation_error", "invalid cursor", {})
    return value


def list_records(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    *,
    kind: str | None = None,
    include_archived: bool = False,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    at_seq: int | None = None,
) -> dict[str, Any]:
    if kind is not None and kind not in KINDS:
        raise PrometheosError("validation_error", "unknown kind", {"kind": kind})
    if not 1 <= limit <= MAX_LIMIT:
        raise PrometheosError("validation_error", f"limit must be 1..{MAX_LIMIT}", {})
    filters = {"kind": kind, "include_archived": include_archived, "scope": caller.scope}
    filter_digest = canonical_sha256(filters)
    after = ""
    if cursor is not None:
        decoded = _decode_cursor(cursor)
        if decoded.get("f") != filter_digest:
            raise PrometheosError("validation_error", "cursor belongs to different filters", {})
        snapshot = int(decoded.get("s", -1))
        if at_seq is not None and at_seq != snapshot:
            raise PrometheosError("validation_error", "cursor belongs to a different snapshot", {})
        at_seq = snapshot
        after = str(decoded.get("a", ""))
    snapshot = at_seq if at_seq is not None else project_seq(conn)
    rows = conn.execute(
        "SELECT r.id, r.sensitivity AS current_sensitivity FROM records r "
        "WHERE r.created_seq <= ? AND r.id > ? AND (? IS NULL OR r.kind = ?) ORDER BY r.id",
        (snapshot, after, kind, kind),
    ).fetchall()
    items: list[dict[str, Any]] = []
    next_cursor = None
    for row in rows:
        if not caller.may_see(row["current_sensitivity"]):
            continue
        record = record_as_of(conn, row["id"], snapshot)
        if record is None or not caller.may_see(record["sensitivity"]):
            continue
        if record["lifecycle"] != "active" and not include_archived:
            continue
        if len(items) == limit:
            next_cursor = _encode_cursor({"f": filter_digest, "s": snapshot, "a": items[-1]["id"]})
            break
        items.append(present(conn, store_dir, record))
    return {"project_seq": snapshot, "items": items, "next_cursor": next_cursor}


_ID_TOKEN = re.compile(RECORD_ID_PATTERN.strip("^$"))
HIDDEN = "[hidden record]"


def redact_hidden(conn: sqlite3.Connection, caller: Caller, value: Any) -> Any:
    """Replace IDs of records the caller may not see with a neutral placeholder.

    Applied to every agent-scope output (records, lists, context, notebook, search,
    links, next steps, agenda, reconciliation, history) so a reference never reveals
    *which* hidden record it points to or anything about it. That some reference exists
    remains visible (e.g. a claim superseded by a hidden replacement). Owner scope is
    returned unchanged.
    """
    if caller.scope == "owner":
        return value
    cache: dict[str, bool] = {}

    def visible(record_id: str) -> bool:
        if record_id not in cache:
            row = conn.execute(
                "SELECT sensitivity FROM records WHERE id = ?", (record_id,)
            ).fetchone()
            cache[record_id] = row is None or caller.may_see(row[0])
        return cache[record_id]

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            if "_" not in item:
                return item
            return _ID_TOKEN.sub(lambda m: m.group(0) if visible(m.group(0)) else HIDDEN, item)
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        return item

    return walk(value)


def history(conn: sqlite3.Connection, caller: Caller, record_id: str) -> dict[str, Any]:
    """Revision metadata and recorded transition reasons/evidence for one record."""
    if not _visible(conn, caller, record_id):
        raise PrometheosError("not_found", "record not found", {"id": record_id})
    revisions = []
    for row in conn.execute(
        "SELECT v.rev, v.commit_seq, v.recorded_at, v.occurred_at, v.sensitivity, v.lifecycle, "
        "b.encoded IS NULL AS purged FROM revisions v JOIN bodies b ON b.id = v.body_id "
        "WHERE v.record_id = ? ORDER BY v.rev",
        (record_id,),
    ):
        operation = conn.execute(
            "SELECT operation FROM audit WHERE seq = ? AND record_id = ? AND rev = ?",
            (row["commit_seq"], record_id, row["rev"]),
        ).fetchone()
        revisions.append(
            {
                "rev": row["rev"],
                "commit_seq": row["commit_seq"],
                "recorded_at": row["recorded_at"],
                "occurred_at": row["occurred_at"],
                "sensitivity": row["sensitivity"],
                "lifecycle": row["lifecycle"],
                "operation": operation[0] if operation else None,
                "content_purged": bool(row["purged"]),
            }
        )
    notes = [
        {
            "rev": r["rev"],
            "from": r["from_state"],
            "to": r["to_state"],
            "reason": r["reason"],
            "evidence": loads_internal(r["evidence"]),
            "protected_effects": loads_internal(r["protected"]),
            "commit_seq": r["commit_seq"],
        }
        for r in conn.execute(
            "SELECT * FROM transition_notes WHERE record_id = ? ORDER BY rev", (record_id,)
        )
    ]
    return {
        "id": record_id,
        "project_seq": project_seq(conn),
        "revisions": revisions,
        "transitions": notes,
    }


# ----- hub support: change feed and source text ------------------------------------------


def changes(
    conn: sqlite3.Connection, caller: Caller, after_seq: int, limit: int = 500
) -> dict[str, Any]:
    """Metadata-only revisions committed after ``after_seq`` that the caller may see."""
    allowed = ("normal",) if caller.scope != "owner" else ("normal", "restricted", "private")
    marks = ",".join("?" for _ in allowed)
    rows = conn.execute(
        "SELECT v.record_id, r.kind, v.rev, v.commit_seq FROM revisions v "  # noqa: S608
        "JOIN records r ON r.id = v.record_id "
        f"WHERE v.commit_seq > ? AND r.sensitivity IN ({marks}) "
        "ORDER BY v.commit_seq, v.op_index LIMIT ?",
        (after_seq, *allowed, limit + 1),
    ).fetchall()
    return {
        "after_seq": after_seq,
        "latest_seq": project_seq(conn),
        "changed": [
            {
                "id": row["record_id"],
                "kind": row["kind"],
                "rev": row["rev"],
                "seq": row["commit_seq"],
            }
            for row in rows[:limit]
        ],
        "truncated": len(rows) > limit,
    }


def version_text(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    version_id: str,
    max_chars: int = 200_000,
    offset: int = 0,
) -> dict[str, Any]:
    """Retained UTF-8 text of a version for display; never an excerpt that is not there.
    ``offset`` and ``max_chars`` read one page; ``next_offset`` says where the next starts."""
    record = get_record(conn, store_dir, caller, version_id)
    if record["kind"] != "version":
        raise PrometheosError("not_found", "version not found", {"id": version_id})
    body = record["body"]
    base = {
        "version_id": version_id,
        "source_id": body.get("source_id"),
        "mime": body.get("mime"),
        "availability": body.get("availability"),
        "byte_length": body.get("byte_length"),
    }
    if body.get("availability") != "retained":
        return base | {"text": None, "reason": "bytes are not retained in this store"}
    data: bytes | None = None
    if body["storage"] == "body":
        row = conn.execute(
            "SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)
        ).fetchone()
        data = bytes(row["data"]) if row else None
    elif body["storage"] == "media":
        try:
            data = (store_dir / "media" / str(body["storage_key"])).read_bytes()
        except OSError:
            data = None
    if data is None or hashlib.sha256(data).hexdigest() != body["sha256"]:
        return base | {"text": None, "reason": "retained bytes are missing or corrupt"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return base | {"text": None, "reason": "not UTF-8 text; open it in an existing tool"}
    shown = text[offset : offset + max_chars]
    end = offset + len(shown)
    return base | {
        "text": shown,
        "truncated": end < len(text),
        "reason": None,
        "total_chars": len(text),
        "offset": offset,
        "next_offset": end if end < len(text) else None,
    }


# Types the hub may hand to the browser's own viewers (spec/06 safe viewers). Never HTML or
# SVG: they can carry script. Text is always served as text/plain.
VIEWABLE = {
    "application/pdf": "application/pdf",
    # Audio plays in the browser's own element; transcripts are the text alternative.
    "audio/wav": "audio/wav",
    "audio/mpeg": "audio/mpeg",
    "audio/mp4": "audio/mp4",
    "audio/ogg": "audio/ogg",
    "audio/flac": "audio/flac",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
    "text/plain": "text/plain; charset=utf-8",
    "text/markdown": "text/plain; charset=utf-8",
    "text/x-tex": "text/plain; charset=utf-8",
    "text/csv": "text/plain; charset=utf-8",
    "application/x-tex": "text/plain; charset=utf-8",
    "application/x-latex": "text/plain; charset=utf-8",
}
MAX_VIEW_BYTES = 100 * 1024 * 1024
MAX_AUDIO_VIEW_BYTES = 1024 * 1024 * 1024
MAX_AUDIO_RANGE_BYTES = 4 * 1024 * 1024  # one playback slice; the browser asks for the next


def version_audio_range(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    version_id: str,
    first: int | None,
    last: int | None,
) -> tuple[bytes, int, str, int, int]:
    """A byte range of retained audio for seeking: ``(slice, total, media_type, first, last)``.

    A retained media file is verified once per size/mtime (the disposable verification cache)
    and then only the requested slice is read, so scrubbing through a long recording does not
    re-read and re-hash the whole file on every request (review cp6-a L4). ``first``/``last``
    follow HTTP ranges: ``(n, None)`` is open ended and ``(None, n)`` the last ``n`` bytes."""
    record = get_record(conn, store_dir, caller, version_id)
    body = record["body"]
    media_type = VIEWABLE.get(str(body.get("mime", "")).split(";")[0].strip().lower())
    if record["kind"] != "version" or media_type is None or not media_type.startswith("audio/"):
        raise PrometheosError("unsupported", "this version is not playable audio", {})
    if body.get("availability") != "retained":
        raise PrometheosError("source_unavailable", "bytes are not retained in this store", {})
    total = int(body["byte_length"])
    if total > MAX_AUDIO_VIEW_BYTES:
        raise PrometheosError("budget_exceeded", "file is too large to play", {})
    if first is None:
        first, last = max(0, total - int(last or 0)), total - 1
    elif last is None or last >= total:
        last = total - 1
    if total == 0 or first >= total or first > last:
        raise PrometheosError("validation_error", "range not satisfiable", {"total": total})
    # "bytes=0-" and "bytes=N-" (every seek) are open ended: answer with a bounded 206 and the
    # true Content-Range; the browser requests the following piece (review cp6-a R2-L1).
    last = min(last, first + MAX_AUDIO_RANGE_BYTES - 1)
    if body["storage"] == "media":
        if not _media_verified(store_dir, str(body["storage_key"]), body["sha256"], total):
            raise PrometheosError("source_unavailable", "retained bytes are missing or corrupt", {})
        with open(store_dir / "media" / str(body["storage_key"]), "rb") as handle:
            handle.seek(first)
            data = handle.read(last - first + 1)
    else:
        whole, _ = version_content(conn, store_dir, caller, version_id)
        data = whole[first : last + 1]
    return data, total, media_type, first, last


def version_content(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, version_id: str
) -> tuple[bytes, str]:
    """Verified retained bytes and a safe media type for an inline preview."""
    record = get_record(conn, store_dir, caller, version_id)
    if record["kind"] != "version":
        raise PrometheosError("not_found", "version not found", {"id": version_id})
    body = record["body"]
    media_type = VIEWABLE.get(str(body.get("mime", "")).split(";")[0].strip().lower())
    if media_type is None:
        raise PrometheosError(
            "unsupported",
            "this file type is not previewed in the hub; open it in an existing tool",
            {"mime": body.get("mime")},
        )
    if body.get("availability") != "retained":
        raise PrometheosError("source_unavailable", "bytes are not retained in this store", {})
    limit = MAX_AUDIO_VIEW_BYTES if media_type.startswith("audio/") else MAX_VIEW_BYTES
    if int(body.get("byte_length", 0)) > limit:
        raise PrometheosError("budget_exceeded", "file is too large to preview", {})
    data: bytes | None = None
    if body["storage"] == "body":
        row = conn.execute(
            "SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)
        ).fetchone()
        data = bytes(row["data"]) if row else None
    elif body["storage"] == "media":
        try:
            data = (store_dir / "media" / str(body["storage_key"])).read_bytes()
        except OSError:
            data = None
    if data is None or hashlib.sha256(data).hexdigest() != body["sha256"]:
        raise PrometheosError("source_unavailable", "retained bytes are missing or corrupt", {})
    return data, media_type
