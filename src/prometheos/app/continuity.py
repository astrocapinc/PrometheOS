"""Research continuity (A02, CP10.4; spec/05 "Research continuity"; decision 0018).

Four things that let a researcher resume after a closed chat without a prose handoff:

- observe-only execution registration (``register``/``report``): the coding agent owns the
  run, PrometheOS records what was *reported* and never starts, stops, queues or restarts
  anything. Silence becomes ``status_unknown`` at read time, never failed or completed;
- the awaiting-interpretation queue, fully derived from citations (nothing is stored): a
  completed execution, or an execution receipt that no registered execution holds, waits
  until an assessment or a reviewed entry cites it; a dismissal is such an entry with a reason;
- artifact availability and backup coverage per source version, from the retained bytes, a
  live check of reference-only origins and the verified-backup inventory (reference-only bytes
  are never reported as backed up);
- ``now_view``: what changed, what is due, what is running, what awaits interpretation and
  which approvals wait for the owner, each with its reason.

Reads write nothing (the approval listing only marks expired requests, as elsewhere).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from prometheos.app import apply as apply_mod
from prometheos.app import results
from prometheos.app.context import Snapshot, load_snapshot
from prometheos.app.reads import Caller, computed_availability, current_record
from prometheos.domain.canonical import canonical_sha256, dumps_canonical, loads_internal
from prometheos.domain.ids import derived_request_id, is_record_id, kind_of, new_id
from prometheos.domain.models import (
    CreateOp,
    PrometheosError,
    ReviseOp,
)
from prometheos.sources.paths import is_link_or_reparse, resolve_in_root
from prometheos.store import operational
from prometheos.store.project import ProjectStore, Txn

SILENCE_AFTER_HOURS = 24
"""A run reported running (or registered) that has been silent this long reads as
``status_unknown``. A read-time rule: nothing runs in the background (decision 0018)."""
DUE_WITHIN_DAYS = 7
NOW_LIMIT = 50
FINAL_STATES = ("reported_completed", "reported_failed", "reported_cancelled")
OPEN_STATES = ("registered", "reported_running", "status_unknown")
REPORTABLE = ("reported_running", "reported_completed", "reported_failed", "reported_cancelled")
CHANGED_KINDS = frozenset(
    {
        "entry",
        "claim",
        "assessment",
        "correction",
        "item",
        "action",
        "meeting",
        "source",
        "paper",
        "anchor",
        "execution",
    }
)
# Derived from the shared run state model, so a state CP10.3 adds is included unless terminal.
RUN_OPEN_STATES = tuple(
    state for state in operational.RUN_STATES if state not in operational.TERMINAL_RUN_STATES
)
CLOCK_SKEW = timedelta(minutes=5)
"""A report may be dated at most this far ahead of the moment it is recorded."""


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _clip(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _invalid(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


# ----- execution registration (observe-only) ----------------------------------------------


def register(
    store: ProjectStore,
    *,
    title: str,
    host_label: str,
    reporter: str,
    command_summary: str | None,
    working_root_alias: str | None,
    external_ref: str | None,
    expected_outputs: list[str],
    sensitivity: str,
    request_id: str,
    actor: str,
    max_rank: int,
) -> dict[str, Any]:
    """Record that an external run exists (state ``registered``). Nothing is started: this
    function writes one record and calls no process, scheduler or network API."""
    params = {
        "title": title,
        "host_label": host_label,
        "reporter": reporter,
        "command_summary": command_summary,
        "working_root_alias": working_root_alias,
        "external_ref": external_ref,
        "expected_outputs": expected_outputs,
        "sensitivity": sensitivity,
    }
    body = {
        "title": title,
        "host_label": host_label,
        "reporter": reporter,
        "command_summary": command_summary,
        "working_root_alias": working_root_alias,
        "external_ref": external_ref,
        "expected_outputs": expected_outputs,
        "state": "registered",
        "last_reported_at": None,
        "receipt_refs": [],
    }
    execution_id = new_id("execution")

    def handler(txn: Txn) -> None:
        if working_root_alias is not None:
            project = txn.get(store.project_id)
            aliases = {r["alias"] for r in (project.body["allowed_roots"] if project else [])}
            if working_root_alias not in aliases:
                raise PrometheosError(
                    "policy_denied",
                    "working_root_alias is not an approved research root alias",
                    {"alias": working_root_alias},
                )
        apply_mod.op_create(
            txn,
            store.directory,
            CreateOp(
                op="create",
                kind="execution",
                id=execution_id,
                base_rev=0,
                sensitivity=sensitivity,  # type: ignore[arg-type]
                occurred_at=None,
                body=apply_mod.validate_body("execution", body).model_dump(mode="json"),
            ),
        )

    return store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256({"op": "execution_register", **params}),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=max_rank,
    )


def report(
    store: ProjectStore,
    *,
    execution_id: str,
    state: str,
    reported_at: str | None,
    receipt_version_ids: list[str],
    request_id: str,
    actor: str,
    max_rank: int,
) -> dict[str, Any]:
    """Record a reported status (and any receipts) as a full revision of the execution.

    ``state`` is what the reporter said, never an observation. A run reported completed,
    failed or cancelled is final: it may still gain receipts (the report of completion usually
    arrives before the receipt) but cannot be reported otherwise; a restart is a new
    registration. Receipt references only grow."""
    if state not in (*REPORTABLE, "status_unknown"):
        raise _invalid(
            "state must be one of reported_running, reported_completed, "
            "reported_failed, reported_cancelled or status_unknown"
        )
    if kind_of(execution_id) != "execution":
        raise _invalid("report names an execution record", id=execution_id)

    def handler(txn: Txn) -> None:
        current = txn.get(execution_id)
        if current is None or current.kind != "execution" or current.lifecycle == "deleted":
            raise PrometheosError("not_found", "execution not found", {"id": execution_id})
        if reported_at is not None and _parse(reported_at) > _parse(txn.recorded_at) + CLOCK_SKEW:
            raise PrometheosError(
                "validation_error",
                "reported_at is in the future; a report cannot be dated ahead of when it is "
                "recorded (a few minutes of clock skew are allowed)",
                {"reported_at": reported_at},
            )
        previous = current.body["state"]
        if previous in FINAL_STATES and state != previous:
            raise PrometheosError(
                "validation_error",
                f"this execution was reported {previous.removeprefix('reported_')}; that is final. "
                "Register a new execution for a restart.",
                {"id": execution_id, "state": previous},
            )
        refs = list(current.body["receipt_refs"])
        held = {r["id"] for r in refs}
        for version_id in receipt_version_ids:
            if version_id in held:
                continue
            version = txn.get(version_id)
            if version is None or version.kind != "version":
                raise PrometheosError("not_found", "receipt version not found", {"id": version_id})
            refs.append({"id": version_id, "rev": version.rev})
            held.add(version_id)
        body = dict(current.body)
        body["state"] = state
        body["receipt_refs"] = refs
        if state == "status_unknown":
            body["last_reported_at"] = reported_at or current.body["last_reported_at"]
        else:
            body["last_reported_at"] = reported_at or txn.recorded_at
        apply_mod.op_revise(
            txn,
            store.directory,
            ReviseOp(
                op="revise",
                id=execution_id,
                base_rev=current.rev,
                sensitivity=current.sensitivity,  # type: ignore[arg-type]
                occurred_at=None,
                body=apply_mod.validate_body("execution", body).model_dump(mode="json"),
            ),
        )

    return store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256(
            {
                "op": "execution_report",
                "id": execution_id,
                "state": state,
                "reported_at": reported_at,
                "receipts": sorted(receipt_version_ids),
            }
        ),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=max_rank,
    )


def effective_state(
    body: dict[str, Any], recorded_at: str, now: datetime, silence_after: timedelta
) -> dict[str, Any]:
    """The stored (reported) state and what a reader should assume now. A run that was
    registered or reported running and has been silent too long reads as ``status_unknown``;
    silence is never read as failed or completed."""
    stored = str(body["state"])
    reference = body.get("last_reported_at") or recorded_at
    effective, silent_since = stored, None
    if stored in ("registered", "reported_running") and now - _parse(reference) > silence_after:
        effective, silent_since = "status_unknown", reference
    return {"state": stored, "effective_state": effective, "silent_since": silent_since}


# ----- citations and the awaiting-interpretation queue ------------------------------------


def _marks(ids: list[str] | set[str]) -> str:
    return ",".join("?" for _ in ids)


def _record_lookup(conn: sqlite3.Connection) -> results.Lookup:
    def find(record_id: str) -> tuple[str, dict[str, Any]] | None:
        record = current_record(conn, record_id)
        return None if record is None else (record["kind"], record["body"])

    return find


def _link_events(
    conn: sqlite3.Connection,
    citer_id: str,
    body: dict[str, Any],
    targets: set[str],
    revision_seq: int,
) -> list[int]:
    """The commits that made ``citer_id`` cite one of ``targets``: the commit of its current
    revision when the body itself mentions one, and each ``cites`` relation added from it."""
    seqs = [
        int(r[0])
        for r in conn.execute(
            f"SELECT created_seq FROM relations WHERE source_id = ? AND relation = 'cites' "  # noqa: S608
            f"AND retired_seq IS NULL AND target_id IN ({_marks(targets)})",
            (citer_id, *sorted(targets)),
        )
    ]
    if results.cited_result_ids(_record_lookup(conn), body, []) & targets:
        seqs.append(revision_seq)
    return seqs


def _owner_decided(conn: sqlite3.Connection, seq: int) -> bool:
    """Did the owner decide the commit ``seq`` (a grant was spent in it)? Judged by the audit rows
    the apply path writes for an owner decision, not by the transport's name."""
    return (
        conn.execute(
            "SELECT 1 FROM audit WHERE seq = ? AND (operation LIKE 'owner_decision:%' OR "
            "operation LIKE 'owner_local_action:%') LIMIT 1",
            (seq,),
        ).fetchone()
        is not None
    )


