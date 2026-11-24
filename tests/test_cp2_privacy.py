"""C02-PRIVACY: restricted content stays out of agent egress and logs; purge removes live
copies (S08)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from cp1_support import AGENT, FULL, OWNER, Env, create_entry

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError

CANARY = "CANARY-PRIV-55K"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    (e.research / "private-notes.txt").write_text(
        f"Interview note: {CANARY} mentioned the prototype.\nSecond line.\n", encoding="utf-8"
    )
    return e


def restricted_setup(env: Env) -> dict:
    item = env.import_files(["private-notes.txt"], sensitivity="restricted")["plan"]["items"][0]
    derived = new_id("entry")
    ev = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": 1, "end_line": 1},
        "quote_sha256": None,
    }
    env.apply(
        [
            create_entry(
                derived,
                sensitivity="restricted",
                title="Derived summary",
                text=f"The participant said {CANARY}.",
                evidence=[ev],
            )
        ],
        caller=OWNER,
    )
    return {"item": item, "derived": derived}


def everything_agent_can_read(env: Env) -> str:
    pid = env.project_id
    views = [
        env.svc.context(pid, AGENT),
        env.svc.notebook(pid, AGENT),
        env.svc.search(pid, AGENT, CANARY)["hits"],  # the response echoes the query itself
        env.svc.search(pid, AGENT, "participant")["hits"],
        env.svc.list_records(pid, AGENT, limit=200, include_archived=True),
        env.svc.next_steps(pid, AGENT),
        env.svc.project_info(pid, AGENT),
    ]
    return json.dumps(views)


def test_no_default_egress_of_restricted_content(env: Env) -> None:
    ids = restricted_setup(env)
    assert CANARY not in everything_agent_can_read(env)
    for rid in (ids["derived"], ids["item"]["version_id"], ids["item"]["source_id"]):
        with pytest.raises(PrometheosError) as err:
            env.svc.get(env.project_id, AGENT, rid, None)
        assert err.value.code == "not_found"
        with pytest.raises(PrometheosError):
            env.svc.links(env.project_id, AGENT, rid)
    # Owner scope sees it, through the same read paths.
    assert CANARY in json.dumps(env.svc.search(env.project_id, OWNER, CANARY))


def test_audit_and_request_logs_hold_no_content(env: Env) -> None:
    restricted_setup(env)
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        audit = json.dumps(conn.execute("SELECT * FROM audit").fetchall())
        requests = json.dumps(conn.execute("SELECT * FROM requests").fetchall())
    finally:
        conn.close()
    assert CANARY not in audit and CANARY not in requests


def test_purge_removes_live_copies_and_reports_limits(env: Env) -> None:
    ids = restricted_setup(env)
    other = env.import_files(["baseline.md"])["plan"]["items"][0]
    backup = env.svc.backup(env.project_id, env.tmp / "pre-purge.zip", FULL)
    plan = env.svc.purge_plan(env.project_id, [ids["item"]["source_id"]], OWNER)
    assert set(plan["closure"]) == {
        ids["item"]["source_id"],
        ids["item"]["version_id"],
        ids["derived"],
    }
    assert other["version_id"] not in plan["closure"]
    with pytest.raises(PrometheosError) as err:
        env.svc.purge_commit(env.project_id, plan["plan_id"], None)
    assert err.value.code == "policy_denied"
    wrong = env.svc.grant(
        env.project_id, operation="purge", digest="0" * 64, label="SYNTHETIC-TEST"
    )
    with pytest.raises(PrometheosError):
        env.svc.purge_commit(env.project_id, plan["plan_id"], wrong.secret)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    first = env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    again = env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    assert again["commit"]["replayed"] is True
    receipt = first["purge_receipt"]
    assert receipt["records_purged"] == 3 and receipt["payloads_removed"] == 1
    assert CANARY not in json.dumps(first)
    for path in env.store_dir.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert CANARY.encode() not in data, path
            assert b"55k" not in data.lower(), path  # tokenised remnants in the FTS index
    assert env.svc.search(env.project_id, OWNER, CANARY)["hits"] == []
    tomb = env.svc.get(env.project_id, OWNER, ids["derived"], None)
    assert tomb["lifecycle"] == "deleted" and tomb["body"] == {
        "purged": True,
        "purged_at_seq": receipt["project_seq"],
    }
    # Unrelated retained bytes survive.
    assert (
        env.svc.get(env.project_id, OWNER, other["version_id"], None)["body"]["availability"]
        == "retained"
    )
    # The pre-purge backup still holds it; the receipt says so rather than pretending.
    import zipfile

    with zipfile.ZipFile(backup["backup"]) as archive:
        assert CANARY.encode() in archive.read("record.sqlite")
    assert any("Backups" in line for line in receipt["limitations"])


def test_purge_plan_goes_stale_when_project_moves(env: Env) -> None:
    ids = restricted_setup(env)
    plan = env.svc.purge_plan(env.project_id, [ids["item"]["source_id"]], OWNER)
    env.apply([create_entry(title="later")])
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    assert err.value.code == "revision_conflict"
