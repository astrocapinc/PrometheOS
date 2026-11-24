"""C01-RECOVERY: process death before/after commit and during media staging leaves no
partial references or duplicate mutations (S03).

Each probe runs a separate process that terminates abruptly (os._exit, no cleanup)
at a named point, then inspects the store from a fresh process.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cp1_support import FULL, WORKER, Env, counts, create_entry, request

from prometheos.domain.models import PrometheosError
from prometheos.faults import FAULT_EXIT_CODE

pytestmark = pytest.mark.slow


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    from prometheos.app import sources

    # Force media staging for binary files so the blob path is exercised.
    monkeypatch.setattr(sources, "INLINE_LIMIT", 1024)
    return Env(tmp_path)


def integrity(store: Path) -> str:
    conn = sqlite3.connect(store / "record.sqlite")
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def last_json(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_kill_before_commit_exposes_nothing_and_retry_commits_once(
    env: Env, tmp_path: Path
) -> None:
    req = tmp_path / "req.json"
    req.write_bytes(request(env.project_id, [create_entry(title="Before kill")], 1))
    before = counts(env.store_dir)
    killed = env.worker("apply", env.project_id, str(req), fault="commit_before")
    assert killed.returncode == FAULT_EXIT_CODE, killed.stderr
    assert integrity(env.store_dir) == "ok"
    assert counts(env.store_dir) == before
    assert env.seq() == 1
    retry = env.worker("apply", env.project_id, str(req))
    assert retry.returncode == 0, retry.stderr
    assert last_json(retry)["result"]["replayed"] is False
    assert env.seq() == 2


def test_kill_after_commit_retry_replays_without_duplicate(env: Env, tmp_path: Path) -> None:
    req = tmp_path / "req.json"
    req.write_bytes(request(env.project_id, [create_entry(title="After kill")], 1))
    killed = env.worker("apply", env.project_id, str(req), fault="commit_after")
    assert killed.returncode == FAULT_EXIT_CODE
    assert killed.stdout == ""  # the client never received a response
    after_kill = counts(env.store_dir)
    assert env.seq() == 2
    retry = env.worker("apply", env.project_id, str(req))
    assert last_json(retry)["result"]["replayed"] is True
    assert last_json(retry)["result"]["project_seq"] == 2
    assert counts(env.store_dir) == after_kill


@pytest.mark.parametrize(
    "fault", ["import_after_stage", "import_after_blob", "import_before_commit"]
)
def test_kill_during_media_import_leaves_no_partial_reference(env: Env, fault: str) -> None:
    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["artifact.bin"],
        retention="copy",
        sensitivity="normal",
    )
    item = plan["items"][0]
    assert item["storage"] == "media"
    before = counts(env.store_dir)
    killed = env.worker("import-commit", env.project_id, plan["plan_id"], fault=fault)
    assert killed.returncode == FAULT_EXIT_CODE, killed.stderr
    assert integrity(env.store_dir) == "ok"
    assert counts(env.store_dir) == before  # no version/media row references anything
    report = env.svc.orphans(env.project_id, collect=False)["orphans"]
    areas = {o["area"] for o in report}
    assert areas == ({"staging"} if fault == "import_after_stage" else {"media"})
    assert not any(o["removed"] for o in report)

    retry = env.worker("import-commit", env.project_id, plan["plan_id"])
    assert retry.returncode == 0, retry.stderr
    committed = last_json(retry)["result"]
    assert committed["replayed"] is False and committed["project_seq"] == 2
    blob = env.store_dir / "media" / item["sha256"][:2] / item["sha256"]
    assert blob.read_bytes() == (env.research / "artifact.bin").read_bytes()
    # Media blob is now referenced; only a stale staging file (if any) remains as orphan.
    remaining = env.svc.orphans(env.project_id, collect=False)["orphans"]
    assert {o["area"] for o in remaining} <= {"staging"}
    kept = env.svc.orphans(env.project_id, collect=True, grace_seconds=3600)["orphans"]
    assert not any(o["removed"] for o in kept)  # grace period protects recent files
    collected = env.svc.orphans(env.project_id, collect=True, grace_seconds=0)["orphans"]
    assert all(o["removed"] for o in collected)
    assert env.svc.orphans(env.project_id, collect=False)["orphans"] == []
    assert blob.is_file()
    again = env.worker("import-commit", env.project_id, plan["plan_id"])
    assert last_json(again)["result"]["replayed"] is True


def test_killed_lock_holder_releases_maintenance_lock(env: Env, tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    import os

    holder = subprocess.Popen(
        [sys.executable, str(WORKER), "hold-lock", str(env.store_dir), "60", str(ready)],
        env={**os.environ, "PROMETHEOS_HOME": str(env.home)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert time.monotonic() < deadline, "lock holder did not start"
            time.sleep(0.05)
        started = time.monotonic()
        with pytest.raises(PrometheosError) as err:
            env.svc.backup(env.project_id, tmp_path / "blocked.zip", FULL)
        assert err.value.code == "busy"
        assert time.monotonic() - started < 15  # bounded acquisition
        assert not (tmp_path / "blocked.zip").exists()
    finally:
        holder.kill()  # TerminateProcess: no cleanup in the holder
        holder.communicate(timeout=30)
    result = env.svc.backup(env.project_id, tmp_path / "after.zip", FULL)
    assert Path(result["backup"]).is_file()


def test_kill_during_backup_snapshot_leaves_store_usable(env: Env, tmp_path: Path) -> None:
    out = tmp_path / "partial.zip"
    killed = env.worker("backup", env.project_id, str(out), fault="backup_in_snapshot")
    assert killed.returncode == FAULT_EXIT_CODE
    assert not out.exists()
    assert integrity(env.store_dir) == "ok"
    env.apply([create_entry(title="Writable after crashed backup")])
    assert env.seq() == 2
    result = env.svc.backup(env.project_id, tmp_path / "complete.zip", FULL)
    assert env.svc.verify_backup(Path(result["backup"]))["verified"]
    stale = env.svc.orphans(env.project_id, collect=False)["orphans"]
    assert {o["area"] for o in stale} <= {"staging"}


def test_kill_during_restore_leaves_registry_and_target_untouched(env: Env, tmp_path: Path) -> None:
    backup = Path(env.svc.backup(env.project_id, tmp_path / "b.zip", FULL)["backup"])
    target = tmp_path / "restored"
    script = (
        "import sys; from pathlib import Path; from prometheos.app.service import Service; "
        "from prometheos.app.reads import Caller; "
        f"Service().restore(Path(r'{backup}'), Path(r'{target}'), "
        "caller=Caller('test', 'owner', True))"
    )
    import os

    proc = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "PROMETHEOS_HOME": str(env.home),
            "PROMETHEOS_FAULT_POINT": "restore_before_switch",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == FAULT_EXIT_CODE, proc.stderr
    assert not target.exists()
    assert env.svc.registry.lookup(env.project_id) == env.store_dir