def _actor_at(conn: sqlite3.Connection, seq: int) -> str | None:
    row = conn.execute("SELECT actor FROM audit WHERE seq = ? LIMIT 1", (seq,)).fetchone()
    return str(row[0]) if row else None


def citers(conn: sqlite3.Connection, targets: set[str]) -> list[dict[str, Any]]:
    """Active assessments and reviewed entries that cite any of ``targets``. "Cites" is decided
    by ``results.cited_result_ids``, the one definition the protected effect also uses (decision
    0018 round 2): any ID the body mentions as the reference index reads it, or a live ``cites``
    relation. Derived on every read; nothing is stored."""
    if not targets:
        return []
    ids = sorted(targets)
    found = {
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT from_id FROM refs WHERE to_id IN ({_marks(ids)})",  # noqa: S608
            ids,
        )
    }
    found |= {
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT source_id FROM relations WHERE relation = 'cites' "  # noqa: S608
            f"AND retired_seq IS NULL AND target_id IN ({_marks(ids)})",
            ids,
        )
    }
    lookup = _record_lookup(conn)
    out: list[dict[str, Any]] = []
    for citer_id in sorted(found):
        if kind_of(citer_id) not in ("assessment", "entry"):
            continue
        record = current_record(conn, citer_id)
        if record is None or record["lifecycle"] != "active":
            continue
        body = record["body"]
        if body.get("purged"):
            continue
        if record["kind"] == "entry":
            if body.get("review") != "reviewed":
                continue
            how = {"review": "reviewed", "category": body.get("category")}
        else:
            how = {"method": body.get("method"), "outcome": body.get("outcome")}
        cites = [
            r[0]
            for r in conn.execute(
                "SELECT target_id FROM relations WHERE source_id = ? AND relation = 'cites' "
                "AND retired_seq IS NULL",
                (citer_id,),
            )
        ]
        if not results.cited_result_ids(lookup, body, cites) & targets:
            continue
        # Who made the link (decision 0018 round 3). A reviewed entry citing a result is a
        # protected effect, so it is the owner's by construction. An assessment is the owner's
        # only if a grant was spent in a commit that made it cite the item; an agent that adds a
        # citation to someone else's assessment (a researcher's included) is not.
        revision_seq = int(
            conn.execute(
                "SELECT commit_seq FROM revisions WHERE record_id = ? AND rev = ?",
                (citer_id, record["rev"]),
            ).fetchone()[0]
        )
        events = _link_events(conn, citer_id, body, targets, revision_seq)
        decided = [seq for seq in events if _owner_decided(conn, seq)]
        undecided = [seq for seq in events if seq not in decided]
        owner_authorised = record["kind"] == "entry" or bool(decided)
        out.append(
            {
                "id": citer_id,
                "kind": record["kind"],
                "sensitivity": record["sensitivity"],
                "owner_authorised": owner_authorised,
                "actor": _actor_at(conn, max(undecided) if undecided else max(events, default=0)),
                **how,
            }
        )
    return out


