"""Regressions for the CP17.5 pre-check (review-precheck-cp17-5-a, F1-F7): owner decisions
survive a family's change of ID, results and decisions reopen when what they were about
changes, reported results cannot be fabricated, a re-run that does not reproduce adds the
compare and recompute steps, the plan never silently drops kinds, and the store migrates."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from cp1_support import AGENT, OWNER, OWNER_ACT
from test_cp17_review_r2 import GOOD, LOG, SWEEPS, _send, family_with, scan_tree

from prometheos.adoption import scan as scan_mod


def _rescan(world: dict[str, Any]) -> int:
    env = world["env"]
    original = scan_mod.STAT_PROVIDER
    try:
        scan = env.svc.adoption_scan(env.project_id, AGENT, root_alias="tree", mode="wait")
    finally:
        scan_mod.STAT_PROVIDER = original
    assert scan["state"] == "complete"
    world["gen"] = scan["scan_gen"]
    return int(scan["scan_gen"])


def _steps(world: dict[str, Any]) -> dict[str, dict[str, Any]]:
    env = world["env"]
    plan = env.svc.adoption_plan(env.project_id, OWNER, world["gen"])
    return {s["step"]["step_id"]: s for s in plan["steps"]}


def _of_kind(world: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [s for s in _steps(world).values() if s["step"]["kind"] == kind]


# ----- F1 and F7: a decision follows its file, and stops when the file changes -----------------


def test_a_decision_survives_a_new_earlier_member_and_reopens_when_its_file_changes(
    tmp_path: Path,
) -> None:
    world = scan_tree(tmp_path, SWEEPS)
    env = world["env"]
    family = family_with(world, "results/sweep_v2.csv")
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    env.svc.adoption_decide(
        env.project_id, world["gen"], family["family_id"], "results/sweep_v2.csv",
        authority=OWNER_ACT,
    )  # fmt: skip
    (resolve,) = _of_kind(world, "resolve_conflict")
    assert resolve["state"] == "settled"
    # A member that sorts first changes the family's ID; the decision still applies.
    (world["root"] / "results" / "aaa").mkdir()
    (world["root"] / "results" / "aaa" / "sweep_v0.csv").write_text("speed,loss\n10,0.5\n")
    _rescan(world)
    moved = family_with(world, "results/sweep_v2.csv")
    assert moved["family_id"] != family["family_id"]
    assert (moved["current"], moved["deciding_rule"]) == ("results/sweep_v2.csv", "R1")
    # The decided file is edited: the decision no longer applies and the owner is asked again.
    (world["root"] / "results" / "sweep_v2.csv").write_text("speed,loss\n10,0.25\n")
    _rescan(world)
    assert family_with(world, "results/sweep_v2.csv")["deciding_rule"] != "R1"
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    reopened = [s for s in _of_kind(world, "resolve_conflict") if s["state"] == "ready"]
    assert reopened, "the family's decision step is asked again"


# ----- F2: a validated result whose inputs changed is reopened, never accepted -----------------


def test_a_result_is_reopened_when_its_inputs_change(tmp_path: Path) -> None:
    copies = {f"data/copy_{i}/table.csv": "a,b\n1,2\n3,4\n5,6\n7,8\n9,10\n" for i in range(3)}
    world = scan_tree(tmp_path, {**SWEEPS, **copies})
    env = world["env"]
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    (dedupe,) = _of_kind(world, "dedupe_proposal")
    report = env.svc.adoption_step_report(
        env.project_id, AGENT, world["gen"], dedupe["step"]["step_id"],
        {"summary": "keep copy_0", "proposal": "Keep data/copy_0; nothing deleted.",
         "paths": [i["path"] for i in dedupe["step"]["inputs"]],
         "actions_taken": ["adoption_read"]},
    )  # fmt: skip
    assert report["state"] == "validated"
    (world["root"] / "data" / "copy_2" / "table.csv").write_text("a,b\n0,0\n", "utf-8")
    _rescan(world)
    accepted = env.svc.adoption_baseline_accept(env.project_id, world["gen"], authority=OWNER_ACT)
    assert accepted["accepted_steps"] == 0, "a result about other inputs is not accepted"
    (now,) = _of_kind(world, "dedupe_proposal")
    assert now["state"] == "ready", now["state"]
    assert now["result"]["previous"]["result"]["summary"] == "keep copy_0", "kept as history"


# ----- F3: reported results cannot be fabricated ---------------------------------------------


def test_a_tool_extract_report_needs_real_tasks_and_a_proposal_names_an_input(
    tmp_path: Path,
) -> None:
    world = scan_tree(tmp_path, {**SWEEPS, "notes/log.md": LOG})
    env = world["env"]
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    extract = next(
        s for s in _of_kind(world, "tool_extract") if "notes/log.md" in str(s["step"]["inputs"])
    )
    fake = env.svc.adoption_step_report(
        env.project_id, AGENT, world["gen"], extract["step"]["step_id"],
        {"assist_ids": ["as_" + "0" * 32], "actions_taken": ["adoption_read"]},
    )  # fmt: skip
    assert fake["state"] == "rejected"
    assert fake["problems"] == [f"no accepted reading task: as_{'0' * 32}"]
    report = {"actions_taken": ["adoption_read", "adoption_manage.assist_submit"]}
    step_id = extract["step"]["step_id"]
    waiting = _send(world, GOOD, 1, "model-a")
    assert waiting["outcome"]["state"] == "waiting"
    early = env.svc.adoption_step_report(
        env.project_id,
        AGENT,
        world["gen"],
        step_id,
        {"assist_ids": [waiting["assist_id"]], **report},
    )
    assert early["state"] == "rejected", "a task that is not accepted does not count"
    assert _send(world, GOOD, 2, "model-b")["outcome"]["state"] == "accepted"
    real = env.svc.adoption_step_report(
        env.project_id,
        AGENT,
        world["gen"],
        step_id,
        {"assist_ids": [waiting["assist_id"]], **report},
    )
    assert real["state"] == "validated", real


# ----- F4: a re-run that does not reproduce adds compare and recompute steps -----------------


def test_a_rerun_that_differs_adds_compare_and_recompute_steps(tmp_path: Path) -> None:
    producer = "% make the sweep\nx = 1;\nwritematrix(x, 'results/sweep_v3.csv');\n"
    user = "% plot\nm = readmatrix('results/sweep_v3.csv');\nplot(m);\n"
    world = scan_tree(
        tmp_path, {**SWEEPS, "analysis/make_sweep.m": producer, "analysis/plot_it.m": user}
    )
    env = world["env"]
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    (rerun,) = _of_kind(world, "rerun")
    out = env.svc.adoption_step_report(
        env.project_id, AGENT, world["gen"], rerun["step"]["step_id"],
        {"command": "matlab -batch make_sweep", "exit_code": 0, "tool_version": "R2024b",
         "outputs": [{"name": "sweep_v3.csv", "sha256": "f" * 64}],
         "actions_taken": ["copy_inputs_to_scratch", "run_producer_in_scratch", "matlab"]},
    )  # fmt: skip
    assert out["state"] == "validated" and out["outcome"]["reproduced"] is False
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    (compare,) = _of_kind(world, "compare")
    (recompute,) = _of_kind(world, "recompute_verify")
    assert [i["path"] for i in compare["step"]["inputs"]] == ["results/sweep_v3.csv"]
    assert "analysis/plot_it.m" in [i["path"] for i in recompute["step"]["inputs"]]
    assert (
        compare["step"]["output_schema"] == recompute["step"]["output_schema"] == "compare_result"
    )


# ----- F6: many open decisions never crowd out the other kinds --------------------------------


def test_the_plan_keeps_every_kind_and_reports_omitted_decisions(tmp_path: Path) -> None:
    files: dict[str, str] = {}
    for i in range(70):
        for v in (1, 2):
            files[f"series/s{i:02d}/trace{i:02d}_v{v}.csv"] = f"t,y\n{i},{v}\n"
    files.update({"dup/a/same.txt": "same content here\n", "dup/b/same.txt": "same content here\n"})
    world = scan_tree(tmp_path, files)
    env = world["env"]
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    plan = env.svc.adoption_plan(env.project_id, OWNER, world["gen"])
    kinds = [s["step"]["kind"] for s in plan["steps"] if s["state"] != "superseded"]
    assert "dedupe_proposal" in kinds
    kept = kinds.count("resolve_conflict")
    assert len(kinds) == 60 and kept < 60
    assert plan["omitted_decisions"] > 0 and kept + plan["omitted_decisions"] >= 70


# ----- F5: the schema is migrated, not edited in place ----------------------------------------


def test_migration_eleven_adds_decisions_and_pruning_on_a_version_ten_store(tmp_path: Path) -> None:
    from prometheos.store.schema import MIGRATIONS

    assert [m.version for m in MIGRATIONS][9:11] == [10, 11]  # later migrations follow (0058)
    conn = sqlite3.connect(tmp_path / "v10.sqlite")
    try:
        for migration in MIGRATIONS[:10]:
            migration.apply(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "adoption_scans" in tables and "adoption_decisions" not in tables
        MIGRATIONS[10].apply(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"adoption_decisions", "adoption_pruned"} <= tables
    finally:
        conn.close()


# ----- r2: answers to review-precheck-cp17-5-a-r2 -----------------------------------------------


def test_the_newest_decision_wins_after_family_ids_change(tmp_path: Path) -> None:
    """The later choice (sweep_v3, alphabetically last) wins after the ID changes twice, and
    still wins when the ID comes back to its first value (review cp17-5-a-r3 R3-1, R3-2)."""
    world = scan_tree(tmp_path, SWEEPS)
    env = world["env"]
    first = family_with(world, "results/sweep_v1.csv")
    env.svc.adoption_decide(
        env.project_id,
        world["gen"],
        first["family_id"],
        "results/sweep_v1.csv",
        authority=OWNER_ACT,
    )
    (world["root"] / "results" / "aa").mkdir()
    (world["root"] / "results" / "aa" / "sweep_v0.csv").write_text("speed,loss\n10,0.6\n")
    _rescan(world)
    second = family_with(world, "results/sweep_v1.csv")
    assert second["family_id"] != first["family_id"] and second["current"] == "results/sweep_v1.csv"
    env.svc.adoption_decide(
        env.project_id,
        world["gen"],
        second["family_id"],
        "results/sweep_v3.csv",
        authority=OWNER_ACT,
    )
    (world["root"] / "results" / "a").mkdir()
    (world["root"] / "results" / "a" / "sweep_v9.csv").write_text("speed,loss\n10,0.7\n")
    _rescan(world)
    third = family_with(world, "results/sweep_v1.csv")
    assert third["family_id"] not in (first["family_id"], second["family_id"])
    assert (third["current"], third["deciding_rule"]) == ("results/sweep_v3.csv", "R1")
    for extra in ("a/sweep_v9.csv", "aa/sweep_v0.csv"):
        (world["root"] / "results" / extra).unlink()
    _rescan(world)
    back = family_with(world, "results/sweep_v1.csv")
    assert back["family_id"] == first["family_id"], "the ID is back to its first value"
    assert (back["current"], back["deciding_rule"]) == ("results/sweep_v3.csv", "R1"), (
        "the older decision for this ID does not come back"
    )


def test_migration_eleven_accepts_a_store_that_already_has_the_decisions_table(
    tmp_path: Path,
) -> None:
    from prometheos.store.schema import MIGRATIONS

    conn = sqlite3.connect(tmp_path / "intermediate.sqlite")
    try:
        for migration in MIGRATIONS[:10]:
            migration.apply(conn)
        # what migration 10 of commits c52ef86 and 6f036fc created
        conn.execute(
            "CREATE TABLE adoption_decisions (decision_id TEXT PRIMARY KEY, root_alias TEXT NOT "
            "NULL, family_id TEXT NOT NULL, current_path TEXT NOT NULL, current_sha256 TEXT, note "
            "TEXT, decided_by TEXT NOT NULL, decided_at TEXT NOT NULL)"
        )
        MIGRATIONS[10].apply(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"adoption_decisions", "adoption_pruned"} <= names
    finally:
        conn.close()


def test_a_dedupe_proposal_covers_every_copy(tmp_path: Path) -> None:
    copies = {f"data/copy_{i}/table.csv": "a,b\n1,2\n3,4\n5,6\n7,8\n9,10\n" for i in range(4)}
    world = scan_tree(tmp_path, copies)
    env = world["env"]
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    (dedupe,) = _of_kind(world, "dedupe_proposal")
    inputs = [i["path"] for i in dedupe["step"]["inputs"]]
    part = env.svc.adoption_step_report(
        env.project_id, AGENT, world["gen"], dedupe["step"]["step_id"],
        {"summary": "keep one", "proposal": "Keep copy_0.", "paths": inputs[:1],
         "actions_taken": ["adoption_read"]},
    )  # fmt: skip
    assert part["state"] == "rejected"
    assert part["problems"] == [f"the proposal leaves out {len(inputs) - 1} of the step's files"]


def test_a_reading_task_about_an_earlier_version_does_not_count(tmp_path: Path) -> None:
    world = scan_tree(tmp_path, {**SWEEPS, "notes/log.md": LOG})
    env = world["env"]
    sample = _send(world, GOOD, 1, "model-a")
    assert _send(world, GOOD, 2, "model-b")["outcome"]["state"] == "accepted"
    (world["root"] / "notes" / "log.md").write_text(LOG + "Later: more runs.\n", "utf-8")
    _rescan(world)
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    extract = next(
        s for s in _of_kind(world, "tool_extract") if "notes/log.md" in str(s["step"]["inputs"])
    )
    out = env.svc.adoption_step_report(
        env.project_id, AGENT, world["gen"], extract["step"]["step_id"],
        {"assist_ids": [sample["assist_id"]],
         "actions_taken": ["adoption_read", "adoption_manage.assist_submit"]},
    )  # fmt: skip
    assert out["state"] == "rejected"
    assert out["problems"] == [
        f"the reading task was about an earlier version: {sample['assist_id']}"
    ]


def test_backup_gap_and_metadata_proposals_cover_every_input(tmp_path: Path) -> None:
    """Review cp17-5-a-r4 R4-2: like a dedupe proposal, a backup-gap proposal names every backup
    copy and a metadata proposal every result without a producer (review cp17-5-a-r3 R3-4)."""
    files = {
        "backup/sweep_v1.csv": "speed,loss\n10,0.31\n",
        "backup/sweep_v2.csv": "speed,loss\n10,0.30\n",
        "results/loss_v1.csv": "step,loss\n1,0.9\n",
        "results/loss_v2.csv": "step,loss\n1,0.8\n",
    }
    world = scan_tree(tmp_path, files)
    env = world["env"]
    env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    for kind in ("backup_gap", "metadata_proposal"):
        (step,) = _of_kind(world, kind)
        inputs = [i["path"] for i in step["step"]["inputs"]]
        assert len(inputs) >= 2, (kind, inputs)
        report = {"summary": "s", "proposal": "p", "actions_taken": ["adoption_read"]}
        part = env.svc.adoption_step_report(
            env.project_id, AGENT, world["gen"], step["step"]["step_id"],
            {**report, "paths": inputs[:1]},
        )  # fmt: skip
        assert part["state"] == "rejected", kind
        assert part["problems"] == [
            f"the proposal leaves out {len(inputs) - 1} of the step's files"
        ]
        whole = env.svc.adoption_step_report(
            env.project_id, AGENT, world["gen"], step["step"]["step_id"],
            {**report, "paths": inputs},
        )  # fmt: skip
        assert whole["state"] == "validated", (kind, whole)
