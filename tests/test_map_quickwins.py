"""Map quick wins (decision 0108): a claim's track by its files' location and the inbox event when
a claim turns stale; and the brief on Now (decision 0109).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from test_cp20_claim_status import assess, claim, imported, line_ev

from prometheos.app import research_map


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    for track, n in (("track2", 2), ("track5", 5)):
        folder = environment.research / track
        folder.mkdir()
        (folder / "run_sim.m").write_text(
            f"T = table([1;{n}]);\nwritetable(T, 'energy.csv');\n", "utf-8"
        )
        (folder / "energy.csv").write_text(f"name = value\nenergy_kWh = 19{n}.9\n", "utf-8")
    (environment.research / "summary.csv").write_text("name = value\nsaving = 17.4\n", "utf-8")
    return environment


def confirm_outline(env: Env, edits: dict[str, Any] | None = None) -> dict[str, Any]:
    gen = env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")[
        "scan_gen"
    ]
    outline = env.svc.adoption_outline(env.project_id, AGENT, gen)
    env.svc.adoption_outline_confirm(
        env.project_id, gen, outline["sha256"], edits or {}, authority=OWNER_ACT
    )
    return outline


def tracks(env: Env) -> dict[str, dict[str, Any]]:
    return {t["title"]: t for t in env.svc.research_map(env.project_id, OWNER)["tracks"]}


def test_a_claim_draws_on_the_track_its_file_lies_in(env: Env) -> None:
    files = imported(env, ["track2/energy.csv", "summary.csv"])
    on_track = claim(env, [line_ev(files["track2/energy.csv"], 2)])
    elsewhere = claim(env, [line_ev(files["summary.csv"], 2)])
    assess(env, on_track)
    # Before any confirmed outline there are no tracks, so nothing is inferred.
    assert tracks(env) == {}
    confirm_outline(env)
    found = tracks(env)
    assert set(found) == {"track2", "track5"}
    assert (found["track2"]["claims"], found["track2"]["claims_by_path"]) == (0, 1)
    assert found["track2"]["folders"] == ["track2"]
    assert found["track5"]["claims_by_path"] == 0
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        built = research_map._Map(conn, env.store_dir, OWNER)
        track2 = found["track2"]["entry_id"]
        assert built.by_path[on_track] == [track2]
        assert built.by_path[elsewhere] == []
    finally:
        conn.close()


def test_a_stored_link_is_not_counted_twice_by_location(env: Env) -> None:
    files = imported(env, ["track2/energy.csv"])
    cid = claim(env, [line_ev(files["track2/energy.csv"], 2)])
    confirm_outline(env)
    track2 = tracks(env)["track2"]["entry_id"]
    env.apply(
        [
            {
                "op": "relation_add",
                "source_ref": {"id": cid, "rev": 1},
                "target_ref": {"id": track2, "rev": 1},
                "relation": "draws_on",
            }
        ],
        caller=AGENT,
    )
    found = tracks(env)["track2"]
    assert (found["claims"], found["claims_by_path"]) == (1, 0)


def test_mutation_a_file_in_a_sibling_folder_with_the_same_prefix_is_not_on_the_track(
    env: Env,
) -> None:
    """``track2b/`` starts with ``track2`` but is not inside it (a file of its own, so the
    folder is no track). Without the ``/`` in the prefix test this claim would count."""
    sibling = env.research / "track2b"
    sibling.mkdir()
    (sibling / "loss_table.csv").write_text("name = value\nloss_kWh = 1.0\n", "utf-8")
    files = imported(env, ["track2b/loss_table.csv"])
    claim(env, [line_ev(files["track2b/loss_table.csv"], 2)])
    confirm_outline(env)
    found = tracks(env)
    assert set(found) == {"track2", "track5"}
    assert all(t["claims_by_path"] == 0 for t in found.values())


# ----- the inbox event when a claim needs review (gap 12) ----------------------------------


def claim_events(env: Env) -> list[dict[str, Any]]:
    found = env.svc.events_detect(env.project_id)["emitted"]
    return [e for e in found if e["type"] == "claim.needs_review"]


def test_a_claim_turning_stale_is_announced_once(env: Env) -> None:
    files = imported(env, ["summary.csv"])
    cid = claim(env, [line_ev(files["summary.csv"], 2)])
    assess(env, cid)
    assert claim_events(env) == []  # it holds
    (env.research / "summary.csv").write_text("name = value\nsaving = 18.2\n", "utf-8")
    env.svc.source_check(env.project_id, [files["summary.csv"]["version_id"]], save=True,
                         caller=OWNER)  # fmt: skip
    (event,) = claim_events(env)
    assert event["subject_ref"] == {"kind": "claim", "id": cid}
    listed = env.svc.event_list(
        env.project_id, AGENT, state="open", event_type="claim.needs_review"
    )["items"]
    data = next(e["data"] for e in listed if e["type"] == "claim.needs_review")
    assert data == {"status": "stale", "cause": "origin_changed"}
    assert claim_events(env) == []  # once per entry, not once per pass


# ----- the brief on Now (decision 0109) ------------------------------------------------------


def test_the_brief_lists_what_needs_the_owner_in_the_fixed_order(env: Env) -> None:
    from datetime import UTC, datetime, timedelta

    from test_cp18_work_items import add_work_item

    files = imported(env, ["summary.csv"])
    cid = claim(env, [line_ev(files["summary.csv"], 2)])
    assess(env, cid)
    clear = env.svc.now(env.project_id, OWNER)["brief"]
    assert (clear["items"], clear["total"]) == ([], 0)
    (env.research / "summary.csv").write_text("name = value\nsaving = 18.2\n", "utf-8")
    env.svc.source_check(env.project_id, [files["summary.csv"]["version_id"]], save=True,
                         caller=OWNER)  # fmt: skip
    due = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    action = add_work_item(env, due_at=due)
    brief = env.svc.now(env.project_id, OWNER)["brief"]
    assert [(i["group"], i["id"]) for i in brief["items"]] == [("due", action), ("evidence", cid)]
    stale = brief["items"][1]
    assert stale["why"].startswith("Do not quote it. A source check") and stale["status"] == "stale"
    assert brief["counts"] == {"decide": 0, "due": 1, "evidence": 1, "interpret": 0}
    assert brief["generation"] == "deterministic" and brief["total"] == 2


def test_the_brief_never_shows_a_claim_the_caller_may_not_see(env: Env) -> None:
    files = imported(env, ["summary.csv"])
    cid = claim(env, [line_ev(files["summary.csv"], 2)])
    env.apply_protected(
        [{"op": "revise", "id": cid, "base_rev": 1, "sensitivity": "restricted",
          "occurred_at": None, "body": env.svc.get(env.project_id, OWNER, cid, None)["body"]}],
        caller=OWNER,
    )  # fmt: skip
    (env.research / "summary.csv").write_text("name = value\nsaving = 18.2\n", "utf-8")
    env.svc.source_check(env.project_id, [files["summary.csv"]["version_id"]], save=True,
                         caller=OWNER)  # fmt: skip
    assert cid in {i["id"] for i in env.svc.now(env.project_id, OWNER)["brief"]["items"]}
    assert cid not in {i["id"] for i in env.svc.now(env.project_id, AGENT)["brief"]["items"]}


def test_r1_f7_a_cut_short_group_stops_the_brief_so_the_order_holds() -> None:
    from prometheos.app import brief

    def section(count: int, ids: list[str]) -> dict[str, Any]:
        items = [{"kind": "approval_request", "id": i, "title": i, "reason": "r"} for i in ids]
        return {"count": count, "items": items}

    view = {
        "approvals": section(3, ["apreq_1"]),  # three wait; the section lists one
        "due": section(2, ["action_1", "action_2"]),
        "awaiting_interpretation": section(0, []),
    }
    built = brief.build(view, [])
    assert [i["id"] for i in built["items"]] == ["apreq_1"]
    assert built["total"] == 5
