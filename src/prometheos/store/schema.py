"""Versioned store schema. Each migration adds only what its checkpoint needs."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from prometheos.store.derived import migrate_v2, migrate_v3
from prometheos.store.migrations import Migration

V1_CORE = """
CREATE TABLE project_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    project_id TEXT NOT NULL,
    project_seq INTEGER NOT NULL CHECK (project_seq >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE bodies (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL,
    encoded TEXT,
    purged_seq INTEGER
);

CREATE TABLE records (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    current_rev INTEGER NOT NULL CHECK (current_rev >= 1),
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('normal','restricted','private')),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active','archived','deleted')),
    created_seq INTEGER NOT NULL,
    updated_seq INTEGER NOT NULL
);
CREATE INDEX records_kind ON records(kind, updated_seq);

CREATE TABLE revisions (
    record_id TEXT NOT NULL REFERENCES records(id),
    rev INTEGER NOT NULL CHECK (rev >= 1),
    commit_seq INTEGER NOT NULL,
    op_index INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    occurred_at TEXT,
    body_id INTEGER NOT NULL REFERENCES bodies(id),
    sensitivity TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    PRIMARY KEY (record_id, rev)
);
CREATE INDEX revisions_seq ON revisions(commit_seq);

CREATE TABLE relations (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES records(id),
    source_rev INTEGER NOT NULL,
    target_id TEXT NOT NULL REFERENCES records(id),
    target_rev INTEGER NOT NULL,
    relation TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    retired_seq INTEGER
);
CREATE INDEX relations_source ON relations(source_id);
CREATE INDEX relations_target ON relations(target_id);

CREATE TABLE audit (
    seq INTEGER NOT NULL,
    op_index INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    record_id TEXT,
    rev INTEGER,
    actor TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (seq, op_index)
);

CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    project_seq INTEGER NOT NULL,
    response TEXT NOT NULL,
    committed_at TEXT NOT NULL
);

CREATE TABLE media (
    sha256 TEXT PRIMARY KEY,
    byte_length INTEGER NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    created_seq INTEGER NOT NULL
);

CREATE TABLE payloads (
    sha256 TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    byte_length INTEGER NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE source_checks (
    id INTEGER PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES records(id),
    checked_at TEXT NOT NULL,
    retained_digest_ok INTEGER,
    origin_current_digest TEXT,
    origin_reachable INTEGER,
    error TEXT,
    commit_seq INTEGER NOT NULL
);
CREATE INDEX source_checks_version ON source_checks(version_id, commit_seq);

CREATE TABLE import_plans (
    plan_id TEXT PRIMARY KEY,
    plan_sha256 TEXT NOT NULL,
    plan TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','committed')),
    commit_request_id TEXT
);
"""


def _v1(conn: sqlite3.Connection) -> None:
    for statement in V1_CORE.split(";"):
        if statement.strip():
            conn.execute(statement)


def _v4(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE task_contexts (task_id TEXT PRIMARY KEY REFERENCES records(id), "
        "context_text TEXT, manifest TEXT)"
    )


V5_JOBS = """
CREATE TABLE job_slots (
    job_id TEXT NOT NULL REFERENCES records(id),
    due_utc TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending','running','succeeded','failed','unknown','skipped')
    ),
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    input_digest TEXT,
    result TEXT,
    reason TEXT,
    external_started_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, due_utc)
);
CREATE INDEX job_slots_state ON job_slots(state, next_attempt_at);

