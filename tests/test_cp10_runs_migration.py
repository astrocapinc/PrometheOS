"""CP10.3 forward migration (store schema 7 -> 8; decision 0017).

CP6 ``transcription_runs`` become ``operation_runs`` (the single run of record): each keeps its
UUID as ``oprun_<UUID>``, its state (``pending`` -> ``queued``), inputs, configuration digest,
rank, attempts, lease, cancel request, error and result; its stored chunks follow it as stage
detail. The migration runs only after a verified backup, and a migrated unfinished run resumes
from its stored chunks and commits under the same request key it would have used before.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from audio_support import AudioEnv
from cp1_support import AGENT, OWNER, OWNER_ACT

from prometheos.app import sources
from prometheos.audio import engines, transcribe
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.ids import derived_request_id
from prometheos.store import db
from prometheos.store import operational as op
from prometheos.store.backup import semantic_snapshot
from prometheos.store.schema import MIGRATIONS

PREVIOUS = 7  # CP10.1 operation runs, approval requests, backup inventory


def user_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _schema7_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AudioEnv, str]:
    """A schema-7 store with one audio version, built with the low-level import (the
    schema-7 application stored no operation runs for imports)."""
    from prometheos.store import project

    with monkeypatch.context() as patch:
        patch.setattr(project, "MIGRATIONS", MIGRATIONS[:PREVIOUS])
        env = AudioEnv(tmp_path)
        env.audio_file("meeting.wav")
        store = env.svc.open(env.project_id)
        plan = sources.create_plan(
            store, root_alias="research", paths=["meeting.wav"], retention="copy",
            sensitivity="normal", max_rank=2,
        )  # fmt: skip
        sources.commit_plan(store, plan["plan_id"], "test", max_rank=2)
        version_id = plan["items"][0]["version_id"]
    return env, version_id


def _cp6_rows(env: AudioEnv, version_id: str) -> dict[str, str]:
    """CP6 transcription runs in every state, written as the CP6 code wrote them."""
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        sha = conn.execute(
            "SELECT b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
            "WHERE v.record_id = ?",
            (version_id,),
        ).fetchone()[0]
        sha = json.loads(sha)["sha256"]
        config = env.synthetic_config()
        document = config.document("1")
        digest = canonical_sha256(document)
        ids = {
            "pending": "00000000-0000-4000-8000-0000000000c1",
            "running": "00000000-0000-4000-8000-0000000000c2",
            "failed": "00000000-0000-4000-8000-0000000000c3",
            "completed": "00000000-0000-4000-8000-0000000000c4",
            "cancelled": "00000000-0000-4000-8000-0000000000c5",
        }
        at = "2026-10-01T10:00:00.000000Z"
        for state, run_id in ids.items():
            conn.execute(
                "INSERT INTO transcription_runs (id, version_id, input_sha256, config_digest, "
                "config, state, total_ms, next_chunk, chunk_count, lease_owner, "
                "lease_expires_at, cancel_requested, attempts, max_rank, error, "
                "result_version_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, version_id, sha, digest, json.dumps(document, sort_keys=True),
                    state,
                    8000 if state in ("running", "failed", "completed") else None,
                    1 if state in ("running", "failed") else (3 if state == "completed" else 0),
                    3 if state in ("running", "failed", "completed") else None,
                    "cp6-worker:1234" if state == "running" else None,
                    "2026-10-01T10:02:00.000000Z" if state == "running" else None,
                    1 if state == "cancelled" else 0,
                    2 if state in ("running", "failed") else 1,
                    2 if state == "completed" else 0,
                    "RuntimeError: decoder blew up" if state == "failed" else None,
                    "version_00000000-0000-4000-8000-0000000000c9"
                    if state == "completed"
                    else None,
                    at, at,
                ),
            )  # fmt: skip
        chunk = json.dumps(
            {
                "language": None,
                "segments": [
                    {"start_ms": 0, "end_ms": 2000, "text": "Compare the controllers under "
                     "actuator limits.", "confidence": 0.99, "uncertain": False,
                     "speaker_label": None}
                ],
            }
        )  # fmt: skip
        for run_key in ("running", "failed"):
            conn.execute(
                "INSERT INTO transcription_chunks (run_id, idx, start_ms, end_ms, segments) "
                "VALUES (?,?,?,?,?)",
                (ids[run_key], 0, 0, 4000, chunk),
            )
        conn.commit()
    finally:
        conn.close()
    return ids


def test_the_cp10_3_migration_is_version_eight() -> None:
    # Pinned by position: later checkpoints append (CP18.2, decision 0035: version 9).
    assert MIGRATIONS[7].version == 8
    assert "transcription runs folded into operation runs" in MIGRATIONS[7].name


def test_cp6_transcription_runs_become_operation_runs_without_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    env, version_id = _schema7_store(tmp_path, monkeypatch)
    ids = _cp6_rows(env, version_id)
    db_path = env.store_dir / "record.sqlite"
    assert user_version(db_path) == PREVIOUS
    before = semantic_snapshot(env.store_dir)

    result = env.svc.migrate(env.project_id, authority=OWNER_ACT)
    assert result["from_schema"] == PREVIOUS
    assert result["applied"] == list(range(8, len(MIGRATIONS) + 1))  # 8 and later
    backup = Path(result["backup"]["path"])
    assert result["backup"]["verified"]
    with zipfile.ZipFile(backup) as archive:
        (tmp_path / "old.sqlite").write_bytes(archive.read("record.sqlite"))
    with closing(sqlite3.connect(tmp_path / "old.sqlite")) as old:
        assert old.execute("SELECT COUNT(*) FROM transcription_runs").fetchone()[0] == 5
    assert user_version(tmp_path / "old.sqlite") == PREVIOUS
    assert semantic_snapshot(env.store_dir) == before  # research content untouched

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "transcription_runs" not in tables and "operation_run_stages" in tables
        migrated = {
            r["id"]: op.from_row(op.OperationRun, r)
            for r in conn.execute("SELECT * FROM operation_runs")
        }
        chunks = conn.execute("SELECT run_id, idx FROM transcription_chunks").fetchall()
        stages = {
            (r["run_id"], r["name"]): json.loads(r["output"])
            for r in conn.execute("SELECT * FROM operation_run_stages")
        }
        fks = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert fks == []
    expected_states = {
        "pending": "queued", "running": "running", "failed": "failed",
        "completed": "completed", "cancelled": "cancelled",
    }  # fmt: skip
    assert set(migrated) == {f"oprun_{u}" for u in ids.values()}
    for cp6_state, uuid in ids.items():
        run = migrated[f"oprun_{uuid}"]
        assert run.operation == "audio_transcribe" and run.state == expected_states[cp6_state]
        assert run.params["version_id"] == version_id
        assert run.input_digests[0].name == "audio"
        assert run.workflow_version == "transcribe-v1" and run.model == "synthetic"
    running = migrated[f"oprun_{ids['running']}"]
    assert running.lease_owner == "cp6-worker:1234" and running.attempts == 2
    assert running.progress is not None and running.progress.done == 1
    failed = migrated[f"oprun_{ids['failed']}"]
    assert failed.error is not None and failed.error.message == "RuntimeError: decoder blew up"
    completed = migrated[f"oprun_{ids['completed']}"]
    assert completed.caller_rank == 2 and completed.finished_at is not None
    assert [a.model_dump() for a in completed.artifacts] == [
        {"id": "version_00000000-0000-4000-8000-0000000000c9", "rev": 1, "role": "transcript"}
    ]
    assert migrated[f"oprun_{ids['cancelled']}"].cancel_requested_at is not None
    assert sorted((r["run_id"], r["idx"]) for r in chunks) == sorted(
        [(f"oprun_{ids['failed']}", 0), (f"oprun_{ids['running']}", 0)]
    )
    assert stages[(f"oprun_{ids['running']}", "decode")] == {"total_ms": 8000, "chunk_count": 3}

    # A migrated unfinished run resumes from its stored chunk and commits once, under the
    # request key the CP6 run would have used.
    status = env.svc.transcription_status(env.project_id, AGENT, ids["pending"])
    assert status["run_id"] == f"oprun_{ids['pending']}" and status["state"] == "queued"
    seq = env.seq()
    store = env.svc.open(env.project_id)
    conn = store.connect()
    try:
        with db.write_transaction(conn):  # the CP6 worker's lease has long expired
            conn.execute(
                "UPDATE operation_runs SET lease_expires_at = '2000-01-01T00:00:00.000000Z' "
                "WHERE id = ?",
                (f"oprun_{ids['running']}",),
            )
    finally:
        conn.close()
    seen: list[int] = []
    finished = transcribe.execute(
        store, env.svc.home, ids["running"], owner="after-migration", after_chunk=seen.append
    )
    assert finished["state"] == "completed" and seen == [1, 2]  # chunk 0 came from CP6
    assert env.seq() == seq + 1
    key = derived_request_id("transcript", env.project_id, ids["running"])
    with closing(sqlite3.connect(db_path)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM requests WHERE request_id = ?", (key,)).fetchone()[0]
            == 1
        )
    retried = env.svc.transcribe_start(env.project_id, OWNER, version_id, env.synthetic_config())
    # Starting the same settings again finds an unfinished migrated run instead of a new one.
    unfinished = {f"oprun_{ids['pending']}", f"oprun_{ids['failed']}"}
    assert retried["run_id"] in unfinished and retried["resumed"] is True


def test_a_failed_backup_leaves_schema_seven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.domain.models import PrometheosError
    from prometheos.store import backup as backup_mod

    env, version_id = _schema7_store(tmp_path, monkeypatch)
    _cp6_rows(env, version_id)

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise PrometheosError("io_error", "simulated backup failure", {})

    monkeypatch.setattr(backup_mod, "create_backup", broken)
    with pytest.raises(PrometheosError):
        env.svc.migrate(env.project_id, authority=OWNER_ACT)
    db_path = env.store_dir / "record.sqlite"
    assert user_version(db_path) == PREVIOUS
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transcription_runs").fetchone()[0] == 5
