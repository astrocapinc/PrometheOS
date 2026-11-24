"""Typed meeting capture and deterministic reconciliation views.

Capture stores the original notes as a retained ``typed`` source version and a
meeting record in one commit; ``held_at`` is what the researcher asserts, while
the commit sequence records when PrometheOS learned it, so late entry never
rewrites what was known earlier. Extraction of items is agent (or owner) work
submitted through ``apply`` as proposals with exact line locators.
Reconciliation is a pure view: it shows each proposed item beside current
actions with transparent token-overlap candidates and never merges anything.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any

from prometheos.app.context import load_snapshot
from prometheos.app.reads import Caller
from prometheos.audio import transcribe
from prometheos.domain.bodies import MeetingBody, SourceBody, VersionBody
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.store import derived
from prometheos.store.project import ProjectStore, Txn

MAX_NOTES_BYTES = 8 * 1024 * 1024
SIMILARITY_THRESHOLD = 0.25


def capture(
    store: ProjectStore,
    *,
    title: str,
    text: str,
    held_at: str | None,
    sensitivity: str = "normal",
    actor: str,
    request_id: str | None = None,
    max_rank: int,
) -> dict[str, Any]:
    """``max_rank`` is the caller's visibility ceiling (agent 0, owner 2): a retry or replay
    from a lower rank conflicts instead of returning IDs of the owner's records."""
    if sensitivity not in SENSITIVITY_ORDER:
        raise PrometheosError("validation_error", "unknown sensitivity", {})
    data = text.encode("utf-8")
    if not data.strip() or len(data) > MAX_NOTES_BYTES:
        raise PrometheosError("validation_error", "notes must be non-empty and at most 8 MiB", {})
    sha = hashlib.sha256(data).hexdigest()
    source_id, version_id, meeting_id = new_id("source"), new_id("version"), new_id("meeting")
    rid = request_id or new_request_id()
    request_sha = canonical_sha256(
        {
            "op": "meeting_capture",
            "title": title,
            "sha256": sha,
            "held_at": held_at,
            "sensitivity": sensitivity,
        }
    )

    def handler(txn: Txn) -> None:
        source = SourceBody(
            title=f"Meeting notes: {title}",
            origin="typed",
            origin_uri=None,
            root_binding=None,
            retention="copy",
            description="Typed meeting notes",
        )
        sid = txn.insert_body(source.model_dump(mode="json"))
        txn.write_revision(
            source_id,
            "source",
            1,
            sid,
            sensitivity,
            "active",
            held_at,
            "meeting_capture",
            body=source.model_dump(mode="json"),
        )
        txn.result(source_id, 1)
        txn.conn.execute(
            "INSERT OR IGNORE INTO payloads (sha256, data, byte_length, created_seq) "
            "VALUES (?,?,?,?)",
            (sha, data, len(data), txn.seq),
        )
        txn.op_index = 1
        version = VersionBody(
            source_id=source_id,
            sha256=sha,
            byte_length=len(data),
            mime="text/plain",
            captured_at=txn.recorded_at,
            storage="body",
            storage_key=f"payload:{sha}",
            availability="retained",
            extraction="none",
            derived_from=None,
            payload_schema="text",
            transformation=None,
        )
        vid = txn.insert_body(version.model_dump(mode="json"))
        txn.write_revision(
            version_id,
            "version",
            1,
            vid,
            sensitivity,
            "active",
            held_at,
            "meeting_capture",
            body=version.model_dump(mode="json"),
        )
        derived.index_text(txn.conn, version_id, "version", "", text)
        txn.result(version_id, 1)
        txn.op_index = 2
        meeting = MeetingBody(
            title=title,
            held_at=held_at,
            participant_refs=[],
            source_version_refs=[{"id": version_id, "rev": 1}],  # type: ignore[list-item]
            summary_entry_ref=None,
            processing="captured",
        )
        mid = txn.insert_body(meeting.model_dump(mode="json"))
        txn.write_revision(
            meeting_id,
            "meeting",
            1,
            mid,
            sensitivity,
            "active",
            held_at,
            "meeting_capture",
            body=meeting.model_dump(mode="json"),
        )
        txn.result(meeting_id, 1)

    response = store.commit(
        request_id=rid,
        request_sha256=request_sha,
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=max_rank,
    )
    # On a replay the committed IDs (not this call's fresh ones) are the truth.
    committed = [r["id"] for r in response["results"]]
    return {
        "source_id": committed[0],
        "version_id": committed[1],
        "meeting_id": committed[2],
        "sha256": sha,
        "commit": response,
    }