CREATE TABLE scheduler_lease (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE job_notifications (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL,
    due_utc TEXT NOT NULL,
    commit_seq INTEGER NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    record_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dismissed_at TEXT
);
CREATE INDEX job_notifications_seq ON job_notifications(commit_seq);
"""


def _v5(conn: sqlite3.Connection) -> None:
    for statement in V5_JOBS.split(";"):
        if statement.strip():
            conn.execute(statement)


V6_AUDIO_PROVIDER = """
CREATE TABLE recording_sessions (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (
        state IN ('recording','paused','interrupted','stopped','cancelled','imported','discarded')
    ),
    title TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('normal','restricted','private')),
    backend TEXT NOT NULL,
    sources TEXT NOT NULL,
    sample_rate INTEGER NOT NULL,
    segment_seconds INTEGER NOT NULL,
    frames INTEGER NOT NULL DEFAULT 0,
    peak_level REAL NOT NULL DEFAULT 0,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    heartbeat_at TEXT,
    reason TEXT,
    consent_acknowledged INTEGER NOT NULL,
    max_rank INTEGER NOT NULL DEFAULT 0,
    version_id TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE recording_segments (
    session_id TEXT NOT NULL REFERENCES recording_sessions(id),
    idx INTEGER NOT NULL,
    file TEXT NOT NULL,
    frames INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (session_id, idx)
);

CREATE TABLE transcription_runs (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    config TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending','running','completed','failed','cancelled')
    ),
    total_ms INTEGER,
    next_chunk INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER,
    lease_owner TEXT,
    lease_expires_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_rank INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    result_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX transcription_runs_version ON transcription_runs(version_id);

CREATE TABLE transcription_chunks (
    run_id TEXT NOT NULL REFERENCES transcription_runs(id),
    idx INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    segments TEXT NOT NULL,
    PRIMARY KEY (run_id, idx)
);

CREATE TABLE provider_settings (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL,
    config TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE provider_calls (
    id TEXT PRIMARY KEY,
    run_key TEXT NOT NULL,
    purpose TEXT NOT NULL,
    route TEXT NOT NULL,
    endpoint_host TEXT NOT NULL,
    model TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('reserved','sent','succeeded','failed','unknown','refused')
    ),
    reserved_cost TEXT,
    actual_cost TEXT,
    cost_basis TEXT,
    usage TEXT,
    input_digest TEXT NOT NULL,
    request_identity TEXT NOT NULL DEFAULT '',
    max_rank INTEGER NOT NULL DEFAULT 2,
    output_digest TEXT,
    input_refs TEXT NOT NULL,
    task_id TEXT,
    error TEXT,
    acknowledges TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX provider_calls_run ON provider_calls(run_key, attempt);
"""


def _v6(conn: sqlite3.Connection) -> None:
    for statement in V6_AUDIO_PROVIDER.split(";"):
        if statement.strip():
            conn.execute(statement)


# A02, CP10 (decision 0015): operation runs (a), approval requests (b) and the metadata-only
# backup inventory (c). Row contracts: ``store.operational``. Statuses and heartbeats here
# never advance the project sequence.
V7_CP10 = r"""
CREATE TABLE operation_runs (
    id TEXT PRIMARY KEY CHECK (id LIKE 'oprun\_%' ESCAPE '\'),
    operation TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued','running','waiting_for_input','paused_budget','completed','failed',
                  'cancelled','unknown_external')
    ),
    caller_rank INTEGER NOT NULL CHECK (caller_rank BETWEEN 0 AND 2),
    actor TEXT NOT NULL,
    start_request_id TEXT,
    start_request_sha256 TEXT,
    params TEXT NOT NULL,
    params_digest TEXT NOT NULL,
    input_digests TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    workflow_version TEXT,
    model TEXT,
    budget_reservation TEXT,
    external_effect TEXT NOT NULL DEFAULT 'none' CHECK (
        external_effect IN ('none','started','confirmed','uncertain')
    ),
    external_call TEXT,
    waiting_on TEXT,
    stage TEXT,
    progress TEXT,
    cancel_requested_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error TEXT,
    limitations TEXT NOT NULL DEFAULT '[]',
    artifacts TEXT NOT NULL DEFAULT '[]',
    result_seq INTEGER,
    job_id TEXT,
    job_due_utc TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    CHECK ((state IN ('completed','failed','cancelled')) = (finished_at IS NOT NULL)),
    CHECK (state != 'unknown_external' OR external_effect = 'uncertain'),
    CHECK ((external_effect = 'none') = (external_call IS NULL)),
    CHECK ((state = 'waiting_for_input') = (waiting_on IS NOT NULL))
);
CREATE INDEX operation_runs_state ON operation_runs(state, updated_at);
CREATE UNIQUE INDEX operation_runs_start_request ON operation_runs(start_request_id)
    WHERE start_request_id IS NOT NULL;

CREATE TABLE operation_run_logs (
    run_id TEXT NOT NULL REFERENCES operation_runs(id),
    idx INTEGER NOT NULL CHECK (idx >= 0),
    at TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('info','warning','error')),
    message TEXT NOT NULL,
    PRIMARY KEY (run_id, idx)
);

