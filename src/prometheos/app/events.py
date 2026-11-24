"""Research events: detectors, the agent-visible queue, listing and acknowledgement (A05,
CP18.2-CP18.3; decision 0035).

- **Detectors** (:func:`detect`) find conditions that no single operation reports (a deadline
  coming closer, a result waiting, a backup gap, an unavailable source) and emit one event per
  condition, recording it in ``detector_state`` in the same transaction, so a condition fires
  once across restarts and concurrent passes. They run when ``mcp serve`` or ``hub serve``
  starts and in every jobs tick, never inside a read, never on a timer.
- **The queue** (:func:`queue`) and :func:`listing` are pure reads: they never expire, recover,
  claim, lease or change anything (C18-QUEUE).
- **Acknowledgement** (:func:`acknowledge`) is the owner's; an agent requests it as an approval.
  Nothing here starts, schedules or claims work, and no event executes an operation.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from prometheos.app import outbox
from prometheos.app.reads import Caller, current_record
from prometheos.domain.events import EVENT_TYPES, Event
from prometheos.domain.ids import UUID4_PATTERN, kind_of
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.store import capabilities, db
from prometheos.store.project import ProjectStore

DEADLINE_THRESHOLDS: tuple[int, ...] = (14, 7, 2)
OPEN_ACTION_STATES = ("proposed", "ready", "in_progress", "blocked")
MAX_ACKNOWLEDGE = 50
EVENT_STATES = ("open", "acknowledged", "all")
QUEUE_LIMIT = 100
QUEUE_PROPOSED = ("fold", "list")
FOLDED_EVENT_TYPES = ("work_item.created", "work_item.changed")
GROUP_EXAMPLES = 3

SYSTEM = Caller(actor="event-detector", scope="owner")
"""What a detector scans with: every record. Visibility is applied when events are read, from
the subject's current sensitivity, so a detector never decides who may see an event."""

_RECORD_SUBJECTS = frozenset({"action", "execution", "version", "correction", "source", "claim"})