def _receipt_versions(conn: sqlite3.Connection, caller: Caller) -> list[dict[str, Any]]:
    """Visible execution-receipt versions with the facts a reader needs to recognise them."""
    rows = conn.execute(
        "SELECT r.id, r.sensitivity, r.created_seq, b.encoded FROM records r JOIN revisions v ON "
        "v.record_id = r.id AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = 'version' AND r.lifecycle != 'deleted' "
        "AND json_extract(b.encoded, '$.payload_schema') = 'execution_receipt_v1' "
        "ORDER BY r.created_seq, r.id"
    ).fetchall()
    out = []
    for row in rows:
        if not caller.may_see(row["sensitivity"]):
            continue
        body = loads_internal(row["encoded"])
        source = current_record(conn, body["source_id"])
        payload_row = conn.execute(
            "SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)
        ).fetchone()
        payload = loads_internal(bytes(payload_row["data"])) if payload_row else {}
        out.append(
            {
                "version_id": row["id"],
                "source_id": body["source_id"],
                "title": source["body"]["title"] if source else "(execution receipt)",
                "sensitivity": row["sensitivity"],
                "created_seq": row["created_seq"],
                "captured_at": body["captured_at"],
                "reporter": payload.get("reporter"),
                "outcome": payload.get("outcome"),
                "command": payload.get("command"),
                "ended_at": payload.get("ended_at"),
                "outputs": len(payload.get("outputs", [])),
            }
        )
    return out


def _executions(conn: sqlite3.Connection, caller: Caller) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.id FROM records r WHERE r.kind = 'execution' AND r.lifecycle = 'active' "
        "ORDER BY r.created_seq, r.id"
    ).fetchall()
    out = []
    for row in rows:
        record = current_record(conn, row["id"])
        if (
            record is None
            or not caller.may_see(record["sensitivity"])
            or record["body"].get("purged")
        ):
            continue
        out.append(record)
    return out


def _receipt_source_ids(conn: sqlite3.Connection, version_ids: list[str]) -> set[str]:
    sources = set()
    for version_id in version_ids:
        record = current_record(conn, version_id)
        if record is not None and "source_id" in record["body"]:
            sources.add(record["body"]["source_id"])
    return sources


def _visible_citers(found: list[dict[str, Any]], caller: Caller) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in c.items() if k != "sensitivity"}
        for c in found
        if caller.may_see(c["sensitivity"])
    ]