CREATE TABLE approval_requests (
    id TEXT PRIMARY KEY CHECK (id LIKE 'apreq\_%' ESCAPE '\'),
    project_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    caller_rank INTEGER NOT NULL CHECK (caller_rank BETWEEN 0 AND 2),
    actor TEXT NOT NULL,
    request_id TEXT,
    plan TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    required_scope TEXT NOT NULL,
    owner_route_hub TEXT NOT NULL,
    owner_route_cli TEXT NOT NULL,
    summary TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending','granted','denied','withdrawn','expired','revoked','used')
    ),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_note TEXT,
    capability_id TEXT REFERENCES capabilities(id),
    used_at TEXT,
    used_seq INTEGER,
    result TEXT,
    closed_at TEXT,
    CHECK ((state IN ('pending','granted')) = (closed_at IS NULL)),
    CHECK ((state = 'used') = (used_at IS NOT NULL)),
    CHECK ((decided_at IS NULL) = (decided_by IS NULL)),
    CHECK (capability_id IS NULL OR decided_at IS NOT NULL),
    CHECK (CASE WHEN state = 'pending' THEN decided_at IS NULL
                WHEN state IN ('granted','denied','used','revoked') THEN decided_at IS NOT NULL
                ELSE 1 END),
    CHECK (CASE WHEN state IN ('granted','used','revoked') THEN capability_id IS NOT NULL
                WHEN state IN ('pending','denied') THEN capability_id IS NULL
                ELSE (decided_at IS NULL) = (capability_id IS NULL) END)
);
CREATE INDEX approval_requests_state ON approval_requests(state, expires_at);
CREATE UNIQUE INDEX approval_requests_request ON approval_requests(request_id)
    WHERE request_id IS NOT NULL;

CREATE TABLE backup_inventory (
    id INTEGER PRIMARY KEY,
    backup_sha256 TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL,
    location TEXT NOT NULL,
    label TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('manual','pre_migration','pre_restore')),
    project_seq INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    software_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    database_sha256 TEXT NOT NULL,
    media_count INTEGER NOT NULL,
    media_bytes INTEGER NOT NULL,
    reference_only_count INTEGER NOT NULL,
    verification TEXT NOT NULL CHECK (verification IN ('verified','failed','unverified')),
    verified_at TEXT,
    last_checked_at TEXT,
    present INTEGER CHECK (present IN (0, 1)),
    recorded_at TEXT NOT NULL
);

CREATE TABLE backup_inventory_media (
    backup_id INTEGER NOT NULL REFERENCES backup_inventory(id),
    sha256 TEXT NOT NULL,
    PRIMARY KEY (backup_id, sha256)
);
CREATE INDEX backup_inventory_media_sha ON backup_inventory_media(sha256);
"""


def _v7(conn: sqlite3.Connection) -> None:
    for statement in V7_CP10.split(";"):
        if statement.strip():
            conn.execute(statement)


# A02, CP10.3 (decision 0017): completed run stages, and CP6 transcription runs folded into
# ``operation_runs`` as the single run of record. Chunk results stay as the transcription's
# stage detail, keyed by the oprun_ id. No row is dropped: every transcription run becomes an
# operation run (``oprun_<its UUID>``) and every stored chunk follows it.
V8_STAGES = r"""
CREATE TABLE operation_run_stages (
    run_id TEXT NOT NULL REFERENCES operation_runs(id),
    idx INTEGER NOT NULL CHECK (idx >= 0),
    name TEXT NOT NULL,
    output TEXT NOT NULL,
    charged TEXT,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, idx),
    UNIQUE (run_id, name)
);