def _invalid(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


def _ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


# ----- rows ----------------------------------------------------------------------------------


def _event(row: sqlite3.Row) -> Any:
    """The validated event model of one outbox row (its catalogue type, closed data)."""
    value = {
        "event_id": row["event_id"],
        "type": row["type"],
        "subject_ref": json.loads(row["subject_ref"]),
        "occurred_at": row["occurred_at"],
        "project_seq": row["project_seq"],
        "data": json.loads(row["data"]),
        "acknowledged_at": row["acknowledged_at"],
        "acknowledged_by": row["acknowledged_by"],
        "acknowledged_note": row["acknowledged_note"],
    }
    return Event.model_validate(value, strict=True).root


def _subject_rank(conn: sqlite3.Connection, subject: dict[str, str]) -> int | None:
    """The rank a caller needs to see this subject now; ``None`` when it is gone."""
    kind, subject_id = subject["kind"], subject["id"]
    if kind in _RECORD_SUBJECTS:
        row = conn.execute(
            "SELECT sensitivity, lifecycle FROM records WHERE id = ?", (subject_id,)
        ).fetchone()
        if row is None or row["lifecycle"] == "deleted":
            return None
        return SENSITIVITY_ORDER[row["sensitivity"]]
    if kind == "approval_request":
        row = conn.execute(
            "SELECT caller_rank FROM approval_requests WHERE id = ?", (subject_id,)
        ).fetchone()
        return None if row is None else int(row["caller_rank"])
    if kind == "operation_run":
        row = conn.execute(
            "SELECT caller_rank FROM operation_runs WHERE id = ?", (subject_id,)
        ).fetchone()
        return None if row is None else int(row["caller_rank"])
    if kind == "project":
        return 0
    return None  # pragma: no cover - the catalogue admits no other subject kind


def visible(conn: sqlite3.Connection, caller: Caller, event: Any) -> bool:
    rank = _subject_rank(conn, event.subject_ref.model_dump())
    return rank is not None and rank <= caller.rank


def reason(event: Any) -> str:
    """One plain sentence from the event's type and data only (never record text)."""
    data = event.data
    match event.type:
        case "work_item.created":
            return f"A work item was added (status {data.status.replace('_', ' ')})."
        case "work_item.changed":
            fields = ", ".join(f.replace("_", " ") for f in data.changed) or "no field"
            return f"A work item changed: {fields} (status {data.status.replace('_', ' ')})."
        case "deadline.approaching":
            return f"A work item's deadline is within {data.days_before} days."
        case "approval.requested":
            return (
                f"An approval was requested for {data.operation}; the researcher decides it in "
                "Approvals."
            )
        case "approval.decided":
            return f"The researcher {data.decision} an approval request."
        case "result.awaiting_interpretation":
            if data.state == "completed":
                return "A completed execution awaits interpretation."
            return "An execution receipt without a registered execution awaits interpretation."
        case "correction.flagged_dependents":
            return (
                f"An applied correction flagged {_plural(data.total, 'dependent record')} "
                "for review."
            )
        case "backup.coverage_gap":
            return (
                f"{_plural(data.uncovered_versions, 'retained source version')} "
                f"{'has' if data.uncovered_versions == 1 else 'have'} no verified backup."
            )
        case "source.unavailable":
            return (
                f"{_plural(data.versions, 'version')} of a source "
                f"{'is' if data.versions == 1 else 'are'} unavailable."
            )
        case "run.outcome_unknown":
            return (
                f"An external call made by {data.operation} (stage {data.stage}) has an unknown "
                "outcome; it is not repeated."
            )
        case "claim.needs_review":
            cause = data.cause.replace("_", " ")
            return f"A claim's derived status is now {data.status} ({cause}); do not quote it."
    raise AssertionError(event.type)  # pragma: no cover - every catalogue type is handled


_TITLED = frozenset({"action", "source", "execution", "claim"})


def subject_title(conn: sqlite3.Connection, event: Any) -> str | None:
    """The subject's current title, joined when the event is read (never stored in the event):
    researcher text, shown as data like a work item's title. Callers check visibility first."""
    if event.subject_ref.kind not in _TITLED:
        return None
    record = current_record(conn, event.subject_ref.id)
    if record is None or record["body"].get("purged"):
        return None
    title = record["body"].get("title") or record["body"].get("statement")
    return str(title)[:200] if title else None


def view(event: Any, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    document: dict[str, Any] = event.model_dump(mode="json")
    document["reason"] = reason(event)
    if conn is not None:
        document["subject_title"] = subject_title(conn, event)
    return document


# ----- detector state ------------------------------------------------------------------------


def _state(conn: sqlite3.Connection, detector: str, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT state FROM detector_state WHERE detector = ? AND condition_key = ?",
        (detector, key),
    ).fetchone()
    return None if row is None else dict(json.loads(row["state"]))


def _keys(conn: sqlite3.Connection, detector: str) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT condition_key FROM detector_state WHERE detector = ?", (detector,)
        )
    }


def _put(
    conn: sqlite3.Connection,
    detector: str,
    key: str,
    state: dict[str, Any],
    event_id: str | None,
    at: str,
) -> None:
    conn.execute(
        "INSERT INTO detector_state (detector, condition_key, state, event_id, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT (detector, condition_key) DO UPDATE SET "
        "state = excluded.state, event_id = excluded.event_id, updated_at = excluded.updated_at",
        (detector, key, json.dumps(state, sort_keys=True), event_id, at),
    )


def _clear(conn: sqlite3.Connection, detector: str, key: str) -> None:
    conn.execute(
        "DELETE FROM detector_state WHERE detector = ? AND condition_key = ?", (detector, key)
    )


# ----- detectors -----------------------------------------------------------------------------


