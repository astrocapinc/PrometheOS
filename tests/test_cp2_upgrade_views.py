"""Schema upgrade from the CP1 store format, next steps/agenda, and notebook after restore."""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, FULL, OWNER, OWNER_ACT, Env, create_entry, request

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError


def _schema_v1_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """A store written by CP1 code paths: schema version 1, no derived indexes."""
    from prometheos.app import sources
    from prometheos.store import derived, project, schema

    with monkeypatch.context() as patch:
        patch.setattr(project, "MIGRATIONS", schema.MIGRATIONS[:1])
        patch.setattr(derived, "index_record", lambda *a, **k: None)
        patch.setattr(derived, "clear_flags", lambda *a, **k: None)
        patch.setattr(derived, "index_text", lambda *a, **k: None)
        patch.setattr(sources.derived, "index_text", lambda *a, **k: None)
        env = Env(tmp_path)
        # The low-level import: schema 1 had no operation runs (decision 0017).
        store = env.svc.open(env.project_id)
        plan = sources.create_plan(
            store, root_alias="research", paths=["baseline.md"], retention="copy",
            sensitivity="normal", max_rank=0,
        )  # fmt: skip
        sources.commit_plan(store, plan["plan_id"], "test", max_rank=0)
        env.apply([create_entry(title="Written under schema one", text="zebra crossing note")])
    return env


def test_schema_one_store_upgrades_after_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _schema_v1_env(tmp_path, monkeypatch)
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()
    with pytest.raises(PrometheosError) as err:
        env.svc.project_info(env.project_id, OWNER)
    assert err.value.code == "unsupported" and "migrate" in err.value.message

    result = env.svc.migrate(env.project_id, authority=OWNER_ACT)
    from prometheos.store.schema import MIGRATIONS

    latest = len(MIGRATIONS)
    assert result["from_schema"] == 1 and result["to_schema"] == latest
    assert result["applied"] == list(range(2, latest + 1))
    backup = Path(result["backup"]["path"])
    assert result["backup"]["verified"] and backup.parent == env.store_dir / "exports"
    with zipfile.ZipFile(backup) as archive:
        old_db = tmp_path / "old.sqlite"
        old_db.write_bytes(archive.read("record.sqlite"))
    old = sqlite3.connect(old_db)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1
    old.close()
    # Existing content is indexed by the migration's rebuild.
    hits = env.svc.search(env.project_id, AGENT, "zebra")["hits"]
    assert [h["title"] for h in hits] == ["Written under schema one"]
    assert env.svc.search(env.project_id, AGENT, "SIMPLIFIED")["hits"]
    assert env.svc.migrate(env.project_id, authority=OWNER_ACT)["applied"] == []


def test_failed_pre_migration_backup_leaves_old_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.store import backup as backup_mod

    env = _schema_v1_env(tmp_path, monkeypatch)

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise PrometheosError("io_error", "simulated backup failure", {})

    monkeypatch.setattr(backup_mod, "create_backup", broken)
    with pytest.raises(PrometheosError):
        env.svc.migrate(env.project_id, authority=OWNER_ACT)
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _action(env: Env, title: str, **body: Any) -> str:
    aid = new_id("action")
    fields = {
        "title": title,
        "reason": "test",
        "source_refs": [],
        "owner_ref": None,
        "due_at": None,
        "dependencies": [],
        "status": "proposed",
        "blocker": None,
        "completion_evidence": [],
        "commitment_item_id": None,
    }
    fields.update(body)
    env.apply(
        [
            {
                "op": "create",
                "kind": "action",
                "id": aid,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": fields,
            }
        ]
    )
    return aid


def _protected(env: Env, ops: list[dict[str, Any]]) -> dict[str, Any]:
    raw = request(env.project_id, ops, env.seq())
    digest = env.svc.review_request(env.project_id, raw)["digest"]
    grant = env.svc.grant(
        env.project_id,
        operation="exact_request",
        digest=digest,
        label="SYNTHETIC-TEST",
        synthetic=True,
    )
    return env.svc.apply(env.project_id, raw, capability=grant.secret)


