"""The research event outbox (A05, CP18.2; decision 0035): writing typed events.

Every function here writes inside the caller's open write transaction, so an event commits
exactly when the change it reports commits, and a rollback removes both. Writing the outbox
never advances the project sequence (events are operational state). Detectors, the queue,
listing and acknowledgement are in :mod:`prometheos.app.events`.
"""

from __future__ import annotations

import sqlite3
from typing import Any, get_args

from prometheos.domain.bodies import ActionBody
from prometheos.domain.canonical import dumps_canonical
from prometheos.domain.events import (
    CATALOGUE,
    OWN_CHANGE_NOTE,
    SELF_ACKNOWLEDGED_TYPES,
    ActionField,
)
from prometheos.domain.ids import new_operational_id
from prometheos.store.project import Txn

ACTION_FIELDS: tuple[str, ...] = get_args(ActionField)
OWNER_RANK = 2


def current_seq(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT project_seq FROM project_state").fetchone()[0])


def emit(
    conn: sqlite3.Connection,
    event_type: str,
    subject: dict[str, str],
    data: dict[str, Any],
    *,
    occurred_at: str,
    project_seq: int,
    acknowledged_by: str | None = None,
) -> Any:
    """Validate one event against its catalogue type and insert it; returns the event model.

    ``acknowledged_by`` writes it already acknowledged as the caller's own change (only the
    types in ``SELF_ACKNOWLEDGED_TYPES``)."""
    entry = CATALOGUE[event_type]
    if acknowledged_by is not None and event_type not in SELF_ACKNOWLEDGED_TYPES:
        raise ValueError(f"{event_type} is never written acknowledged")
    event = entry.model.model_validate(
        {
            "event_id": new_operational_id("evt"),
            "type": event_type,
            "subject_ref": subject,
            "occurred_at": occurred_at,
            "project_seq": project_seq,
            "data": data,
            "acknowledged_at": occurred_at if acknowledged_by else None,
            "acknowledged_by": acknowledged_by[:100] if acknowledged_by else None,
            "acknowledged_note": OWN_CHANGE_NOTE if acknowledged_by else None,
        },
        strict=True,
    )
    row = event.model_dump(mode="json")
    # The insertion order (decision 0058): the next number in the writing transaction, unique.
    (seq,) = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events").fetchone()
    conn.execute(
        "INSERT INTO events (event_id, type, subject_ref, occurred_at, project_seq, data, "
        "acknowledged_at, acknowledged_by, acknowledged_note, seq) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            row["event_id"],
            row["type"],
            dumps_canonical(row["subject_ref"]).decode("utf-8"),
            row["occurred_at"],
            row["project_seq"],
            dumps_canonical(row["data"]).decode("utf-8"),
            row["acknowledged_at"],
            row["acknowledged_by"],
            row["acknowledged_note"],
            seq,
        ),
    )
    return event


# The service actor PrometheOS's check detector writes as (A06.1, CP20.3; decision 0084).
DETECTOR_ACTOR = "prometheos-verification-detector"


def emit_txn(txn: Txn, event_type: str, subject: dict[str, str], data: dict[str, Any]) -> Any:
    """An event reported by a record change, in that change's commit (its sequence and time).
    A work-item event an owner-scope caller caused is written acknowledged as its own change
    (decision 0035, item 4). So is one the check detector caused: each planned check is shown
    under "Checks planned", not again in the inbox (decision 0084)."""
    own = event_type in SELF_ACKNOWLEDGED_TYPES and (
        txn.max_rank >= OWNER_RANK or txn.actor == DETECTOR_ACTOR
    )
    return emit(
        txn.conn,
        event_type,
        subject,
        data,
        occurred_at=txn.recorded_at,
        project_seq=txn.seq,
        acknowledged_by=txn.actor if own else None,
    )


def _action(body: dict[str, Any]) -> dict[str, Any]:
    """An action body with the A05 defaults filled, so a stored pre-A05 body compares equal."""
    dumped: dict[str, Any] = ActionBody.model_validate(body).model_dump(mode="json")
    return dumped


def work_item_created(txn: Txn, action_id: str, body: dict[str, Any]) -> Any:
    return emit_txn(
        txn,
        "work_item.created",
        {"kind": "action", "id": action_id},
        {"status": body["status"], "due_at": body.get("due_at")},
    )


def work_item_changed(
    txn: Txn, action_id: str, before: dict[str, Any], after: dict[str, Any]
) -> Any:
    old, new = _action(before), _action(after)
    changed = [name for name in ACTION_FIELDS if old.get(name) != new.get(name)]
    return emit_txn(
        txn,
        "work_item.changed",
        {"kind": "action", "id": action_id},
        {"changed": changed, "status": new["status"], "due_at": new.get("due_at")},
    )