class _Pass:
    def __init__(self, conn: sqlite3.Connection, store_dir: Path, moment: datetime) -> None:
        self.conn = conn
        self.store_dir = store_dir
        self.moment = moment
        self.at = _ts(moment)
        self.seq = outbox.current_seq(conn)
        self.emitted: list[Any] = []

    def emit(self, event_type: str, subject: dict[str, str], data: dict[str, Any]) -> str:
        event = outbox.emit(
            self.conn, event_type, subject, data, occurred_at=self.at, project_seq=self.seq
        )
        self.emitted.append(event)
        return str(event.event_id)

    def deadlines(self) -> None:
        """Each open work item with a future deadline: every threshold reached is recorded
        once; only the tightest newly reached one emits (an item first seen 1 day out emits the
        2-day notice, not three). A moved deadline is a new condition."""
        rows = self.conn.execute(
            "SELECT id FROM records WHERE kind = 'action' AND lifecycle = 'active' ORDER BY id"
        ).fetchall()
        for row in rows:
            record = current_record(self.conn, row["id"])
            if record is None or record["body"].get("purged"):
                continue
            body = record["body"]
            if body["status"] not in OPEN_ACTION_STATES or body.get("due_at") is None:
                continue
            due = _parse(body["due_at"])
            left = due - self.moment
            if left <= timedelta(0):
                continue  # passed: Now and the queue show it as overdue
            reached = [t for t in DEADLINE_THRESHOLDS if left <= timedelta(days=t)]
            if not reached:
                continue
            key = f"{record['id']}|{body['due_at']}"
            new = [t for t in reached if _state(self.conn, "deadline", f"{key}|{t}") is None]
            if not new:
                continue
            tightest = min(reached)
            event_id = None
            if tightest in new:
                event_id = self.emit(
                    "deadline.approaching",
                    {"kind": "action", "id": record["id"]},
                    {"due_at": body["due_at"], "days_before": tightest},
                )
            for threshold in new:
                fired = threshold == tightest and event_id is not None
                _put(
                    self.conn,
                    "deadline",
                    f"{key}|{threshold}",
                    {"due_at": body["due_at"], "threshold": threshold, "fired": fired},
                    event_id if fired else None,
                    self.at,
                )

    def awaiting(self) -> None:
        """Once per queue entry; an entry that leaves the queue clears its state."""
        from prometheos.app import continuity

        current = {
            i["target_id"]: i
            for i in continuity.awaiting_queue(self.conn, SYSTEM)
            if kind_of(i["target_id"]) in ("execution", "version")
        }
        known = _keys(self.conn, "awaiting")
        for target_id in current:
            if target_id in known:
                continue
            kind = kind_of(target_id)
            event_id = self.emit(
                "result.awaiting_interpretation",
                {"kind": kind, "id": target_id},
                {"state": "completed" if kind == "execution" else "unregistered_receipt"},
            )
            _put(self.conn, "awaiting", target_id, {"kind": kind}, event_id, self.at)
        for gone in sorted(known - set(current)):
            _clear(self.conn, "awaiting", gone)

    def claims(self, current: dict[str, dict[str, str]]) -> None:
        """Once per entry into stale or conflicting (or a move between them); a claim that
        leaves both clears its state, so a later entry is noticed again (decision 0108)."""
        known = {k: _state(self.conn, "claim_review", k) for k in _keys(self.conn, "claim_review")}
        for claim_id, found in sorted(current.items()):
            last = known.get(claim_id)
            if last is not None and last.get("status") == found["status"]:
                continue
            event_id = self.emit("claim.needs_review", {"kind": "claim", "id": claim_id}, found)
            _put(
                self.conn, "claim_review", claim_id, {"status": found["status"]}, event_id, self.at
            )
        for gone in sorted(set(known) - set(current)):
            _clear(self.conn, "claim_review", gone)

    def coverage(self, items: list[dict[str, Any]]) -> None:
        """Backup gap (retained versions with no verified backup; reference-only versions are
        never backed up by design and do not count) and unavailable sources, from one coverage
        scan made before the write transaction (review s2-a F5)."""
        uncovered = sum(
            1 for i in items if i["availability"] == "retained" and not i["backup"]["covered"]
        )
        project_id = str(self.conn.execute("SELECT project_id FROM project_state").fetchone()[0])
        last = _state(self.conn, "backup_gap", "project")
        if uncovered == 0:
            if last is not None:
                _clear(self.conn, "backup_gap", "project")
        elif last is None or uncovered > int(last["count"]):
            event_id = self.emit(
                "backup.coverage_gap",
                {"kind": "project", "id": project_id},
                {"uncovered_versions": uncovered},
            )
            _put(self.conn, "backup_gap", "project", {"count": uncovered}, event_id, self.at)
        unavailable: dict[str, int] = {}
        for item in items:
            if item["availability"] == "unavailable":
                unavailable[item["source_id"]] = unavailable.get(item["source_id"], 0) + 1
        known = _keys(self.conn, "source_unavailable")
        for source_id, count in sorted(unavailable.items()):
            if source_id in known:
                continue
            event_id = self.emit(
                "source.unavailable",
                {"kind": "source", "id": source_id},
                {"availability": "unavailable", "versions": count},
            )
            _put(self.conn, "source_unavailable", source_id, {"versions": count}, event_id, self.at)
        for gone in sorted(known - set(unavailable)):
            _clear(self.conn, "source_unavailable", gone)


