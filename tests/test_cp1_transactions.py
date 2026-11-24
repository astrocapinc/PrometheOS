"""C01-TXN: concurrent/stale writes and duplicate retries have one atomic result (S01)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from cp1_support import AGENT, OWNER, Env, counts, create_entry, entry_body, request

from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def code(exc: pytest.ExceptionInfo[PrometheosError]) -> str:
    return exc.value.code


def test_project_init_is_the_first_commit(env: Env) -> None:
    info = env.svc.project_info(env.project_id, OWNER)
    assert info["project_seq"] == 1
    assert info["settings"]["current_objective"].startswith("Compare")
    assert info["settings"]["allowed_roots"][0]["alias"] == "research"


def test_identical_retry_replays_original_result(env: Env) -> None:
    raw = request(env.project_id, [create_entry(title="Question")], 1)
    first = env.svc.apply(env.project_id, raw)
    before = counts(env.store_dir)
    second = env.svc.apply(env.project_id, raw)
    assert first["replayed"] is False and second["replayed"] is True
    assert {**second, "replayed": False} == first
    assert counts(env.store_dir) == before
    assert env.seq() == 2


def test_same_request_id_with_different_payload_conflicts(env: Env) -> None:
    rid = new_request_id()
    op = create_entry(title="Original")
    env.svc.apply(env.project_id, request(env.project_id, [op], 1, rid))
    before = counts(env.store_dir)
    changed = dict(op, body=entry_body(title="Changed"))
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, request(env.project_id, [changed], 1, rid))
    assert code(err) == "request_key_conflict"
    assert counts(env.store_dir) == before


def test_stale_expected_seq_has_exactly_one_winner(env: Env) -> None:
    a = request(env.project_id, [create_entry(title="A")], 1)
    b = request(env.project_id, [create_entry(title="B")], 1)
    env.svc.apply(env.project_id, a)
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, b)
    assert code(err) == "revision_conflict"
    assert err.value.details == {"expected_seq": 1, "project_seq": 2}
    titles = [
        r["body"]["title"]
        for r in env.svc.list_records(env.project_id, OWNER, kind="entry")["items"]
    ]
    assert titles == ["A"]


def test_failed_request_is_not_saved_and_its_id_stays_usable(env: Env) -> None:
    rid = new_request_id()
    with pytest.raises(PrometheosError):
        env.svc.apply(env.project_id, request(env.project_id, [create_entry()], 99, rid))
    result = env.svc.apply(env.project_id, request(env.project_id, [create_entry()], 1, rid))
    assert result["project_seq"] == 2 and result["replayed"] is False


def test_batch_is_all_or_nothing(env: Env) -> None:
    good = create_entry(title="Would be first")
    missing_source = create_entry(
        title="Cites nothing real",
        evidence=[
            {
                "source_id": new_id("source"),
                "version_id": new_id("version"),
                "locator": {"type": "text", "start_line": 1, "end_line": 1},
                "quote_sha256": None,
            }
        ],
    )
    before = counts(env.store_dir)
    with pytest.raises(PrometheosError) as err:
        env.apply([good, missing_source])
    assert code(err) == "not_found"
    assert err.value.details["op_index"] == 1
    assert counts(env.store_dir) == before
    assert env.seq() == 1


def test_one_sequence_per_request_with_ordered_audit(env: Env) -> None:
    ops = [create_entry(title=f"E{i}") for i in range(3)]
    result = env.apply(ops)
    assert result["project_seq"] == 2
    assert [r["op_index"] for r in result["results"]] == [0, 1, 2]
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    rows = conn.execute("SELECT seq, op_index, operation FROM audit WHERE seq = 2").fetchall()
    conn.close()
    assert rows == [(2, 0, "create"), (2, 1, "create"), (2, 2, "create")]


def test_revision_base_rev_conflict_and_as_known_history(env: Env) -> None:
    eid = new_id("entry")
    env.apply([create_entry(eid, title="Draft")])
    env.apply(
        [
            {
                "op": "revise",
                "id": eid,
                "base_rev": 1,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": entry_body(title="Revised"),
            }
        ]
    )
    with pytest.raises(PrometheosError) as err:
        env.apply(
            [
                {
                    "op": "revise",
                    "id": eid,
                    "base_rev": 1,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": entry_body(title="Lost update"),
                }
            ]
        )
    assert code(err) == "revision_conflict"
    assert env.svc.get(env.project_id, OWNER, eid, None)["body"]["title"] == "Revised"
    assert env.svc.get(env.project_id, OWNER, eid, 2)["body"]["title"] == "Draft"
    with pytest.raises(PrometheosError):
        env.svc.get(env.project_id, OWNER, eid, 1)  # did not exist yet


def test_protected_and_unsupported_routes_refused(env: Env) -> None:
    cases = [
        (
            {
                "op": "create",
                "kind": "version",
                "id": new_id("version"),
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {},
            },
            "policy_denied",
        ),
        (
            {
                "op": "create",
                "kind": "source",
                "id": new_id("source"),
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {},
            },
            "policy_denied",
        ),
        (
            {
                "op": "create",
                "kind": "paper",
                "id": new_id("paper"),
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {},
            },
            "validation_error",  # papers exist since CP7; an empty body is invalid
        ),
        (
            {
                "op": "create",
                "kind": "job",
                "id": new_id("job"),
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {},
            },
            "policy_denied",  # jobs are created only by `job add`
        ),
        (
            {
                "op": "create",
                "kind": "claim",
                "id": new_id("claim"),
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {},
            },
            "validation_error",
        ),
        ({"op": "correction_apply", "id": new_id("correction"), "base_rev": 1}, "not_found"),
    ]
    for op, expected in cases:
        with pytest.raises(PrometheosError) as err:
            env.apply([op])
        assert code(err) == expected, op
    assert env.seq() == 1


def test_lowering_sensitivity_is_refused(env: Env) -> None:
    eid = new_id("entry")
    env.apply([create_entry(eid, sensitivity="restricted")])
    with pytest.raises(PrometheosError) as err:
        env.apply(
            [
                {
                    "op": "revise",
                    "id": eid,
                    "base_rev": 1,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": entry_body(),
                }
            ],
            caller=OWNER,
        )
    assert code(err) == "approval_required"  # CP10.2: the typed refusal; nothing committed


def test_request_size_and_schema_limits(env: Env) -> None:
    huge = create_entry(text="x" * 60000)
    raw = request(env.project_id, [huge] * 150, 1)
    assert len(raw) > 8 * 1024 * 1024
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, raw)
    assert code(err) == "budget_exceeded"
    with pytest.raises(PrometheosError) as err:
        env.apply([create_entry(title="t" * 301)])
    assert code(err) == "validation_error"
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, b'{"schema_version":1,"schema_version":1}')
    assert code(err) == "validation_error"


def test_other_project_request_rejected(env: Env) -> None:
    raw = request(new_id("project"), [create_entry()], 1)
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, raw)
    assert code(err) == "validation_error"


def test_agent_scope_conceals_restricted_records(env: Env) -> None:
    hidden, visible = new_id("entry"), new_id("entry")
    env.apply(
        [
            create_entry(hidden, sensitivity="restricted", title="Canary-7781"),
            create_entry(visible, title="Ordinary"),
        ]
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.get(env.project_id, AGENT, hidden, None)
    assert code(err) == "not_found"
    with pytest.raises(PrometheosError) as err_missing:
        env.svc.get(env.project_id, AGENT, new_id("entry"), None)
    assert err.value.message == err_missing.value.message  # existence not revealed
    listed = env.svc.list_records(env.project_id, AGENT, kind="entry")
    assert [r["id"] for r in listed["items"]] == [visible]
    assert "Canary-7781" not in json.dumps(listed)
    assert env.svc.get(env.project_id, OWNER, hidden, None)["body"]["title"] == "Canary-7781"


def test_cursor_bound_to_filters_and_snapshot(env: Env) -> None:
    env.apply([create_entry(title=f"E{i}") for i in range(5)])
    page = env.svc.list_records(env.project_id, OWNER, kind="entry", limit=2)
    assert len(page["items"]) == 2 and page["next_cursor"]
    env.apply([create_entry(title="Later")])  # snapshot keeps later records out
    seen = [r["id"] for r in page["items"]]
    cursor = page["next_cursor"]
    while cursor:
        page = env.svc.list_records(env.project_id, OWNER, kind="entry", limit=2, cursor=cursor)
        seen += [r["id"] for r in page["items"]]
        cursor = page["next_cursor"]
    assert len(seen) == 5 and len(set(seen)) == 5
    first = env.svc.list_records(env.project_id, OWNER, kind="entry", limit=2)
    with pytest.raises(PrometheosError) as err:
        env.svc.list_records(env.project_id, OWNER, kind=None, limit=2, cursor=first["next_cursor"])
    assert code(err) == "validation_error"
    with pytest.raises(PrometheosError):
        env.svc.list_records(env.project_id, OWNER, cursor="not-a-cursor")


@pytest.mark.slow
def test_concurrent_processes_same_expected_seq_one_winner(env: Env, tmp_path: Path) -> None:
    files = []
    for i in range(6):
        path = tmp_path / f"req{i}.json"
        path.write_bytes(request(env.project_id, [create_entry(title=f"P{i}")], 1))
        files.append(path)
    import subprocess
    import sys

    from cp1_support import WORKER

    envvars = {**__import__("os").environ, "PROMETHEOS_HOME": str(env.home)}
    procs = [
        subprocess.Popen(
            [sys.executable, str(WORKER), "apply", env.project_id, str(f)],
            env=envvars,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for f in files
    ]
    outcomes = [json.loads(p.communicate(timeout=120)[0].strip().splitlines()[-1]) for p in procs]
    winners = [o for o in outcomes if o["ok"]]
    losers = [o for o in outcomes if not o["ok"]]
    assert len(winners) == 1
    assert {o["error"]["error"]["code"] for o in losers} <= {"revision_conflict", "busy"}
    assert env.seq() == 2
    assert len(env.svc.list_records(env.project_id, OWNER, kind="entry")["items"]) == 1


@pytest.mark.slow
def test_concurrent_writers_with_refresh_lose_nothing(env: Env, tmp_path: Path) -> None:
    import subprocess
    import sys

    from cp1_support import WORKER

    start = tmp_path / "go"
    envvars = {**__import__("os").environ, "PROMETHEOS_HOME": str(env.home)}
    procs = [
        subprocess.Popen(
            [sys.executable, str(WORKER), "race", env.project_id, str(start), "5"],
            env=envvars,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    time.sleep(1.0)
    start.write_text("go", encoding="utf-8")
    results = [p.communicate(timeout=300) for p in procs]
    for out, err in results:
        assert json.loads(out.strip().splitlines()[-1])["ok"], err
    assert env.seq() == 1 + 20
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    seqs = [r[0] for r in conn.execute("SELECT DISTINCT seq FROM audit ORDER BY seq")]
    conn.close()
    assert seqs == list(range(1, 22))
    assert len(env.svc.list_records(env.project_id, OWNER, kind="entry", limit=200)["items"]) == 20
