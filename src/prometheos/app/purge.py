"""Owner-authorised purge of retained content (spec/08 "Sensitivity, retention and deletion").

``plan`` computes the dependency closure of the requested records: every record
whose current or historical body references a purged record (transitively),
every version of a purged source, retained payloads and media used only by the
closure, search/reference index rows, observations and import plans that name
them. ``commit`` requires an owner capability bound to the exact plan digest,
rechecks that nothing changed since the plan, and in one transaction (with
``secure_delete``) replaces every body in the closure by a non-content
tombstone, removes payloads, index rows and plans, then deletes media files and
vacuums. The receipt carries counts only. Older backups, exports, external
files and Git history are outside automatic purge and are listed, not deleted.
Forensic erasure on SSDs/journals is not claimed.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prometheos.app import receipts
from prometheos.audio import transcribe
from prometheos.domain.canonical import canonical_sha256, dumps_canonical, loads_internal
from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError, utc_now
from prometheos.library import index as library_index
from prometheos.store import capabilities, db, derived, pending
from prometheos.store.lock import maintenance_lock
from prometheos.store.project import ProjectStore, Txn

LIMITATIONS = [
    "Backups and exports created before this purge still contain the purged content; delete or "
    "re-create them deliberately.",
    "External research files, Git history and copies held by coding agents or providers are not "
    "touched.",
    "SQLite secure_delete and VACUUM overwrite database pages, but SSD wear levelling, file system "
    "journals and the deleted rollback journal may retain physical remnants; forensic erasure is "
    "not claimed.",
    "The executed purge plan keeps the SHA-256 digests of the payloads it removed (owner-only "
    "identity metadata); for a short or guessable file a digest can confirm a guessed content.",
]


def _closure(conn: sqlite3.Connection, targets: list[str]) -> list[str]:
    closure = set(targets)
    frontier = list(targets)
    while frontier:
        current = frontier.pop()
        rows = conn.execute("SELECT kind FROM records WHERE id = ?", (current,)).fetchone()
        if rows is None:
            raise PrometheosError("not_found", "purge target does not exist", {"id": current})
        # Versions of a purged source.
        if rows["kind"] == "source":
            for v in conn.execute("SELECT id FROM records WHERE kind = 'version'"):
                if current in _all_body_text(conn, v["id"]) and v["id"] not in closure:
                    closure.add(v["id"])
                    frontier.append(v["id"])
        # A transcript version's source would otherwise outlive it with its title
        # (decision 0013 item 17); the transcript's other versions follow by body mention.
        if rows["kind"] == "version":
            source = _transcript_source(conn, current)
            if source is not None and source not in closure:
                closure.add(source)
                frontier.append(source)
        # Execution receipts naming the purged record: their artifact references live in
        # the payload, linked by persistent relations (decision 0009; review 0009 H2).
        # Only receipts follow this rule; other relations keep their own semantics.
        for rel in conn.execute(
            "SELECT DISTINCT source_id FROM relations WHERE target_id = ? AND relation = ?",
            (current, receipts.RECEIPT_RELATION),
        ):
            if rel[0] not in closure and receipts.is_receipt_record(conn, rel[0]):
                closure.add(rel[0])
                frontier.append(rel[0])
        # Anything whose body (any revision) mentions the purged record.
        for r in conn.execute("SELECT id FROM records WHERE id != ?", (current,)):
            if r["id"] not in closure and current in _all_body_text(conn, r["id"]):
                kind = conn.execute("SELECT kind FROM records WHERE id = ?", (r["id"],)).fetchone()[
                    0
                ]
                if kind == "project":
                    continue
                closure.add(r["id"])
                frontier.append(r["id"])
    return sorted(closure)


def _transcript_source(conn: sqlite3.Connection, version_id: str) -> str | None:
    """The source of a version that is a transcript (any revision), else ``None``."""
    for row in conn.execute(
        "SELECT b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
        "WHERE v.record_id = ? AND b.encoded IS NOT NULL",
        (version_id,),
    ):
        body = json.loads(row[0])
        # A library text version's source likewise (CP11 S11.1): "Text of <file>" names the PDF.
        if body.get("payload_schema") in ("transcript_v1", "pdf_pages_v1") and isinstance(
            body.get("source_id"), str
        ):
            return str(body["source_id"])
    return None


def _all_body_text(conn: sqlite3.Connection, record_id: str) -> str:
    rows = conn.execute(
        "SELECT b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id WHERE v.record_id = ?",
        (record_id,),
    ).fetchall()
    return " ".join(r[0] or "" for r in rows)


def plan(store: ProjectStore, targets: list[str]) -> dict[str, Any]:
    if not targets:
        raise PrometheosError("validation_error", "name at least one record to purge", {})
    conn = store.connect()
    try:
        with db.read_snapshot(conn):
            seq = int(conn.execute("SELECT project_seq FROM project_state").fetchone()[0])
            if store.project_id in targets:
                raise PrometheosError("policy_denied", "the project record cannot be purged", {})
            closure = _closure(conn, targets)
            payloads, media = set(), set()
            for rid in closure:
                for row in conn.execute(
                    "SELECT b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
                    "WHERE v.record_id = ? AND b.encoded IS NOT NULL",
                    (rid,),
                ):
                    body = json.loads(row[0])
                    if (
                        conn.execute("SELECT kind FROM records WHERE id = ?", (rid,)).fetchone()[0]
                        == "version"
                    ):
                        if body["storage"] == "body":
                            payloads.add(body["sha256"])
                        elif body["storage"] == "media":
                            media.add(body["storage_key"])
            # Shared bytes survive only if a version outside the closure still uses them.
            for row in conn.execute("SELECT id FROM records WHERE kind = 'version'"):
                if row["id"] in closure:
                    continue
                for enc in conn.execute(
                    "SELECT b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
                    "WHERE v.record_id = ? AND b.encoded IS NOT NULL",
                    (row["id"],),
                ):
                    body = json.loads(enc[0])
                    payloads.discard(body.get("sha256"))
                    media.discard(body.get("storage_key"))
            summary = {
                "plan_id": str(uuid.uuid4()),
                "project_id": store.project_id,
                "request_id": new_request_id(),
                "expected_seq": seq,
                "targets": sorted(targets),
                "closure": closure,
                "payloads": sorted(payloads),
                "media": sorted(media),
                "limitations": LIMITATIONS,
                "created_at": utc_now(),
            }
            summary["plan_sha256"] = canonical_sha256(summary)
    finally:
        conn.close()
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO purge_plans (plan_id, plan_sha256, plan, created_at, status) "
            "VALUES (?,?,?,?, 'open')",
            (
                summary["plan_id"],
                summary["plan_sha256"],
                dumps_canonical(summary).decode(),
                summary["created_at"],
            ),
        )
    finally:
        conn.close()
    return summary


def commit(
    store: ProjectStore,
    plan_id: str,
    capability_secret: capabilities.Secret,
    actor: str,
    *,
    before_commit: Callable[[list[str]], object] | None = None,
) -> dict[str, Any]:
    """Execute a reviewed plan.

    The project's maintenance lock is held from the first check to the last file removal, so
    no backup, restore, media import or other purge runs in between, and a purge that cannot
    have the lock is refused (``busy``) before it has done anything.

    ``before_commit(closure)`` runs only when the transaction is expected to be accepted (the
    capability verifies and the project is still at the sequence the plan was made for) and
    before anything is changed: the service uses it to ask running operations on the purged
    records to stop. A purge that is not going to commit therefore stops nothing. The residual
    (decision 0031): a commit that lands between this check and the transaction, or a database
    that stays busy for the transaction, refuses the purge after the stop was asked.

    Everything after the transaction has committed only removes files and never raises: a
    file that cannot be removed now (Windows: it is open) goes on the project's pending-deletion
    list (``store/pending.py``), the database is compacted whatever happened, and the receipt
    counts what is still pending (counts only: no names)."""
    with maintenance_lock(store.directory):
        summary, stop = _preflight(store, plan_id, capability_secret, before_commit)
        closure: list[str] = summary["closure"]
        if stop is not None:
            stop(closure)
        sessions: list[str] = []  # the recorder sessions of purged recordings (filled on commit)
        removed_events: list[str] = []  # the research events of purged subjects (decision 0035)
        response = store.commit(
            request_id=summary["request_id"],
            request_sha256=canonical_sha256(
                {"op": "purge_commit", "plan_sha256": summary["plan_sha256"]}
            ),
            expected_seq=None,
            actor=actor,
            handler=_transaction(
                store, plan_id, summary, capability_secret, sessions, removed_events
            ),
        )
        removed, media_failed = 0, list[str]()
        for key in summary["media"]:
            path = store.directory / "media" / key
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:  # held open (a decoder, a playback slice, a backup): list it
                media_failed.append(pending.media_entry(key))
        pending.add(store.directory, media_failed)
        purged = [*summary["payloads"], *(key.rpartition("/")[2] for key in summary["media"])]
        cache_pending = transcribe.clear_cache(store, purged)
        # A recorder session folder of a purged recording (``staging/rec-<id>``, which the
        # recorder removes after registering it unless a file in it was held then) goes too.
        staging_pending = sum(
            pending.remove_tree(store.directory, store.directory / "staging" / f"rec-{session}")
            for session in sessions
        )
        compacted = _compact(store)
    pending.sweep(store.directory, store)  # earlier leftovers that nothing holds any more
    limitations = list(LIMITATIONS)
    if media_failed:
        limitations.append(
            f"{len(media_failed)} retained media file(s) were in use and could not be deleted "
            "yet; they are listed for deletion and removed when released."
        )
    if cache_pending:
        limitations.append(
            f"{cache_pending} cache file(s) were in use by a running operation and could not "
            "be deleted yet; they are listed for deletion and removed when it lets go."
        )
    if staging_pending:
        limitations.append(
            f"{staging_pending} recorder staging file(s) were in use and could not be deleted "
            "yet; they are listed for deletion and removed when released."
        )
    if not compacted:
        limitations.append(
            "The database could not be compacted just now; purged content was already "
            "overwritten (secure_delete), and the next purge compacts it."
        )
    return {
        "purge_receipt": {
            "plan_sha256": summary["plan_sha256"],
            "project_seq": response["project_seq"],
            "replayed": response["replayed"],
            "records_purged": len(closure),
            "events_removed": len(removed_events),
            "payloads_removed": len(summary["payloads"]),
            "media_files_removed_now": removed,
            "media_files_pending_deletion": len(media_failed),
            "cache_files_pending_deletion": cache_pending,
            "staging_files_pending_deletion": staging_pending,
            "database_compacted": compacted,
            "limitations": limitations,
        },
        "commit": response,
    }


def _preflight(
    store: ProjectStore,
    plan_id: str,
    capability_secret: capabilities.Secret,
    before_commit: Callable[[list[str]], object] | None,
) -> tuple[dict[str, Any], Callable[[list[str]], object] | None]:
    """The stored plan, and ``before_commit`` only if the transaction is expected to accept it.
    A pre-flight only: the transaction decides (and consumes the capability's use)."""
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT * FROM purge_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise PrometheosError("not_found", "purge plan does not exist", {"plan_id": plan_id})
        summary: dict[str, Any] = loads_internal(row["plan"])
        if before_commit is None:
            return summary, None
        try:
            capabilities.verify(
                conn,
                secret=capability_secret,
                project_id=store.project_id,
                operation="purge",
                digest=summary["plan_sha256"],
            )
        except PrometheosError:
            return summary, None  # unauthorised or already used: do not touch anything
        current = int(conn.execute("SELECT project_seq FROM project_state").fetchone()[0])
        if current != summary["expected_seq"]:
            return summary, None  # the transaction will refuse (revision_conflict): stop nothing
        return summary, before_commit
    finally:
        conn.close()


def _transaction(
    store: ProjectStore,
    plan_id: str,
    summary: dict[str, Any],
    capability_secret: capabilities.Secret,
    sessions: list[str],
    removed_events: list[str] | None = None,
) -> Callable[[Txn], None]:
    """The purge's transaction; ``sessions`` receives the IDs of the recorder sessions whose
    rows it deletes (their staging folders are removed after the commit), ``removed_events``
    the IDs of the research events it deletes with their subjects."""
    closure: list[str] = summary["closure"]

    def handler(txn: Txn) -> None:
        txn.conn.execute("PRAGMA secure_delete = ON")
        capabilities.consume(
            txn.conn,
            secret=capability_secret,
            project_id=store.project_id,
            operation="purge",
            digest=summary["plan_sha256"],
        )
        if txn.seq - 1 != summary["expected_seq"]:
            raise PrometheosError(
                "revision_conflict",
                "the project changed since the purge plan; plan again",
                {"expected_seq": summary["expected_seq"], "project_seq": txn.seq - 1},
            )
        tombstone = {"purged": True, "purged_at_seq": txn.seq}
        for index, record_id in enumerate(closure):
            txn.op_index = index
            state = txn.conn.execute(
                "SELECT kind, current_rev, sensitivity FROM records WHERE id = ?", (record_id,)
            ).fetchone()
            for body_row in txn.conn.execute(
                "SELECT body_id FROM revisions WHERE record_id = ?", (record_id,)
            ).fetchall():
                txn.conn.execute(
                    "UPDATE bodies SET encoded = NULL, sha256 = '', purged_seq = ? WHERE id = ?",
                    (txn.seq, body_row[0]),
                )
            body_id = txn.insert_body(tombstone)
            txn.write_revision(
                record_id,
                state["kind"],
                state["current_rev"] + 1,
                body_id,
                state["sensitivity"],
                "deleted",
                None,
                "purge",
                body=None,
            )
            txn.result(record_id, state["current_rev"] + 1)
        marks = ",".join("?" for _ in closure)
        txn.conn.execute(
            f"DELETE FROM relations WHERE source_id IN ({marks}) OR target_id IN ({marks})",  # noqa: S608
            closure + closure,
        )
        txn.conn.execute(f"DELETE FROM transition_notes WHERE record_id IN ({marks})", closure)  # noqa: S608
        txn.conn.execute(f"DELETE FROM view_flags WHERE record_id IN ({marks})", closure)  # noqa: S608
        txn.conn.execute(f"DELETE FROM task_contexts WHERE task_id IN ({marks})", closure)  # noqa: S608
        txn.conn.execute(f"DELETE FROM source_checks WHERE version_id IN ({marks})", closure)  # noqa: S608
        _purge_job_tables(txn.conn, closure, marks)
        sessions[:] = _purge_audio_tables(txn.conn, closure, marks)
        gone = _purge_events(txn.conn, closure)
        if removed_events is not None:
            removed_events[:] = gone
        for sha in summary["payloads"]:
            txn.conn.execute("DELETE FROM payloads WHERE sha256 = ?", (sha,))
        for key in summary["media"]:
            txn.conn.execute("DELETE FROM media WHERE storage_key = ?", (key,))
        for plan_row in txn.conn.execute("SELECT plan_id, plan FROM import_plans").fetchall():
            if any(rid in plan_row["plan"] for rid in closure):
                txn.conn.execute(
                    "DELETE FROM import_plans WHERE plan_id = ?", (plan_row["plan_id"],)
                )
        if derived.search_mode(txn.conn) == "fts5":
            # FTS5 keeps deleted tokens in old index segments until they are merged;
            # rebuilding from the (already purged) content table drops them.
            txn.conn.execute("INSERT INTO search_fts (search_fts) VALUES ('rebuild')")
        # The library's page index and work map are rebuilt from what remains (CP11 S11.1):
        # purged page text leaves neither a row nor an FTS token.
        if library_index.schema14(txn.conn):
            # The owner's dedup decisions (and their notes) about purged works go too (S11.2).
            txn.conn.execute(
                f"DELETE FROM library_decisions WHERE work_a IN ({marks}) OR work_b IN ({marks})",  # noqa: S608
                closure + closure,
            )
        if library_index.installed(txn.conn):
            for plan_row in txn.conn.execute("SELECT plan_id, plan FROM library_plans").fetchall():
                if any(rid in plan_row["plan"] for rid in closure):
                    txn.conn.execute(
                        "DELETE FROM library_plans WHERE plan_id = ?", (plan_row["plan_id"],)
                    )
        library_index.rebuild(txn.conn)
        txn.conn.execute(
            "UPDATE purge_plans SET status = 'committed' WHERE plan_id = ? AND status = 'open'",
            (plan_id,),
        )
        txn.conn.execute(
            "INSERT INTO purges (plan_sha256, request_id, commit_seq, record_count, payload_count, "
            "media_count) VALUES (?,?,?,?,?,?)",
            (
                summary["plan_sha256"],
                txn.request_id,
                txn.seq,
                len(closure),
                len(summary["payloads"]),
                len(summary["media"]),
            ),
        )

    return handler


def _purge_job_tables(conn: sqlite3.Connection, closure: list[str], marks: str) -> None:
    """Job slots and inbox notifications are derived records: drop the purged jobs' rows,
    every notification naming a purged record, and slot results that mention one."""
    conn.execute(f"DELETE FROM job_slots WHERE job_id IN ({marks})", closure)  # noqa: S608
    conn.execute(f"DELETE FROM job_notifications WHERE job_id IN ({marks})", closure)  # noqa: S608
    purged = set(closure)
    for row in conn.execute(
        "SELECT id, title, summary, record_ids FROM job_notifications"
    ).fetchall():
        text = f"{row['title']} {row['summary']} {row['record_ids']}"
        if any(rid in text for rid in purged):
            conn.execute("DELETE FROM job_notifications WHERE id = ?", (row["id"],))
    for row in conn.execute(
        "SELECT job_id, due_utc, result, reason FROM job_slots WHERE result IS NOT NULL "
        "OR reason IS NOT NULL"
    ).fetchall():
        text = f"{row['result'] or ''} {row['reason'] or ''}"
        if any(rid in text for rid in purged):
            conn.execute(
                "UPDATE job_slots SET result = NULL, reason = 'details removed by purge' "
                "WHERE job_id = ? AND due_utc = ?",
                (row["job_id"], row["due_utc"]),
            )


def _purge_events(conn: sqlite3.Connection, closure: list[str]) -> list[str]:
    """Research events (decision 0035) follow the record purge rule: the events whose subject
    is a purged record, or an operation run the purge removed, go in this same transaction with
    their detector state. Event data holds no record text, so no other event names content."""
    purged = set(closure)
    gone = [
        str(row["event_id"])
        for row in conn.execute(
            "SELECT event_id, subject_ref FROM events WHERE "
            "json_extract(subject_ref, '$.id') IN (SELECT value FROM json_each(?)) OR "
            "(json_extract(subject_ref, '$.kind') = 'operation_run' AND "
            "json_extract(subject_ref, '$.id') NOT IN (SELECT id FROM operation_runs))",
            (json.dumps(sorted(purged)),),
        )
    ]
    for event_id in gone:
        conn.execute("DELETE FROM detector_state WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
    for row in conn.execute("SELECT detector, condition_key FROM detector_state").fetchall():
        if row["condition_key"].split("|", 1)[0] in purged:
            conn.execute(
                "DELETE FROM detector_state WHERE detector = ? AND condition_key = ?",
                (row["detector"], row["condition_key"]),
            )
    return gone


def _purge_audio_tables(conn: sqlite3.Connection, closure: list[str], marks: str) -> list[str]:
    """Transcription chunks hold transcript text, recording sessions hold titles, provider
    call rows name input records: all go with the records they derive from. Operation runs
    (decision 0017) whose parameters, artifacts, stage outputs, results or logs name a purged
    record go too, with their stages, logs and chunks. Returns the deleted sessions' IDs."""
    purged_ids = set(closure)
    for row in conn.execute(
        "SELECT id, params, artifacts, error, limitations FROM operation_runs"
    ).fetchall():
        texts = [str(row[k] or "") for k in ("params", "artifacts", "error", "limitations")]
        texts += [
            str(r[0])
            for r in conn.execute(
                "SELECT output FROM operation_run_stages WHERE run_id = ?", (row["id"],)
            )
        ]
        texts += [
            str(r[0])
            for r in conn.execute(
                "SELECT message FROM operation_run_logs WHERE run_id = ?", (row["id"],)
            )
        ]
        blob = "\n".join(texts)
        if any(rid in blob for rid in purged_ids):
            for table in ("transcription_chunks", "operation_run_stages", "operation_run_logs"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (row["id"],))  # noqa: S608
            conn.execute("DELETE FROM operation_runs WHERE id = ?", (row["id"],))
    sessions = [
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM recording_sessions WHERE version_id IN ({marks})",  # noqa: S608
            closure,
        )
    ]
    for session_id in sessions:
        conn.execute("DELETE FROM recording_segments WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM recording_sessions WHERE id = ?", (session_id,))
    purged = set(closure)
    for row in conn.execute("SELECT id, input_refs, task_id FROM provider_calls").fetchall():
        if any(rid in row["input_refs"] for rid in purged) or row["task_id"] in purged:
            conn.execute(
                "UPDATE provider_calls SET input_refs = '[]', task_id = NULL, "
                "error = NULL WHERE id = ?",
                (row["id"],),
            )
    return [str(session_id) for session_id in sessions]


def _compact(store: ProjectStore) -> bool:
    """VACUUM after the purge (secure_delete already overwrote the deleted pages). A database
    that is busy just now is not a failed purge: the receipt says so."""
    try:
        vacuum = store.connect()
    except Exception:  # busy or unreadable just now: the receipt says so
        return False
    try:
        vacuum.execute("PRAGMA secure_delete = ON")
        vacuum.execute("VACUUM")
        return True
    except sqlite3.Error:
        return False
    finally:
        vacuum.close()


__all__ = ["Path", "commit", "plan"]