def _claims_needing_review(conn: sqlite3.Connection, store_dir: Path) -> dict[str, dict[str, str]]:
    """Claim -> its derived status and first cause, for the claims that are stale or
    conflicting now (decision 0108). Read in the snapshot: status reads files' digests."""
    from prometheos.app import claim_status

    out = {}
    for item in claim_status.statuses(conn, store_dir, SYSTEM):
        if item["status"] in ("stale", "conflicting"):
            causes = [c for c in item["causes"] if c["code"] != "supported"]
            out[str(item["claim_id"])] = {
                "status": str(item["status"]),
                "cause": str(causes[0]["code"]) if causes else "unknown",
            }
    return out


def detect(store: ProjectStore, now: datetime | None = None) -> dict[str, Any]:
    """One detector pass in one write transaction. Writes only ``events`` and
    ``detector_state``; starts, schedules and claims nothing."""
    from prometheos.app import continuity

    moment = now or datetime.now(UTC)
    # The coverage scan reads files (digests through the verification cache): it runs in a
    # read snapshot first, so the write lock is held only for the cheap scans and the writes.
    # A version added in between is seen by the next pass.
    conn = store.connect(readonly=True)
    try:
        with db.read_snapshot(conn):
            coverage = continuity.coverage_items(conn, store.directory, SYSTEM)
            claims = _claims_needing_review(conn, store.directory)
    finally:
        conn.close()
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            run = _Pass(conn, store.directory, moment)
            run.deadlines()
            run.awaiting()
            run.coverage(coverage)
            run.claims(claims)
    finally:
        conn.close()
    return {
        "checked_at": run.at,
        "emitted": [
            {"event_id": e.event_id, "type": e.type, "subject_ref": e.subject_ref.model_dump()}
            for e in run.emitted
        ],
    }


# ----- reads ---------------------------------------------------------------------------------


def _timezone(conn: sqlite3.Connection) -> str | None:
    from prometheos.app.sources import _settings

    project_id = str(conn.execute("SELECT project_id FROM project_state").fetchone()[0])
    zone = _settings(conn, project_id)[0].timezone
    return None if zone is None else str(zone)