def test_next_steps_order_and_agenda(env: Env) -> None:
    blocked = _action(env, "Run the plant comparison")
    overdue = _action(env, "Send figure draft", due_at="2026-01-01T00:00:00Z")
    _protected(
        env,
        [
            {
                "op": "transition",
                "id": blocked,
                "base_rev": 1,
                "to": "ready",
                "reason": "adopt",
                "evidence": [],
            },
            {
                "op": "transition",
                "id": overdue,
                "base_rev": 1,
                "to": "ready",
                "reason": "adopt",
                "evidence": [],
            },
        ],
    )
    env.apply(
        [
            {
                "op": "transition",
                "id": blocked,
                "base_rev": 2,
                "to": "blocked",
                "reason": "Waiting for MATLAB licence",
                "evidence": [],
            }
        ]
    )
    nxt = env.svc.next_steps(env.project_id, AGENT)["candidates"]
    kinds = [c["kind"] for c in nxt]
    assert kinds[0] == "remove_blocker" and "Waiting for MATLAB licence" in nxt[0]["why_now"]
    assert kinds[1] == "due_adopted_work" and nxt[1]["adopted_action_id"] == overdue
    for candidate in nxt:
        assert {
            "why_now",
            "source_refs",
            "expected_output",
            "dependencies",
            "effort",
            "would_change",
        } <= candidate.keys()
    # Done requires completion evidence; the agenda reports it for prior commitments.
    item = env.import_files(["plant-result.md"])["plan"]["items"][0]
    ev = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": 1, "end_line": 1},
        "quote_sha256": None,
    }
    env.apply(
        [
            {
                "op": "transition",
                "id": overdue,
                "base_rev": 2,
                "to": "in_progress",
                "reason": None,
                "evidence": [],
            }
        ]
    )
    with pytest.raises(PrometheosError):
        env.apply(
            [
                {
                    "op": "transition",
                    "id": overdue,
                    "base_rev": 3,
                    "to": "done",
                    "reason": None,
                    "evidence": [],
                }
            ]
        )
    env.apply(
        [
            {
                "op": "transition",
                "id": overdue,
                "base_rev": 3,
                "to": "done",
                "reason": None,
                "evidence": [ev],
            }
        ]
    )
    meeting = env.svc.capture_meeting(
        env.project_id, title="Next supervision", text="Agenda notes\n", held_at=None
    )["meeting_id"]
    agenda = env.svc.agenda(env.project_id, AGENT, meeting)
    assert agenda["prior_commitments"] == []  # none came from an earlier meeting's items


def test_dependency_waiver_and_cycles(env: Env) -> None:
    first = _action(env, "First")
    second = _action(env, "Second", dependencies=[first])
    with pytest.raises(PrometheosError):  # waiver needs a reason
        _protected(
            env,
            [
                {
                    "op": "transition",
                    "id": second,
                    "base_rev": 1,
                    "to": "ready",
                    "reason": None,
                    "evidence": [],
                }
            ],
        )
    preview = env.svc.review_request(
        env.project_id,
        request(
            env.project_id,
            [
                {
                    "op": "transition",
                    "id": second,
                    "base_rev": 1,
                    "to": "ready",
                    "reason": "Owner waives: first is being redone separately",
                    "evidence": [],
                }
            ],
            env.seq(),
        ),
    )
    assert "dependency waiver" in preview["protected_effects"]
    with pytest.raises(PrometheosError):  # a cycle through revision is refused
        body = env.svc.get(env.project_id, OWNER, first, None)["body"]
        env.apply(
            [
                {
                    "op": "revise",
                    "id": first,
                    "base_rev": 1,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": dict(body, dependencies=[second]),
                }
            ]
        )


def test_notebook_equal_after_backup_and_restore(env: Env, tmp_path: Path) -> None:
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    ev = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": 1, "end_line": 1},
        "quote_sha256": None,
    }
    env.apply([create_entry(title="Finding", category="finding", evidence=[ev])])
    before = env.svc.notebook(env.project_id, AGENT)
    backup = Path(env.svc.backup(env.project_id, tmp_path / "nb.zip", FULL)["backup"])
    env.svc.restore(backup, tmp_path / "restored", caller=FULL)
    after = env.svc.notebook(env.project_id, AGENT)
    assert after == before
    assert (
        env.svc.context(env.project_id, AGENT)["manifest"]["project_seq"] == before["project_seq"]
    )