CREATE TABLE transcription_chunks_v8 (
    run_id TEXT NOT NULL REFERENCES operation_runs(id),
    idx INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    segments TEXT NOT NULL,
    PRIMARY KEY (run_id, idx)
)
"""

_V8_STATES = {
    "pending": "queued",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _v8_run(conn: sqlite3.Connection, row: sqlite3.Row) -> Any:
    """The operation run a CP6 transcription run becomes (validated by its row contract)."""
    from prometheos.domain.canonical import canonical_sha256
    from prometheos.store import operational as op

    document = json.loads(row["config"])
    state = _V8_STATES[row["state"]]
    terminal = state in op.TERMINAL_RUN_STATES
    chunks = conn.execute(
        "SELECT COUNT(*) FROM transcription_chunks WHERE run_id = ?", (row["id"],)
    ).fetchone()[0]
    params = {"version_id": row["version_id"], "config": document}
    inputs = [{"name": "audio", "sha256": row["input_sha256"]}]
    artifacts = []
    if row["result_version_id"]:
        artifacts.append({"id": row["result_version_id"], "rev": 1, "role": "transcript"})
    error: dict[str, Any] | None = None
    if row["error"] or state == "failed":
        error = {
            "code": "io_error",
            "message": (row["error"] or "transcription failed")[:4096],
            "retryable": True,
            "details": {},
        }
    lease_live = state == "running" and row["lease_owner"] and row["lease_expires_at"]
    done = row["chunk_count"] if state == "completed" and row["chunk_count"] else chunks
    return op.OperationRun.model_validate(
        {
            "id": f"oprun_{row['id']}",
            "operation": "audio_transcribe",
            "state": state,
            "caller_rank": int(row["max_rank"]),
            "actor": "cp6-migration",
            "start_request_id": None,
            "start_request_sha256": None,
            "params": params,
            "params_digest": canonical_sha256(params),
            "input_digests": inputs,
            "input_digest": canonical_sha256(inputs),
            "config_digest": row["config_digest"],
            "workflow_version": "transcribe-v1",
            "model": document.get("model") or document.get("engine"),
            "budget_reservation": None,
            "external_effect": "none",
            "external_call": None,
            "waiting_on": None,
            "stage": "chunks" if row["chunk_count"] is not None else None,
            "progress": (
                {"done": int(done), "total": int(row["chunk_count"]), "unit": "chunks"}
                if row["chunk_count"] is not None
                else None
            ),
            "cancel_requested_at": row["updated_at"] if row["cancel_requested"] else None,
            "lease_owner": row["lease_owner"] if lease_live else None,
            "lease_expires_at": row["lease_expires_at"] if lease_live else None,
            "heartbeat_at": None,
            "attempts": int(row["attempts"]),
            "error": error if state == "failed" else None,
            "limitations": (
                [f"migrated from the CP6 run in state {row['state']}: {row['error']}"[:2000]]
                if row["error"] and state != "failed"
                else []
            ),
            "artifacts": artifacts,
            "result_seq": None,
            "job_id": None,
            "job_due_utc": None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["updated_at"] if terminal else None,
        },
        strict=True,
    )


def _v8(conn: sqlite3.Connection) -> None:
    from prometheos.store import operational as op

    for statement in V8_STAGES.split(";"):
        if statement.strip():
            conn.execute(statement)
    runs = conn.execute("SELECT * FROM transcription_runs ORDER BY created_at, id").fetchall()
    for row in runs:
        run = _v8_run(conn, row)
        op.insert(conn, "operation_runs", run)
        if row["total_ms"] is not None and row["chunk_count"] is not None:
            # What the decode step established, so a resume does not need to decode again
            # before it knows the chunk plan.
            op.insert(
                conn,
                "operation_run_stages",
                op.OperationRunStage(
                    run_id=run.id,
                    idx=0,
                    name="decode",
                    output={"total_ms": int(row["total_ms"]), "chunk_count": row["chunk_count"]},
                    charged=None,
                    completed_at=row["updated_at"],
                ),
            )
        op.append_run_log(
            conn, run.id, row["updated_at"], "info", f"migrated from CP6 run {row['id']}"
        )
    conn.execute(
        "INSERT INTO transcription_chunks_v8 (run_id, idx, start_ms, end_ms, segments) "
        "SELECT 'oprun_' || run_id, idx, start_ms, end_ms, segments FROM transcription_chunks"
    )
    moved = conn.execute("SELECT COUNT(*) FROM transcription_chunks_v8").fetchone()[0]
    before = conn.execute("SELECT COUNT(*) FROM transcription_chunks").fetchone()[0]
    if moved != before:  # pragma: no cover - defensive: never drop what was not copied
        raise RuntimeError("transcription chunks were not all copied")
    conn.execute("DROP TABLE transcription_chunks")
    conn.execute("DROP TABLE transcription_runs")
    conn.execute("ALTER TABLE transcription_chunks_v8 RENAME TO transcription_chunks")


# A05, CP18.2 (decision 0035): the research event outbox and the detectors' state. Both are
# operational: writing them never advances the project sequence. An event is written in the
# same transaction as the change it reports; ``detector_state`` makes a detector fire once per
# condition across restarts. The migration emits nothing; detectors find existing conditions.
V9_EVENTS = r"""
CREATE TABLE events (
    event_id TEXT PRIMARY KEY CHECK (event_id LIKE 'evt\_%' ESCAPE '\'),
    type TEXT NOT NULL,
    subject_ref TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    project_seq INTEGER NOT NULL CHECK (project_seq >= 0),
    data TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    acknowledged_note TEXT,
    CHECK ((acknowledged_at IS NULL) = (acknowledged_by IS NULL)),
    CHECK (acknowledged_note IS NULL OR acknowledged_at IS NOT NULL)
);
CREATE INDEX events_open ON events(acknowledged_at, occurred_at, event_id);
CREATE INDEX events_subject ON events(json_extract(subject_ref, '$.id'));

CREATE TABLE detector_state (
    detector TEXT NOT NULL,
    condition_key TEXT NOT NULL,
    state TEXT NOT NULL,
    event_id TEXT REFERENCES events(event_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (detector, condition_key)
)
"""


def _v9(conn: sqlite3.Connection) -> None:
    for statement in V9_EVENTS.split(";"):
        if statement.strip():
            conn.execute(statement)


# A05, CP17 (decision 0039 section 2): adoption state. Operational, like the outbox: writing it
# never advances the project sequence, and nothing here is a research record. Paths are
# relative to the adoption root; JSON columns are validated by ``prometheos.adoption.contracts``.
V10_ADOPTION = r"""
CREATE TABLE adoption_scans (
    scan_gen INTEGER PRIMARY KEY CHECK (scan_gen >= 1),
    root_alias TEXT NOT NULL,
    run_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('running', 'complete', 'failed')),
    config TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    root_digest_before TEXT,
    root_digest_after TEXT,
    summary TEXT,
    error TEXT,
    analysis TEXT
);
CREATE INDEX adoption_scans_root ON adoption_scans(root_alias, scan_gen);

CREATE TABLE adoption_files (
    scan_gen INTEGER NOT NULL REFERENCES adoption_scans(scan_gen),
    path TEXT NOT NULL,
    parent TEXT,
    depth INTEGER NOT NULL CHECK (depth >= 0),
    entry TEXT NOT NULL,
    size INTEGER,
    mtime_us INTEGER,
    attributes INTEGER,
    sha256 TEXT,
    text_sha256 TEXT,
    container TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    zone TEXT NOT NULL,
    change TEXT,
    detail TEXT,
    PRIMARY KEY (scan_gen, path)
) WITHOUT ROWID;
CREATE INDEX adoption_files_sha ON adoption_files(sha256);
CREATE INDEX adoption_files_text ON adoption_files(text_sha256);

CREATE TABLE adoption_evidence (
    evidence_id TEXT NOT NULL,
    scan_gen INTEGER NOT NULL REFERENCES adoption_scans(scan_gen),
    analyzer TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    subject TEXT NOT NULL,
    subject_sha256 TEXT,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    locator TEXT,
    quote TEXT,
    quote_sha256 TEXT,
    tier TEXT NOT NULL,
    determinism TEXT NOT NULL,
    PRIMARY KEY (scan_gen, evidence_id)
) WITHOUT ROWID;
CREATE INDEX adoption_evidence_subject ON adoption_evidence(scan_gen, subject);

CREATE TABLE adoption_cache (
    analyzer TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    params_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    output TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (analyzer, analyzer_version, params_sha256, input_sha256)
) WITHOUT ROWID;

CREATE TABLE adoption_families (
    scan_gen INTEGER NOT NULL REFERENCES adoption_scans(scan_gen),
    family_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (scan_gen, family_id)
) WITHOUT ROWID;

CREATE TABLE adoption_plans (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    scan_gen INTEGER NOT NULL REFERENCES adoption_scans(scan_gen),
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, version)
) WITHOUT ROWID;