def item_reason(body: dict[str, Any], moment: datetime) -> str:
    status = body["status"]
    head = {
        "proposed": "Suggested; waiting for the researcher's adoption",
        "ready": "Ready",
        "in_progress": "In progress",
        "blocked": "Blocked",
    }[status]
    due = body.get("due_at")
    if due is None:
        return f"{head}; no deadline."
    seconds = (_parse(due) - moment).total_seconds()
    if seconds < 0:
        days = int(-seconds // 86400)
        when = "today" if days == 0 else f"{_plural(days, 'day')} ago"
        return f"{head}; the deadline passed {when}."
    if seconds < 86400:
        return f"{head}; due within a day."
    return f"{head}; due in {_plural(int(seconds // 86400), 'day')}."


def queue(
    conn: sqlite3.Connection,
    caller: Caller,
    *,
    limit: int = QUEUE_LIMIT,
    proposed: str = "fold",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Open work items and unacknowledged events in one list, ordered by due time (entries
    without one after), then age, then causal order: work items before events, each in the
    order they were written (decision 0058). A pure read (C18-QUEUE).

    ``proposed="fold"`` (the default, decision 0078) lists suggested items that wait for the
    researcher's adoption as one group per first label, with a count and a few titles, instead of
    one entry each: they are the researcher's to adopt, not work an agent may take up, and an
    adoption plan alone files dozens. ``"list"`` lists every one as before. The counts are the
    same either way."""
    if not 1 <= limit <= 200:
        raise _invalid("limit must be 1..200")
    if proposed not in QUEUE_PROPOSED:
        raise _invalid("proposed must be fold or list")
    moment = now or datetime.now(UTC)
    entries: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT r.id, r.created_seq, v.op_index, v.recorded_at AS created_at "
        "FROM records r JOIN revisions v "
        "ON v.record_id = r.id AND v.rev = 1 WHERE r.kind = 'action' AND r.lifecycle = 'active'"
    ).fetchall()
    for row in rows:
        record = current_record(conn, row["id"])
        if record is None or not caller.may_see(record["sensitivity"]):
            continue
        body = record["body"]
        if body.get("purged") or body["status"] not in OPEN_ACTION_STATES:
            continue
        description = body.get("description") or ""
        entries.append(
            {
                "entry": "work_item",
                "ref": record["id"],
                "rev": record["rev"],
                "title": body["title"],
                "status": body["status"],
                "due_at": body.get("due_at"),
                "labels": list(body.get("labels") or []),
                "description_chars": len(description),
                "blocker": (body.get("blocker") or None) and str(body["blocker"])[:200],
                "since": row["created_at"],
                "reason": item_reason(body, moment),
            }
        )
    work_items = len(entries)
    # Ties of due time and age are broken causally (decision 0058): a work item by its
    # creation's commit and its place in that commit (items created together share both the
    # commit and the age), an event by its insertion order; never by a random ID.
    position: dict[str, tuple[int, int, int]] = {
        row["id"]: (0, row["created_seq"], row["op_index"]) for row in rows
    }
    for event_row in conn.execute(
        "SELECT * FROM events WHERE acknowledged_at IS NULL ORDER BY occurred_at, seq"
    ):
        event = _event(event_row)
        if not visible(conn, caller, event):
            continue
        position[event.event_id] = (1, event_row["seq"], 0)
        data = event.data.model_dump(mode="json")
        entries.append(
            {
                "entry": "event",
                "ref": event.event_id,
                "type": event.type,
                "subject_ref": event.subject_ref.model_dump(),
                "subject_title": subject_title(conn, event),
                "data": data,
                "due_at": data.get("due_at"),
                "since": event.occurred_at,
                "reason": reason(event),
            }
        )

    def order(entry: dict[str, Any]) -> tuple[int, str, str, tuple[int, int, int]]:
        due = entry["due_at"]
        return (0 if due else 1, due or "", entry["since"], position[entry["ref"]])

    entries.sort(key=order)
    event_count = len(entries) - work_items
    suggested = [e for e in entries if e["entry"] == "work_item" and e["status"] == "proposed"]
    groups: list[dict[str, Any]] = []
    if proposed == "fold":
        label_of = {e["ref"]: e["labels"][0] if e["labels"] else None for e in suggested}
        # "A work item was added/changed" about a folded suggestion says what its group says;
        # it folds with it. Any other event (a deadline, say) stays listed.
        echoes: dict[str | None, int] = {}

        def folds(e: dict[str, Any]) -> bool:
            if e["entry"] == "work_item":
                return e["ref"] in label_of
            subject = e["subject_ref"]["id"]
            if e["type"] in FOLDED_EVENT_TYPES and subject in label_of:
                echoes[label_of[subject]] = echoes.get(label_of[subject], 0) + 1
                return True
            return False

        entries = [e for e in entries if not folds(e)]
        by_label: dict[str | None, list[dict[str, Any]]] = {}
        for e in suggested:  # already in queue order, so a group's examples come first
            by_label.setdefault(label_of[e["ref"]], []).append(e)
        for label, members in by_label.items():
            groups.append(
                {
                    "label": label,
                    "count": len(members),
                    "events": echoes.get(label, 0),
                    "oldest_since": min(m["since"] for m in members),
                    "next_due_at": min((m["due_at"] for m in members if m["due_at"]), default=None),
                    "examples": [m["title"] for m in members[:GROUP_EXAMPLES]],
                }
            )
        groups.sort(key=lambda g: (g["next_due_at"] is None, g["next_due_at"] or "", -g["count"]))
    return {
        "generation": "deterministic",
        "read_only": True,
        "generated_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone": _timezone(conn),
        "counts": {
            "work_items": work_items,
            "proposed": len(suggested),
            "events": event_count,
        },
        "proposed": proposed,
        "proposed_groups": groups,
        "entries": entries[:limit],
        "truncated": len(entries) > limit,
    }


_EVENT_ID_RE = rf"^evt_{UUID4_PATTERN}$"


def _check_event_id(event_id: str) -> None:
    import re

    if not re.fullmatch(_EVENT_ID_RE, event_id):
        raise _invalid("malformed event id", event_id=event_id[:80])


def listing(
    conn: sqlite3.Connection,
    caller: Caller,
    *,
    event_type: str | None = None,
    subject_id: str | None = None,
    state: str = "open",
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Events by type, subject and acknowledgement state, oldest first, with a cursor
    (``<occurred_at>|<event_id>``). A pure read.

    Oldest first is (occurred_at, insertion order): events of one second keep the order they
    were written in (decision 0058). The cursor names the last event returned; the next page
    starts after that event's position. If that event no longer exists (a purge), the page
    starts at the beginning of its second: an event may be listed again, never skipped."""
    if event_type is not None and event_type not in EVENT_TYPES:
        raise _invalid("unknown event type", type=event_type[:80], known=list(EVENT_TYPES))
    if state not in EVENT_STATES:
        raise _invalid("state must be open, acknowledged or all")
    if not 1 <= limit <= 200:
        raise _invalid("limit must be 1..200")
    clauses: list[str] = []
    params: list[Any] = []
    if event_type is not None:
        clauses.append("type = ?")
        params.append(event_type)
    if subject_id is not None:
        clauses.append("json_extract(subject_ref, '$.id') = ?")
        params.append(subject_id)
    if state == "open":
        clauses.append("acknowledged_at IS NULL")
    elif state == "acknowledged":
        clauses.append("acknowledged_at IS NOT NULL")
    if cursor is not None:
        at, _, after = cursor.partition("|")
        _check_event_id(after)
        clauses.append(
            "(occurred_at > ? OR (occurred_at = ? AND seq > "
            "COALESCE((SELECT seq FROM events WHERE event_id = ? AND occurred_at = ?), 0)))"
        )
        params += [at, at, after, at]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    items: list[dict[str, Any]] = []
    next_cursor: str | None = None
    for row in conn.execute(
        f"SELECT * FROM events {where} ORDER BY occurred_at, seq",  # noqa: S608
        params,
    ):
        event = _event(row)
        if not visible(conn, caller, event):
            continue
        if len(items) == limit:
            last = items[-1]
            next_cursor = f"{last['occurred_at']}|{last['event_id']}"
            break
        items.append(view(event, conn))
    return {
        "generation": "deterministic",
        "state": state,
        "items": items,
        "next_cursor": next_cursor,
    }


# ----- acknowledgement (owner) ---------------------------------------------------------------


def check_ids(event_ids: list[str]) -> list[str]:
    if not 1 <= len(event_ids) <= MAX_ACKNOWLEDGE:
        raise _invalid(f"acknowledge 1..{MAX_ACKNOWLEDGE} events at a time")
    if len(set(event_ids)) != len(event_ids):
        raise _invalid("duplicate event id")
    for event_id in event_ids:
        _check_event_id(event_id)
    return sorted(event_ids)


def open_events(conn: sqlite3.Connection, caller: Caller, event_ids: list[str]) -> list[Any]:
    """The named events, each existing, visible to ``caller`` and still open."""
    found = []
    for event_id in check_ids(event_ids):
        row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        event = None if row is None else _event(row)
        if event is None or not visible(conn, caller, event):
            raise PrometheosError("not_found", "event not found", {"event_id": event_id})
        if event.acknowledged_at is not None:
            raise PrometheosError(
                "revision_conflict",
                "this event is already acknowledged",
                {"event_id": event_id, "acknowledged_at": event.acknowledged_at},
            )
        found.append(event)
    return found


def acknowledge(
    store: ProjectStore,
    event_ids: list[str],
    note: str | None,
    actor: str,
    now: datetime | None = None,
    *,
    grant: capabilities.HeldGrant | None = None,
    grant_digest: str | None = None,
) -> dict[str, Any]:
    """Acknowledge open events: the owner's act (the caller checked its owner act), or an
    agent's approved request, whose server-held ``grant`` is consumed in this same transaction.
    Every named event is open, or nothing changes. Only the acknowledgement fields change."""
    if note is not None and not 1 <= len(note.strip()) <= 2000:
        raise _invalid("a note is 1..2000 characters")
    at = _ts(now or datetime.now(UTC))
    owner = Caller(actor=actor, scope="owner")
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            events = open_events(conn, owner, event_ids)
            if grant is not None:
                capabilities.consume(
                    conn,
                    secret=grant,
                    project_id=ProjectStore.project_id_in(conn),
                    operation="exact_request",
                    digest=grant_digest,
                )
            for event in events:
                cur = conn.execute(
                    "UPDATE events SET acknowledged_at = ?, acknowledged_by = ?, "
                    "acknowledged_note = ? WHERE event_id = ? AND acknowledged_at IS NULL",
                    (at, actor[:100], note.strip() if note else None, event.event_id),
                )
                if cur.rowcount != 1:  # pragma: no cover - checked above in this transaction
                    raise PrometheosError("revision_conflict", "the event changed", {})
            rows = [
                conn.execute("SELECT * FROM events WHERE event_id = ?", (e.event_id,)).fetchone()
                for e in events
            ]
    finally:
        conn.close()
    return {"acknowledged": [view(_event(r)) for r in rows]}
