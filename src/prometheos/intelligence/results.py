"""The ``ai_results`` cache and the layer's call receipts (A07.7 Replay; spec/03 (s), (t), (w)).

``ai_results`` is rebuildable and never authoritative: one row per validated sample, keyed by
task kind, version, template digest, wire-schema digest, model, request parameters, input digest
and sample index. A cache hit returns the stored row and creates nothing; a new sample adds a
row and never alters one. Gateway calls are also written to ``provider_calls`` (the CP6.3 call
ledger): a receipt before the send, finished with what was sent and received (the served model
and provider, the billed cost, the data policy, the input bundle's digest and the reply body,
which inherits the highest sensitivity of the call's inputs).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import utc_now
from prometheos.store import db
from prometheos.store.project import ProjectStore

from .tasks import Task


def result_key(task: Task, model: str, params_sha256: str, input_sha256: str, sample: int) -> str:
    return canonical_sha256(
        {"kind": task.kind, "version": task.version, "template": task.template_sha256,
         "wire": task.wire_sha256, "model": model, "params": params_sha256,
         "input": input_sha256, "sample": sample}
    )  # fmt: skip


@dataclass(frozen=True)
class Stored:
    result_key: str
    model: str
    outcome: str
    reasons: tuple[str, ...]
    output: dict[str, Any] | None
    executor: str
    hint_only: bool
    served_model: str | None
    served_provider: str | None
    call_id: str | None

    @classmethod
    def of(cls, row: sqlite3.Row) -> Stored:
        return cls(
            row["result_key"], row["model"], row["outcome"], tuple(json.loads(row["reasons"])),
            json.loads(row["output"]) if row["output"] else None, row["executor"],
            bool(row["hint_only"]), row["served_model"], row["served_provider"], row["call_id"],
        )  # fmt: skip


def lookup(store: ProjectStore, key: str) -> Stored | None:
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM ai_results WHERE result_key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return Stored.of(row) if row else None


def save(
    store: ProjectStore,
    *,
    key: str,
    task: Task,
    model: str,
    params_sha256: str,
    input_sha256: str,
    sample: int,
    subject: str,
    executor: str,
    outcome: str,
    reasons: tuple[str, ...],
    output: dict[str, Any] | None,
    hint_only: bool,
    served_model: str | None,
    served_provider: str | None,
    call_id: str | None,
    run_id: str,
    sensitivity: str,
    scan_gen: int | None = None,
    subject_sha256: str | None = None,
) -> Stored:
    """Insert one sample; an existing row for the key is returned unchanged (never altered)."""
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO ai_results (result_key, task_kind, task_version, "
                "template_sha256, wire_sha256, model, params_sha256, input_sha256, sample, "
                "subject, subject_sha256, executor, outcome, reasons, output, hint_only, "
                "served_model, served_provider, call_id, run_id, sensitivity, scan_gen, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, task.kind, task.version, task.template_sha256, task.wire_sha256, model,
                 params_sha256, input_sha256, sample, subject, subject_sha256, executor, outcome,
                 json.dumps(list(reasons)), json.dumps(output) if output is not None else None,
                 int(hint_only), served_model, served_provider, call_id, run_id, sensitivity,
                 scan_gen, utc_now()),
            )  # fmt: skip
            row = conn.execute("SELECT * FROM ai_results WHERE result_key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return Stored.of(row)


def save_verdict(
    store: ProjectStore,
    *,
    task: Task,
    run_id: str,
    input_sha256: str,
    subject: str,
    outcome: str,
    result_keys: list[str],
    hint_only: bool,
) -> None:
    """A run's verdict on one subject and the samples it judged as one set (decision 0109; review
    map-ai-spread-a R2-F1). Written for judged outcomes only (not denied, held or not run)."""
    conn = store.connect()
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ai_verdicts'"
        ).fetchone()
        if found is None:  # a store that ran migration 16 before the table (review R3-F1)
            return
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO ai_verdicts (verdict_id, run_id, task_kind, task_version, "
                "template_sha256, wire_sha256, input_sha256, subject, outcome, result_keys, "
                "hint_only, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("aiv_" + uuid.uuid4().hex, run_id, task.kind, task.version, task.template_sha256,
                 task.wire_sha256, input_sha256, subject, outcome, json.dumps(result_keys),
                 int(hint_only), utc_now()),
            )  # fmt: skip
    finally:
        conn.close()


def latest_judged(conn: sqlite3.Connection, task: Task, input_sha256: str) -> dict[str, Any] | None:
    """The latest run's own verdict on this exact input, task version, template and wire schema
    (decisions 0109, 0110; review map-ai-spread-a R1-F1, R2-F1), as a read shows it:

    - ``{"agree": True, "results", "models", "executors", "hint_only"}`` for an ``accepted``
      verdict whose samples (the ones that run judged together) are all accepted, from at least
      ``k`` models;
    - ``{"agree": False, "models"}`` for ``disagreed``;
    - ``None`` for anything else, or a store without ``ai_verdicts`` (review R3-F1).

    A sample from outside the evaluated list makes it a hint (A07.5)."""
    has_verdicts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ai_verdicts'"
    ).fetchone()
    if has_verdicts is None:
        return None
    verdict = conn.execute(
        "SELECT outcome, result_keys, hint_only FROM ai_verdicts WHERE task_kind = ? "
        "AND task_version = ? AND template_sha256 = ? AND wire_sha256 = ? "
        "AND input_sha256 = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task.kind, task.version, task.template_sha256, task.wire_sha256, input_sha256),
    ).fetchone()
    if verdict is None or verdict[0] not in ("accepted", "disagreed"):
        return None
    keys = json.loads(verdict[1])
    marks = ",".join("?" for _ in keys)
    chosen = conn.execute(
        f"SELECT sample, model, outcome, output, executor, hint_only FROM ai_results "  # noqa: S608
        f"WHERE result_key IN ({marks}) ORDER BY sample",
        keys,
    ).fetchall()
    models = sorted({str(r[1]) for r in chosen})
    if verdict[0] == "disagreed":
        return {"agree": False, "models": models}
    if len(chosen) < task.agreement or any(r[2] != "accepted" for r in chosen):
        return None
    found = [(json.loads(r[3]) or {}).get("result") for r in chosen]
    results = [r for r in found if isinstance(r, dict)]
    if len(results) < len(chosen) or len(models) < task.agreement:
        return None
    return {
        "agree": True,
        "results": results,
        "models": models,
        "executors": sorted({str(r[4]) for r in chosen}),
        "hint_only": bool(verdict[2]) or any(bool(r[5]) for r in chosen),
    }


def call_sent(
    store: ProjectStore,
    *,
    run_id: str,
    task_kind: str,
    attempt: int,
    host: str,
    model: str,
    bundle_sha256: str,
    request_sha256: str,
    input_refs: list[str],
    estimate: str,
    data_policy: dict[str, Any],
    sensitivity: str,
) -> str:
    """The receipt written before a call is sent (state ``sent``); a call whose receipt is
    never finished is treated as an unknown outcome and never repeated (review cp22-a F7)."""
    call_id = str(uuid.uuid4())
    now = utc_now()
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO provider_calls (id, run_key, purpose, route, endpoint_host, model, "
                "attempt, state, reserved_cost, cost_basis, input_digest, request_identity, "
                "input_refs, created_at, updated_at, data_policy, bundle_digest, "
                "reply_sensitivity) "
                "VALUES (?,?,?,'remote',?,?,?,'sent',?,'estimate',?,?,?,?,?,?,?,?)",
                (call_id, run_id, f"ai_task:{task_kind}", host, model, attempt, estimate,
                 bundle_sha256, request_sha256, json.dumps(input_refs), now, now,
                 json.dumps(data_policy, sort_keys=True), bundle_sha256, sensitivity),
            )  # fmt: skip
    finally:
        conn.close()
    return call_id


def call_finish(
    store: ProjectStore,
    call_id: str,
    state: str,
    *,
    served_model: str | None = None,
    served_provider: str | None = None,
    billed: str | None = None,
    reply_body: str | None = None,
    error: str | None = None,
) -> None:
    if state not in ("succeeded", "failed", "unknown"):
        raise ValueError(state)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE provider_calls SET state = ?, served_model = ?, served_provider = ?, "
                "billed_cost = ?, actual_cost = ?, cost_basis = ?, reply_body = ?, error = ?, "
                "updated_at = ? WHERE id = ?",
                (state, served_model, served_provider, billed, billed,
                 "billed" if billed is not None else "estimate", reply_body,
                 None if error is None else error[:300], utc_now(), call_id),
            )  # fmt: skip
    finally:
        conn.close()


def summary(outcome: str, output: dict[str, Any] | None) -> str | None:
    """What a sample says, in a line the owner can read before using it (decision 0104)."""
    if output is None:
        return None
    abstain = output.get("abstain")
    if isinstance(abstain, dict):
        return f"abstained: {str(abstain.get('reason', '')).replace('_', ' ')}"
    result = output.get("result")
    if not isinstance(result, dict):
        return None
    if "role" in result:
        return f"role: {str(result['role']).replace('_', ' ')}"
    if "statements" in result:
        said = [f"{s.get('referent') or 'unnamed'} {str(s.get('status', '')).replace('_', ' ')}"
                for s in result["statements"]]  # fmt: skip
        return "; ".join(said) if said else "no status statements"
    return None


def claim(output: dict[str, Any] | None) -> str | None:
    """What two samples must share to agree, as ``assist.same`` compares them: a role, or the
    set of (line, referent, status, date) claims (review cp22-a H5)."""
    result = (output or {}).get("result")
    if not isinstance(result, dict):
        return None
    if "statements" in result:
        return json.dumps(sorted(
            [s.get("line"), s.get("referent"), s.get("status"), s.get("date")]
            for s in result["statements"]
        ))  # fmt: skip
    return json.dumps(result.get("role"))


def listing(
    store: ProjectStore,
    *,
    task_kind: str | None = None,
    limit: int = 200,
    scan_gen: int | None = None,
) -> list[dict[str, Any]]:
    """The newest samples first (review cp22-a R5-F2), optionally one task's or one scan's.

    A sample stored without a scan (a measurement's, reused by a later run from the cache) is
    located in the latest scan that holds a file at its path **with the same bytes** (its digest;
    review cp22-a R8-F2); a sample without a digest is not located. Accepting still re-reads the
    file and takes only samples of its exact current bytes (decision 0104).
    """
    conn = store.connect()
    try:
        kind = task_kind or None
        rows = conn.execute(
            "SELECT r.*, COALESCE(r.scan_gen, (SELECT MAX(f.scan_gen) FROM adoption_files f "
            "WHERE f.path = r.subject AND f.sha256 = r.subject_sha256)) AS located "
            "FROM ai_results r WHERE (? IS NULL OR r.task_kind = ?) AND (? IS NULL OR "
            "COALESCE(r.scan_gen, (SELECT MAX(f.scan_gen) FROM adoption_files f "
            "WHERE f.path = r.subject AND f.sha256 = r.subject_sha256)) = ?) "
            "ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?",
            (kind, kind, scan_gen, scan_gen, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"result_key": r["result_key"], "task_kind": r["task_kind"],
         "task_version": r["task_version"], "subject": r["subject"],
         "sample": r["sample"], "executor": r["executor"], "outcome": r["outcome"],
         "reasons": json.loads(r["reasons"]), "hint_only": bool(r["hint_only"]),
         "model": r["model"], "served_model": r["served_model"], "run_id": r["run_id"],
         "scan_gen": r["located"], "input_sha256": r["input_sha256"],
         "summary": summary(r["outcome"], json.loads(r["output"]) if r["output"] else None),
         "claim": claim(json.loads(r["output"]) if r["output"] else None)}
        for r in rows
    ]  # fmt: skip