CREATE TABLE adoption_steps (
    step_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    action_id TEXT,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    result TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX adoption_steps_plan ON adoption_steps(plan_id, plan_version);

CREATE TABLE adoption_baselines (
    baseline_id TEXT PRIMARY KEY,
    scan_gen INTEGER NOT NULL REFERENCES adoption_scans(scan_gen),
    plan_id TEXT,
    plan_version INTEGER,
    accepted_at TEXT NOT NULL,
    accepted_by TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE adoption_assist (
    cache_key TEXT PRIMARY KEY,
    task_kind TEXT NOT NULL,
    response TEXT NOT NULL,
    validation TEXT NOT NULL,
    model TEXT,
    recorded_at TEXT NOT NULL
)
"""


def _v10(conn: sqlite3.Connection) -> None:
    for statement in V10_ADOPTION.split(";"):
        if statement.strip():
            conn.execute(statement)


# IF NOT EXISTS: stores built by this branch's intermediate commits (c52ef86, 6f036fc) already
# have adoption_decisions from their migration 10 (review cp17-5-a-r2 R2-3).
V11_ADOPTION = r"""
CREATE TABLE IF NOT EXISTS adoption_decisions (
    decision_id TEXT PRIMARY KEY,
    root_alias TEXT NOT NULL,
    family_id TEXT NOT NULL,
    current_path TEXT NOT NULL,
    current_sha256 TEXT,
    note TEXT,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS adoption_decisions_family
    ON adoption_decisions(root_alias, family_id, decided_at);

CREATE TABLE IF NOT EXISTS adoption_pruned (
    scan_gen INTEGER PRIMARY KEY REFERENCES adoption_scans(scan_gen),
    pruned_at TEXT NOT NULL
);
"""


def _v11(conn: sqlite3.Connection) -> None:
    for statement in V11_ADOPTION.split(";"):
        if statement.strip():
            conn.execute(statement)


# Decision 0058: events carry their insertion order. ``occurred_at`` has second resolution, so
# events of one second (an approval requested and decided in the same second) had no defined
# order and ``ORDER BY occurred_at, event_id`` broke the tie by a random UUID. ``seq`` is assigned
# in the writing transaction (MAX + 1, unique) and never changes; rowid is not used because VACUUM
# (run by a purge) may renumber it. Existing rows are numbered in their (occurred_at, rowid)
# order, the best order an older store holds.
V12_EVENT_ORDER: tuple[str, ...] = (
    "ALTER TABLE events ADD COLUMN seq INTEGER",
    "UPDATE events SET seq = ordered.n FROM ("
    " SELECT event_id AS id, ROW_NUMBER() OVER (ORDER BY occurred_at, rowid) AS n FROM events"
    ") AS ordered WHERE ordered.id = events.event_id",
    "CREATE UNIQUE INDEX events_seq ON events(seq)",
    "CREATE INDEX events_order ON events(occurred_at, seq)",
    "CREATE INDEX events_open_order ON events(acknowledged_at, occurred_at, seq)",
    "DROP INDEX events_open",  # its (occurred_at, event_id) order is no longer read
    "CREATE TRIGGER events_seq_required BEFORE INSERT ON events WHEN NEW.seq IS NULL "
    "BEGIN SELECT RAISE(ABORT, 'events.seq is required (decision 0058)'); END",
    "CREATE TRIGGER events_seq_fixed BEFORE UPDATE OF seq ON events "
    "BEGIN SELECT RAISE(ABORT, 'events.seq never changes (decision 0058)'); END",
)


def _v12(conn: sqlite3.Connection) -> None:
    for statement in V12_EVENT_ORDER:
        conn.execute(statement)


def _v13(conn: sqlite3.Connection) -> None:
    # CP11 S11.1 (decision 0060): library import plans (operational) and the rebuildable page
    # text and work index with its FTS5 table (prometheos.library.index).
    from prometheos.library.index import migrate_v13

    migrate_v13(conn)


def _v14(conn: sqlite3.Connection) -> None:
    # CP11 S11.2 (decision 0062): every work in the work map, the identifier index, bibliography
    # entry keys (derived) and the owner's dedup decisions (operational).
    from prometheos.library.index import migrate_v14

    migrate_v14(conn)


# A06.1 (CP20.4, decision 0085): the owner's class decisions for adoption families. A decision
# names the family by its key and exact member set (paths and digests), because family IDs are not
# stable across scans; it applies only while both are unchanged. Append-only; the latest wins.
V15_CLASS_DECISIONS = r"""
CREATE TABLE IF NOT EXISTS adoption_class_decisions (
    decision_id TEXT PRIMARY KEY,
    root_alias TEXT NOT NULL,
    family_key TEXT NOT NULL,
    members TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('variant', 'noise', 'split', 'revoked')),
    note TEXT,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS adoption_class_decisions_key
    ON adoption_class_decisions(root_alias, family_key, decided_at);
"""


def _v15(conn: sqlite3.Connection) -> None:
    for statement in V15_CLASS_DECISIONS.split(";"):
        if statement.strip():
            conn.execute(statement)


# A07, CP22 (decision 0092; spec/03 (s)-(w)): the intelligence layer's records in a project
# store. ``ai_policy`` holds the owner's standing policy as revisions bound to the provider
# configuration revision; ``ai_results`` is the rebuildable cache of validated task outputs (never
# authoritative); ``ai_eval_records`` the evaluation records fixed before measuring and
# ``ai_evaluations`` the measurements and the trust they confer; ``root_sensitivity`` the
# owner-set sensitivity of a research root, bound to the folder it resolved to; the provider call
# records gain what a layer call sent and received.
V16_INTELLIGENCE = r"""
CREATE TABLE IF NOT EXISTS ai_policy (
    revision INTEGER PRIMARY KEY,
    policy TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    provider_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('in_force', 'superseded', 'revoked')),
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    ended_by TEXT,
    ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ai_policy_in_force ON ai_policy(state) WHERE state = 'in_force';
CREATE TABLE IF NOT EXISTS ai_results (
    result_key TEXT PRIMARY KEY,
    task_kind TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    template_sha256 TEXT NOT NULL,
    wire_sha256 TEXT NOT NULL,
    model TEXT NOT NULL,
    params_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    sample INTEGER NOT NULL,
    subject TEXT NOT NULL,
    subject_sha256 TEXT,
    executor TEXT NOT NULL CHECK (executor IN ('rules', 'gateway', 'agent', 'cassette')),
    outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'abstained', 'rejected')),
    reasons TEXT NOT NULL,
    output TEXT,
    hint_only INTEGER NOT NULL CHECK (hint_only IN (0, 1)),
    served_model TEXT,
    served_provider TEXT,
    call_id TEXT,
    run_id TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('normal', 'restricted', 'private')),
    scan_gen INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_results_input ON ai_results(task_kind, input_sha256);
CREATE INDEX IF NOT EXISTS ai_results_run ON ai_results(run_id);
CREATE TABLE IF NOT EXISTS ai_eval_records (
    record_sha256 TEXT PRIMARY KEY,
    task_kind TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    record TEXT NOT NULL,
    fixed_by TEXT NOT NULL,
    fixed_at TEXT NOT NULL
);
-- A measurement started on a record (review cp22-a R11-F1): the record is spent when the run
-- begins, so a run that stops part-way has still shown its items.
CREATE TABLE IF NOT EXISTS ai_eval_starts (
    run_id TEXT PRIMARY KEY,
    record_sha256 TEXT NOT NULL,
    executor TEXT NOT NULL,
    started_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_eval_starts_record ON ai_eval_starts(record_sha256);
CREATE TABLE IF NOT EXISTS ai_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    record_sha256 TEXT NOT NULL REFERENCES ai_eval_records(record_sha256),
    task_kind TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    template_sha256 TEXT NOT NULL,
    wire_sha256 TEXT NOT NULL,
    model TEXT NOT NULL,
    provider_set TEXT NOT NULL,
    executor TEXT NOT NULL,
    trusted INTEGER NOT NULL CHECK (trusted IN (0, 1)),
    result TEXT NOT NULL,
    measured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_evaluations_trust ON ai_evaluations(task_kind, task_version, model);
CREATE TABLE IF NOT EXISTS ai_retrieval_records (
    record_sha256 TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    queries_sha256 TEXT NOT NULL UNIQUE,
    record TEXT NOT NULL,
    fixed_by TEXT NOT NULL,
    fixed_at TEXT NOT NULL
);
-- A06.1, CP20.4 (decision 0106): the owner's one confirmation of an adoption outline, with the
-- items kept, their records and the results with their producers.
CREATE TABLE IF NOT EXISTS adoption_outlines (
    outline_id TEXT PRIMARY KEY,
    scan_gen INTEGER NOT NULL,
    root_alias TEXT NOT NULL,
    proposal_sha256 TEXT NOT NULL,
    confirmed TEXT NOT NULL,
    confirmed_sha256 TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    request_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS adoption_outlines_root ON adoption_outlines(root_alias, confirmed_at);
-- Decision 0109 (review map-ai-spread-a R2-F1): each run's own verdict on each subject, with the
-- samples it judged together, so a view never pairs samples no run judged as one set. Rebuildable
-- like ai_results, never authoritative.
CREATE TABLE IF NOT EXISTS ai_verdicts (
    verdict_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    template_sha256 TEXT NOT NULL,
    wire_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    subject TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'rejected', 'abstained', 'disagreed')),
    result_keys TEXT NOT NULL,
    hint_only INTEGER NOT NULL CHECK (hint_only IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_verdicts_input ON ai_verdicts(task_kind, input_sha256, created_at);
CREATE TABLE IF NOT EXISTS root_sensitivity (
    root_alias TEXT PRIMARY KEY,
    folder TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('normal', 'restricted', 'private')),
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL
);
ALTER TABLE provider_calls ADD COLUMN served_model TEXT;
ALTER TABLE provider_calls ADD COLUMN served_provider TEXT;
ALTER TABLE provider_calls ADD COLUMN billed_cost TEXT;
ALTER TABLE provider_calls ADD COLUMN data_policy TEXT;
ALTER TABLE provider_calls ADD COLUMN bundle_digest TEXT;
ALTER TABLE provider_calls ADD COLUMN reply_body TEXT;
ALTER TABLE provider_calls ADD COLUMN reply_sensitivity TEXT
"""


def _v16(conn: sqlite3.Connection) -> None:
    for statement in V16_INTELLIGENCE.split(";"):
        if statement.strip():
            conn.execute(statement)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "core records, sources, requests", _v1),
    Migration(2, "capabilities, derived indexes, review flags, purge", migrate_v2),
    Migration(3, "transition notes", migrate_v3),
    Migration(4, "task contexts", _v4),
    Migration(5, "job slots, scheduler lease, job notifications", _v5),
    Migration(6, "recording sessions, transcription runs, provider settings and calls", _v6),
    Migration(7, "operation runs, approval requests, backup inventory (A02, CP10)", _v7),
    Migration(8, "run stages; transcription runs folded into operation runs (CP10.3)", _v8),
    Migration(9, "research event outbox and detector state (A05, CP18.2)", _v9),
    Migration(10, "adoption scans, inventory, evidence, plans, baselines (A05, CP17)", _v10),
    Migration(11, "adoption owner decisions and pruned generations (A05, CP17.5-6)", _v11),
    Migration(12, "research events keep their insertion order (decision 0058)", _v12),
    Migration(13, "library plans and the derived page index (A02, CP11 S11.1)", _v13),
    Migration(14, "library identifier index, entries and dedup decisions (CP11 S11.2)", _v14),
    Migration(15, "adoption family class decisions (A06.1, CP20.4)", _v15),
    Migration(
        16,
        "the intelligence layer: policy, results, root sensitivity (A07, CP22); adoption "
        "outlines (A06.1, CP20.4)",
        _v16,
    ),
)