def _result_items(
    conn: sqlite3.Connection, caller: Caller
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Every completed execution and bare receipt the caller can see with the records that cite
    it (oldest first): the population of the queue, split by whether anything cites it."""
    executions = _executions(conn, caller)
    held: set[str] = set()
    for held_ids in _executions_all_receipts(conn):
        held |= held_ids
    items: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    receipts_by_id = {r["version_id"]: r for r in _receipt_versions(conn, caller)}
    for record in executions:
        body = record["body"]
        if body["state"] != "reported_completed":
            continue
        receipt_ids = [r["id"] for r in body["receipt_refs"]]
        targets = {record["id"], *receipt_ids, *_receipt_source_ids(conn, receipt_ids)}
        items.append(
            (
                {
                    "target_id": record["id"],
                    "target_kind": "execution",
                    "title": body["title"],
                    "since": body["last_reported_at"] or record["recorded_at"],
                    "reporter": body["reporter"],
                    "outcome": "reported completed",
                    "receipts": [
                        {"version_id": rid, "title": receipts_by_id[rid]["title"]}
                        for rid in receipt_ids
                        if rid in receipts_by_id
                    ],
                    "reason": (
                        f"Reported completed by {body['reporter']} at "
                        f"{body['last_reported_at'] or record['recorded_at']}; no assessment or "
                        "reviewed entry cites it yet."
                    ),
                    "sort": record["id"],
                },
                citers(conn, targets),
            )
        )
    for version_id, receipt in receipts_by_id.items():
        if version_id in held:
            continue
        targets = {version_id, receipt["source_id"]}
        items.append(
            (
                {
                    "target_id": version_id,
                    "target_kind": "receipt",
                    "title": receipt["title"],
                    "since": receipt["ended_at"] or receipt["captured_at"],
                    "reporter": receipt["reporter"],
                    "outcome": f"reported {receipt['outcome']}",
                    "receipts": [{"version_id": version_id, "title": receipt["title"]}],
                    "reason": (
                        f"Execution receipt from {receipt['reporter']} (reported "
                        f"{receipt['outcome']}) recorded without a registered run; no assessment "
                        "or reviewed entry cites it yet."
                    ),
                    "sort": version_id,
                },
                citers(conn, targets),
            )
        )
    items.sort(key=lambda pair: (pair[0]["since"] or "", pair[0]["sort"]))
    return items


def _unsorted(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "sort"}


def awaiting_queue(conn: sqlite3.Connection, caller: Caller) -> list[dict[str, Any]]:
    """Completed executions and bare receipts that no assessment or reviewed entry cites,
    oldest first. An item leaves only when such a record cites it (its execution, one of its
    receipts or their sources); a dismissal is a reviewed entry giving the reason."""
    return [_unsorted(item) for item, found in _result_items(conn, caller) if not found]


_METHOD_WORDS = {
    "reported_agent": "an agent-reported assessment",
    "researcher": "a researcher-method assessment",
    "deterministic_check": "a deterministic check",
}


def _outcome_words(outcomes: list[str | None]) -> str:
    """What the citing assessments say about their claim, in words (decision 0078)."""
    words = {
        "supports": "the assessment supports its claim",
        "challenges": "the assessment challenges its claim",
        "inconclusive": "the assessment is inconclusive",
    }
    distinct = list(dict.fromkeys(o for o in outcomes if o))
    if not distinct and outcomes:
        return "an assessment you cannot read"
    if len(distinct) == 1:
        return words.get(distinct[0], distinct[0])
    return "the assessments disagree (" + ", ".join(distinct) + ")" if distinct else "no outcome"


def agent_interpreted(conn: sqlite3.Connection, caller: Caller) -> list[dict[str, Any]]:
    """Results that left the queue without the owner's decision: every record citing them is an
    assessment whose citation no grant was spent on (decision 0018 round 3: judged by who added
    the link, not by the assessment's method, so a citation an agent adds to the researcher's own
    assessment shows too). Spec/05 lets an assessment clear an item; the owner is shown that it
    happened, by whom, and may still record a reviewed interpretation."""
    out = []
    for item, found in _result_items(conn, caller):
        if not found or any(c["owner_authorised"] for c in found):
            continue
        out.append(
            _unsorted(item)
            | {
                "interpreted_by": [
                    {
                        "id": c["id"] if caller.may_see(c["sensitivity"]) else None,
                        "method": c["method"],
                        # The outcome is the record's content, hidden with it (review F2).
                        "outcome": c["outcome"] if caller.may_see(c["sensitivity"]) else None,
                        "actor": c["actor"],
                    }
                    for c in found
                ]
            }
        )
    return out


def _executions_all_receipts(conn: sqlite3.Connection) -> list[set[str]]:
    """The receipt version IDs each execution (of any state, visible or not) already holds: a
    receipt a registered run holds is covered by that run's own queue entry."""
    out = []
    for row in conn.execute(
        "SELECT b.encoded FROM records r JOIN revisions v ON v.record_id = r.id AND "
        "v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = 'execution' AND r.lifecycle = 'active' AND b.encoded IS NOT NULL"
    ):
        out.append({ref["id"] for ref in loads_internal(row["encoded"])["receipt_refs"]})
    return out


def execution_views(
    conn: sqlite3.Connection,
    caller: Caller,
    *,
    state: str | None = None,
    awaiting: bool | None = None,
    execution_id: str | None = None,
    limit: int = 100,
    silence_after_hours: int = SILENCE_AFTER_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Executions with their reported and effective state and whether each awaits
    interpretation. ``state`` filters on the effective state."""
    if not 1 <= limit <= 200:
        raise _invalid("limit must be 1..200")
    if not 1 <= silence_after_hours <= 24 * 90:
        raise _invalid("silence_after_hours must be 1..2160")
    if state is not None and state not in ("registered", *REPORTABLE, "status_unknown"):
        raise _invalid("unknown execution state", state=state)
    moment = now or _now()
    silence = timedelta(hours=silence_after_hours)
    awaiting_ids = {i["target_id"] for i in awaiting_queue(conn, caller)}
    receipts = {r["version_id"]: r for r in _receipt_versions(conn, caller)}
    views = []
    for record in _executions(conn, caller):
        if execution_id is not None and record["id"] != execution_id:
            continue
        body = record["body"]
        status = effective_state(body, record["recorded_at"], moment, silence)
        if state is not None and status["effective_state"] != state:
            continue
        is_awaiting = record["id"] in awaiting_ids
        if awaiting is not None and is_awaiting != awaiting:
            continue
        receipt_ids = [r["id"] for r in body["receipt_refs"]]
        targets = {record["id"], *receipt_ids, *_receipt_source_ids(conn, receipt_ids)}
        views.append(
            {
                "id": record["id"],
                "rev": record["rev"],
                "title": body["title"],
                "host_label": body["host_label"],
                "reporter": body["reporter"],
                "command_summary": body["command_summary"],
                "working_root_alias": body["working_root_alias"],
                "external_ref": body["external_ref"],
                "expected_outputs": body["expected_outputs"],
                **status,
                "observation": "reported",
                "last_reported_at": body["last_reported_at"],
                "registered_at": _registered_at(conn, record["id"]),
                "receipts": [
                    {
                        "version_id": rid,
                        "title": receipts[rid]["title"],
                        "outcome": receipts[rid]["outcome"],
                    }
                    for rid in receipt_ids
                    if rid in receipts
                ],
                "awaiting_interpretation": is_awaiting,
                "interpreted_by": _visible_citers(citers(conn, targets), caller),
            }
        )
    truncated = len(views) > limit
    return {"executions": views[:limit], "truncated": truncated}


def _registered_at(conn: sqlite3.Connection, record_id: str) -> str | None:
    row = conn.execute(
        "SELECT recorded_at FROM revisions WHERE record_id = ? AND rev = 1", (record_id,)
    ).fetchone()
    return str(row[0]) if row else None


def awaiting_view(conn: sqlite3.Connection, caller: Caller) -> dict[str, Any]:
    items = awaiting_queue(conn, caller)
    return {
        "generation": "deterministic",
        "rule": (
            "A completed execution, or an execution receipt no registered run holds, waits "
            "until an assessment or a reviewed entry cites it; a dismissal is a reviewed "
            "entry that cites it and gives the reason."
        ),
        "count": len(items),
        "items": items,
    }


def resolution_entry_id(request_id: str) -> str:
    """The entry a resolution creates; derived from the request ID so a retry is the same."""
    return "entry_" + derived_request_id("awaiting_resolve", request_id)


def resolution_replay(
    store: ProjectStore, caller: Caller, request_id: str
) -> dict[str, Any] | None:
    """The stored response when this resolution already committed (a retry), else ``None``.
    The request body names the sequence it was built at, so a retry after the project moved
    is recognised by the entry it created, which must be visible at the caller's rank."""
    conn = store.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT response FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        response: dict[str, Any] = loads_internal(row["response"])
        entry_id = resolution_entry_id(request_id)
        if not any(r.get("id") == entry_id for r in response.get("results", [])):
            raise PrometheosError(
                "request_key_conflict",
                "request_id was already used for a different request",
                {"request_id": request_id},
            )
        record = current_record(conn, entry_id)
        if record is None or not caller.may_see(record["sensitivity"]):
            raise PrometheosError(
                "request_key_conflict",
                "request_id was already used for a different request",
                {"request_id": request_id},
            )
        response["replayed"] = True
        return response
    finally:
        conn.close()


def resolution_request(
    conn: sqlite3.Connection,
    caller: Caller,
    project_id: str,
    *,
    target_id: str,
    resolution: str,
    text: str,
    request_id: str,
) -> bytes:
    """The canonical apply request that takes an item out of the queue: a reviewed entry that
    gives the interpretation or the dismissal reason and cites the execution or receipt. It is
    an ordinary transaction, so an agent's attempt is a protected effect (``approval_required``)
    and the owner's carries a grant. The entry ID derives from ``request_id`` so a retry is the
    same request."""
    if resolution not in ("interpreted", "dismissed"):
        raise _invalid("resolution must be interpreted or dismissed")
    if not text.strip():
        raise _invalid("a resolution gives its text or reason; nothing leaves the queue silently")
    item = next(
        (
            i
            for i in (*awaiting_queue(conn, caller), *agent_interpreted(conn, caller))
            if i["target_id"] == target_id
        ),
        None,
    )
    if item is None:
        raise PrometheosError(
            "validation_error",
            "this record is not awaiting interpretation (nor interpreted only by an agent)",
            {"id": target_id},
        )
    target = current_record(conn, target_id)
    assert target is not None
    seq = int(conn.execute("SELECT project_seq FROM project_state").fetchone()[0])
    entry_id = resolution_entry_id(request_id)
    dismissed = resolution == "dismissed"
    verb = "Dismissed" if dismissed else "Interpretation of"
    body: dict[str, Any] = {
        "title": _clip(f"{verb}: {item['title']}", 300),
        "text": text.strip(),
        "category": "decision" if dismissed else "finding",
        "evidence": [],
        "claim_refs": [],
        "author_ref": None,
        "origin": "typed" if caller.scope == "owner" else "agent",
        "quantities": [],
        "review": "reviewed",
    }
    operations = [
        {
            "op": "create",
            "kind": "entry",
            "id": entry_id,
            "base_rev": 0,
            "sensitivity": target["sensitivity"],
            "occurred_at": None,
            "body": body,
        },
        {
            "op": "relation_add",
            "source_ref": {"id": entry_id, "rev": 1},
            "target_ref": {"id": target_id, "rev": target["rev"]},
            "relation": "cites",
        },
    ]
    return dumps_canonical(
        {
            "schema_version": 1,
            "project_id": project_id,
            "request_id": request_id,
            "expected_seq": seq,
            "operations": operations,
        }
    )


# ----- artifact availability and backup coverage ------------------------------------------


def _origin_reachable(settings: dict[str, Any], binding: dict[str, Any] | None) -> bool | None:
    """Cheap live check (no hashing, no path in the result) that a reference-only origin file
    still exists inside its approved root."""
    if not binding:
        return None
    root = next((r for r in settings["allowed_roots"] if r["alias"] == binding["alias"]), None)
    if root is None:
        return False
    try:
        resolved = resolve_in_root(Path(root["path"]), binding["path"])
    except PrometheosError:
        return False
    return os.path.isfile(resolved.path) and not is_link_or_reparse(resolved.path)


def _usable_backups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Inventory entries that currently count as backups: verified, and the archive still on
    disk (checked live; a deleted archive covers nothing)."""
    rows = conn.execute(
        "SELECT id, label, purpose, project_seq, created_at, location, verification "
        "FROM backup_inventory WHERE verification = 'verified' ORDER BY project_seq, id"
    ).fetchall()
    usable = []
    for row in rows:
        if not os.path.isfile(row["location"]):
            continue
        usable.append(
            {
                "id": row["id"],
                "label": row["label"],
                "purpose": row["purpose"],
                "project_seq": row["project_seq"],
                "created_at": row["created_at"],
            }
        )
    return usable


def _coverage_item(
    conn: sqlite3.Connection,
    store_dir: Path,
    settings: dict[str, Any],
    backups: list[dict[str, Any]],
    version: dict[str, Any],
    source: dict[str, Any],
    created_seq: int,
) -> dict[str, Any]:
    body = version["body"]
    computed = computed_availability(conn, store_dir, body, version["id"])
    latest = conn.execute(
        "SELECT origin_current_digest, origin_reachable FROM source_checks "
        "WHERE version_id = ? ORDER BY commit_seq DESC, id DESC LIMIT 1",
        (version["id"],),
    ).fetchone()
    origin_changed = None
    if body["storage"] == "external":
        live = _origin_reachable(settings, source["body"].get("root_binding"))
        if computed == "missing" or live is False:
            availability = "unavailable"
            why = (
                "the origin file is gone or was last observed unreachable; only its digest is held"
            )
        else:
            availability = "reference_only"
            why = "PrometheOS holds the digest, not the bytes; the origin file exists now"
        if latest is not None and latest["origin_current_digest"] not in (None, body["sha256"]):
            origin_changed = True
        covered, covering = False, []
        backup_why = (
            "reference-only bytes are never backed up; a digest is not a backup "
            "(copy the file under retention to protect it)"
        )
    else:
        availability = "retained" if computed != "missing" else "unavailable"
        why = (
            "retained bytes are present and match their digest"
            if availability == "retained"
            else "retained bytes are missing or fail their digest"
        )
        if body["storage"] == "media":
            holders = {
                int(r[0])
                for r in conn.execute(
                    "SELECT backup_id FROM backup_inventory_media WHERE sha256 = ?",
                    (body["sha256"],),
                )
            }
            covering = [b for b in backups if b["id"] in holders]
        else:
            covering = [b for b in backups if b["project_seq"] >= created_seq]
        covered = bool(covering) and availability == "retained"
        if covered:
            backup_why = "held by a verified backup that is still on disk"
        elif availability != "retained":
            backup_why = "the retained bytes are not intact, so no backup is claimed"
            covering = []
        elif not backups:
            backup_why = "no verified backup is recorded for this project"
        else:
            backup_why = "recorded backups predate this version or do not hold its bytes"
    return {
        "version_id": version["id"],
        "source_id": body["source_id"],
        "title": source["body"]["title"],
        "mime": body["mime"],
        "byte_length": body["byte_length"],
        "storage": body["storage"],
        "created_seq": created_seq,
        "availability": availability,
        "availability_reason": why,
        "origin_changed": origin_changed,
        "backup": {
            "covered": covered,
            "status": "backed_up" if covered else "not_backed_up",
            "reason": backup_why,
            "backups": [{k: v for k, v in b.items() if k != "id"} for b in covering]
            if covered
            else [],
        },
    }


def coverage_items(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    *,
    source_id: str | None = None,
) -> list[dict[str, Any]]:
    """Availability and backup coverage of every visible source version, oldest first (one
    pass; ``artifact_coverage`` pages it, the CP18 detectors read it whole)."""
    from prometheos.app.sources import _settings

    project_id = str(conn.execute("SELECT project_id FROM project_state").fetchone()[0])
    settings = _settings(conn, project_id)[0].model_dump(mode="json")
    backups = _usable_backups(conn)
    rows = conn.execute(
        "SELECT r.id, r.sensitivity, r.created_seq FROM records r WHERE r.kind = 'version' "
        "AND r.lifecycle != 'deleted' ORDER BY r.created_seq, r.id"
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        if not caller.may_see(row["sensitivity"]):
            continue
        version = current_record(conn, row["id"])
        if version is None or version["body"].get("purged") or "storage" not in version["body"]:
            continue
        if source_id is not None and version["body"]["source_id"] != source_id:
            continue
        source = current_record(conn, version["body"]["source_id"])
        if source is None or not caller.may_see(source["sensitivity"]):
            continue
        items.append(
            _coverage_item(conn, store_dir, settings, backups, version, source, row["created_seq"])
        )
    return items


def artifact_coverage(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    *,
    source_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    newest_first: bool = False,
) -> dict[str, Any]:
    """Availability and backup coverage of every visible source version.

    ``availability``: ``retained`` (bytes held and digest-verified), ``reference_only`` (the
    origin file exists; PrometheOS holds only its digest) or ``unavailable`` (retained bytes
    missing or corrupt, or the origin gone or last observed unreachable). ``backup`` is
    ``covered`` only for retained bytes a verified, still-present backup holds; reference-only
    bytes are never covered, whether or not their origin exists (decision 0018). ``totals`` count
    every visible version (of the source, when filtered), not only the page. The order is
    oldest first, or newest first with ``newest_first``; the cursor is ``<creation
    sequence>:<version id>`` (a commit can create many versions) and walks that order."""
    if not 1 <= limit <= 200:
        raise _invalid("limit must be 1..200")
    after: tuple[int, str] | None = None
    if cursor is not None:
        seq_text, _, after_id = cursor.partition(":")
        if not seq_text.isdigit() or not is_record_id(after_id, "version"):
            raise _invalid("malformed cursor")
        after = (int(seq_text), after_id)
    items = coverage_items(conn, store_dir, caller, source_id=source_id)
    if newest_first:
        items = list(reversed(items))
    totals = {
        "versions": 0,
        "retained": 0,
        "reference_only": 0,
        "unavailable": 0,
        "backed_up": 0,
        "not_backed_up": 0,
    }
    page: list[dict[str, Any]] = []
    next_cursor: str | None = None
    for item in items:
        totals["versions"] += 1
        totals[item["availability"]] += 1
        totals["backed_up" if item["backup"]["covered"] else "not_backed_up"] += 1

    def row_key(i: dict[str, Any]) -> tuple[int, str]:
        return (i["created_seq"], i["version_id"])

    start = 0
    if after is not None:
        start = next(
            (
                n
                for n, i in enumerate(items)
                if (row_key(i) < after if newest_first else row_key(i) > after)
            ),
            len(items),
        )
    page = items[start : start + limit]
    if start + limit < len(items) and page:
        next_cursor = f"{page[-1]['created_seq']}:{page[-1]['version_id']}"
    return {
        "generation": "deterministic",
        "totals": totals,
        "verified_backups": len(_usable_backups(conn)),
        "items": page,
        "next_cursor": next_cursor,
    }


# ----- receipt citations (dogfood F5) -----------------------------------------------------


def receipt_citation(
    conn: sqlite3.Connection,
    caller: Caller,
    version: dict[str, Any] | None,
    locator: dict[str, Any],
) -> dict[str, Any] | None:
    """What a JSON-pointer citation of an execution receipt points to, in words.

    ``/outputs/N`` and ``/inputs/N`` on an ``execution_receipt_v1`` version resolve to the
    artifact the receipt names there (its source title and role), so a reader sees the model
    or figure instead of a pointer. Anything else (another pointer, another payload, an
    artifact the caller cannot see) gets no label: the raw locator stays the fallback. A pure
    read; nothing is stored."""
    if version is None or locator.get("type") != "json":
        return None
    body = version["body"]
    if body.get("payload_schema") != "execution_receipt_v1":
        return None
    parts = str(locator.get("pointer", "")).split("/")
    if len(parts) != 3 or parts[1] not in ("outputs", "inputs") or not parts[2].isdigit():
        return None
    row = conn.execute("SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)).fetchone()
    if row is None:
        return None
    artifacts = loads_internal(bytes(row["data"])).get(parts[1], [])
    index = int(parts[2])
    if index >= len(artifacts):
        return None
    artifact = artifacts[index]
    source = current_record(conn, artifact["source_id"])
    target = current_record(conn, artifact["version_id"])
    if (
        source is None
        or target is None
        or not caller.may_see(source["sensitivity"])
        or not caller.may_see(target["sensitivity"])
    ):
        return None
    direction = "output" if parts[1] == "outputs" else "input"
    title = source["body"].get("title", "(untitled)")
    return {
        "direction": direction,
        "role": artifact["role"],
        "source_id": artifact["source_id"],
        "version_id": artifact["version_id"],
        "title": title,
        "label": f"{direction} {artifact['role']}: {title}",
    }


# ----- assumption links -------------------------------------------------------------------


def assumption_links(conn: sqlite3.Connection, caller: Caller, record_id: str) -> dict[str, Any]:
    """The attributed assumptions a claim or entry relies on, and, for an assumption, what
    relies on it; each says whether it was bound to an older revision (stale)."""
    relies_on, relied_on_by = [], []

    def visible(other: str) -> dict[str, Any] | None:
        record = current_record(conn, other)
        if (
            record is None
            or not caller.may_see(record["sensitivity"])
            or record["body"].get("purged")
        ):
            return None
        return record

    for row in conn.execute(
        "SELECT target_id, target_rev FROM relations WHERE source_id = ? AND relation = "
        "'depends_on' AND retired_seq IS NULL ORDER BY created_seq, id",
        (record_id,),
    ):
        target = visible(row["target_id"])
        if target is None or target["body"].get("category") != "assumption":
            continue
        successor = target["body"].get("superseded_by")
        relies_on.append(
            {
                "id": target["id"],
                "rev": row["target_rev"],
                "current_rev": target["rev"],
                "title": target["body"]["title"],
                "text": target["body"]["text"],
                "state": "superseded" if successor else "active",
                "superseded_by": successor,
                "stale": bool(successor) or row["target_rev"] < target["rev"],
            }
        )
    for row in conn.execute(
        "SELECT source_id, source_rev, target_rev FROM relations WHERE target_id = ? AND "
        "relation = 'depends_on' AND retired_seq IS NULL ORDER BY created_seq, id",
        (record_id,),
    ):
        source = visible(row["source_id"])
        if source is None:
            continue
        relied_on_by.append(
            {
                "id": source["id"],
                "kind": source["kind"],
                "rev": row["source_rev"],
                "relied_on_rev": row["target_rev"],
                "title": source["body"].get("title") or _clip(source["body"].get("statement", "")),
            }
        )
    return {"relies_on": relies_on, "relied_on_by": relied_on_by}


# ----- Now --------------------------------------------------------------------------------


def _title(record: dict[str, Any]) -> str:
    body = record["body"]
    if record["kind"] == "anchor":
        locator = body["locator"]
        where = (
            f"line {locator['start_line']}"
            if locator.get("type") == "text"
            else f"{locator.get('type', 'document')} location"
        )
        return f"Manuscript anchor ({where})"
    return _clip(
        str(
            body.get("title")
            or body.get("statement")
            or body.get("text")
            or body.get("reason")
            or record["id"]
        ),
        120,
    )


def now_view(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    *,
    since_seq: int | None = None,
    due_within_days: int = DUE_WITHIN_DAYS,
    silence_after_hours: int = SILENCE_AFTER_HOURS,
    limit: int = NOW_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Changed, due, running and awaiting-interpretation items, each with its reason (the
    approvals section is added by the service, which owns the approval table's bookkeeping).
    Deterministic and read-only."""
    if not 1 <= limit <= 200:
        raise _invalid("limit must be 1..200")
    if not 1 <= due_within_days <= 365:
        raise _invalid("due_within_days must be 1..365")
    moment = now or _now()
    snap: Snapshot = load_snapshot(conn, store_dir, caller, None)

    changed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for flag in snap.flags:
        record = snap.records.get(flag["record_id"])
        if record is None or record["id"] in seen:
            continue
        seen.add(record["id"])
        changed.append(
            {
                "id": record["id"],
                "kind": record["kind"],
                "title": _title(record),
                "reason": f"Needs review: {flag['reason']} (cause {flag['cause_id']})",
                "cause_id": flag["cause_id"],
                "seq": flag["created_seq"],
            }
        )
    for item in agent_interpreted(conn, caller):
        # Plain words first, then who and how (decision 0018 keeps both visible; 0078 words it).
        found = item["interpreted_by"]
        outcomes = [c["outcome"] for c in found]
        by = "; ".join(
            f"{_METHOD_WORDS.get(c['method'], c['method'])} from {c['actor'] or 'an unknown actor'}"
            for c in found
        )
        changed.append(
            {
                "id": item["target_id"],
                "kind": item["target_kind"],
                "title": item["title"],
                "reason": (
                    f"Checked without your review: {_outcome_words(outcomes)}. Recorded as {by}. "
                    "Confirm it, or record your own interpretation."
                ),
                "cause_id": found[0]["id"],
                "seq": None,
                "checked_by_agent": True,
                "outcomes": outcomes,
            }
        )
    if since_seq is not None:
        rows = conn.execute(
            "SELECT record_id, MAX(commit_seq) AS seq, MAX(rev) AS rev FROM revisions "
            "WHERE commit_seq > ? GROUP BY record_id ORDER BY seq DESC, record_id",
            (since_seq,),
        ).fetchall()
        for row in rows:
            record = snap.records.get(row["record_id"])
            if record is None or record["kind"] not in CHANGED_KINDS or record["id"] in seen:
                continue
            seen.add(record["id"])
            what = "Added" if row["rev"] == 1 else f"Revised to revision {row['rev']}"
            changed.append(
                {
                    "id": record["id"],
                    "kind": record["kind"],
                    "title": _title(record),
                    "reason": f"{what} at project sequence {row['seq']} (after {since_seq}).",
                    "cause_id": None,
                    "seq": row["seq"],
                }
            )

    horizon = moment + timedelta(days=due_within_days)
    due: list[dict[str, Any]] = []
    for record in snap.of("action"):
        body = record["body"]
        if body["status"] not in ("ready", "in_progress", "blocked") or body["due_at"] is None:
            continue
        when = _parse(body["due_at"])
        if when > horizon:
            continue
        overdue = when < moment
        due.append(
            {
                "id": record["id"],
                "kind": "action",
                "title": body["title"],
                "due_at": body["due_at"],
                "status": body["status"],
                "reason": (
                    f"Overdue since {body['due_at']}"
                    if overdue
                    else f"Due {body['due_at']} (within {due_within_days} days)"
                )
                + (f"; blocked: {_clip(body['blocker'] or '', 80)}" if body["blocker"] else ""),
            }
        )
    for record in snap.of("paper"):
        body = record["body"]
        if body["status"] in ("accepted", "withdrawn") or body["deadline"] is None:
            continue
        when = _parse(body["deadline"])
        if when > horizon:
            continue
        due.append(
            {
                "id": record["id"],
                "kind": "paper",
                "title": body["title"],
                "due_at": body["deadline"],
                "status": body["status"],
                "reason": (
                    f"Paper deadline passed {body['deadline']}"
                    if when < moment
                    else f"Paper deadline {body['deadline']} (within {due_within_days} days)"
                ),
            }
        )
    due.sort(key=lambda i: (i["due_at"], i["id"]))

    silence = timedelta(hours=silence_after_hours)
    running: list[dict[str, Any]] = []
    for record in (r for r in snap.of("execution") if r["lifecycle"] == "active"):
        body = record["body"]
        status = effective_state(body, record["recorded_at"], moment, silence)
        if status["effective_state"] not in OPEN_STATES:
            continue
        if status["effective_state"] == "status_unknown":
            since = status["silent_since"] or body["last_reported_at"] or record["recorded_at"]
            reason = (
                f"No report since {since}; status unknown (never assumed failed or completed)."
                if status["silent_since"]
                else f"{body['reporter']} reported the status unknown at {since}."
            )
        elif status["effective_state"] == "registered":
            reason = (
                f"Registered by {body['reporter']} on {body['host_label']}; no status reported yet."
            )
        else:
            reason = f"Reported running at {body['last_reported_at']} by {body['reporter']}."
        running.append(
            {
                "id": record["id"],
                "kind": "execution",
                "title": body["title"],
                "state": status["effective_state"],
                "reported_state": status["state"],
                "reason": reason + " Observed, never controlled, by PrometheOS.",
            }
        )
    marks = ",".join("?" for _ in RUN_OPEN_STATES)
    for row in conn.execute(
        "SELECT id, operation, state, stage, updated_at, caller_rank FROM operation_runs "  # noqa: S608
        f"WHERE state IN ({marks}) AND caller_rank <= ? ORDER BY created_at, id",
        (*RUN_OPEN_STATES, caller.rank),
    ):
        stage = f" (stage: {row['stage']})" if row["stage"] else ""
        running.append(
            {
                "id": row["id"],
                "kind": "operation_run",
                "title": row["operation"],
                "state": row["state"],
                "reported_state": None,
                "reason": f"{row['operation']} is {row['state'].replace('_', ' ')}{stage}; "
                f"last update {row['updated_at']}.",
            }
        )

    awaiting = awaiting_queue(conn, caller)
    # Decision 0110 (L2): a draft reading beside each result, when an output is a newer version
    # of a bound quantity's source (rules first, then the layer's agreeing line; writes nothing).
    from prometheos.app import results_layer

    drafts = results_layer.drafts(conn, store_dir, caller, awaiting) if awaiting else {}
    return {
        "generation": "deterministic",
        "project_seq": snap.seq,
        "generated_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "since_seq": since_seq,
        "due_within_days": due_within_days,
        "silence_after_hours": silence_after_hours,
        "changed": _section(changed, limit)
        | {"checked_by_agent": sum(1 for i in changed if i.get("checked_by_agent"))},
        "due": _section(due, limit),
        "running": _section(running, limit),
        "awaiting_interpretation": _section(
            [
                {
                    "id": i["target_id"],
                    "kind": i["target_kind"],
                    "title": i["title"],
                    "since": i["since"],
                    "reason": i["reason"],
                    "drafts": drafts.get(i["target_id"], []),
                }
                for i in awaiting
            ],
            limit,
        ),
    }


def approvals_section(rows: list[operational.ApprovalRequest], limit: int) -> dict[str, Any]:
    items = [
        {
            "id": row.id,
            "kind": "approval_request",
            "title": ", ".join(row.summary)[:200],
            "operation": row.operation,
            "actor": row.actor,
            "expires_at": row.expires_at,
            "owner_route": {"hub": row.owner_route_hub, "cli": row.owner_route_cli},
            "reason": (
                f"{row.actor} asked for your decision ({row.operation}): "
                f"{_clip(', '.join(row.summary), 120)}. Nothing happens until you grant or deny."
            ),
        }
        for row in rows
    ]
    return _section(items, limit)


def _section(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    return {"count": len(items), "items": items[:limit], "truncated": len(items) > limit}