def capture_from_transcript(
    store: ProjectStore,
    *,
    caller: Caller,
    transcript_version_id: str,
    audio_version_id: str | None,
    title: str,
    held_at: str | None,
    actor: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """A meeting whose original material is a recording and its transcript.

    The transcript version is first in ``source_version_refs`` (what the reconcile view
    cites); the audio version follows. The meeting inherits the highest sensitivity of the
    versions it names and nothing is retained twice."""
    conn = store.connect(readonly=True)
    try:
        record, _payload = transcribe.transcript_version(
            conn, store.directory, caller, transcript_version_id
        )
        refs = [{"id": transcript_version_id, "rev": record["rev"]}]
        rank = SENSITIVITY_ORDER[record["sensitivity"]]
        if audio_version_id is not None:
            audio = transcribe.audio_version(conn, store.directory, caller, audio_version_id)
            refs.append({"id": audio_version_id, "rev": audio["rev"]})
            rank = max(rank, SENSITIVITY_ORDER[audio["sensitivity"]])
    finally:
        conn.close()
    sensitivity = ("normal", "restricted", "private")[rank]
    rid = request_id or new_request_id()
    meeting_id = new_id("meeting")
    request_sha = canonical_sha256(
        {"op": "meeting_capture_transcript", "title": title, "held_at": held_at, "refs": refs}
    )

    def handler(txn: Txn) -> None:
        meeting = MeetingBody(
            title=title,
            held_at=held_at,
            participant_refs=[],
            source_version_refs=refs,  # type: ignore[arg-type]
            summary_entry_ref=None,
            processing="captured",
        ).model_dump(mode="json")
        txn.write_revision(
            meeting_id,
            "meeting",
            1,
            txn.insert_body(meeting),
            sensitivity,
            "active",
            held_at,
            "meeting_capture",
            body=meeting,
        )
        txn.result(meeting_id, 1)

    response = store.commit(
        request_id=rid,
        request_sha256=request_sha,
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=2 if caller.scope == "owner" else 0,
    )
    return {
        "meeting_id": response["results"][0]["id"],
        "sensitivity": sensitivity,
        "commit": response,
    }


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def reconcile(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, meeting_id: str
) -> dict[str, Any]:
    snap = load_snapshot(conn, store_dir, caller, None)
    meeting = snap.records.get(meeting_id)
    if meeting is None or meeting["kind"] != "meeting":
        raise PrometheosError("not_found", "meeting not found", {"id": meeting_id})
    actions = snap.of("action")
    rows = []
    for item in sorted(snap.of("item"), key=lambda r: snap.created[r["id"]]):
        body = item["body"]
        if body["meeting_id"] != meeting_id:
            continue
        mine = _tokens(body["text"])
        related = []
        for action in actions:
            theirs = _tokens(action["body"]["title"] + " " + action["body"]["reason"])
            union = mine | theirs
            score = len(mine & theirs) / len(union) if union else 0.0
            if (
                score >= SIMILARITY_THRESHOLD
                or action["body"].get("commitment_item_id") == item["id"]
            ):
                related.append(
                    {
                        "action_id": action["id"],
                        "status": action["body"]["status"],
                        "overlap": round(score, 3),
                        "linked": action["body"].get("commitment_item_id") == item["id"],
                    }
                )
        if body["status"] != "proposed":
            classification = body["status"]
        elif any(r["status"] == "done" for r in related):
            classification = "possibly_already_done"
        elif any(r["status"] in ("ready", "in_progress", "blocked") for r in related):
            classification = "possibly_overlaps_open_action"
        else:
            classification = "new"
        audio_spans = []
        for ev in body["evidence"]:
            if ev["locator"]["type"] == "audio":
                detail = transcribe.audio_evidence_detail(conn, store_dir, caller, ev)
                if detail is not None:
                    audio_spans.append(detail)
        rows.append(
            {
                "item_id": item["id"],
                "type": body["type"],
                "text": body["text"],
                "status": body["status"],
                "classification": classification,
                "owner": "unknown"
                if body["proposed_owner"] is None
                else body["proposed_owner"]["id"],
                "due": body["due_at"] or "unknown",
                "evidence": body["evidence"],
                "audio_spans": audio_spans,
                "related_actions": related,
            }
        )
    return {
        "project_seq": snap.seq,
        "meeting_id": meeting_id,
        "held_at": meeting["body"]["held_at"],
        "recorded_seq": snap.created[meeting_id],
        "items": rows,
        "note": (
            "Similarity only proposes related actions; nothing is merged or adopted automatically."
        ),
    }
