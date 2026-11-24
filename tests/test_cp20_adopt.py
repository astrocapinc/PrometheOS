"""CP20.4 part a (A06.1; C20-ADOPT; decision 0085): family classes, owner items only for real
competition, and the owner's class decisions.

Every family gets a class from rules first, failing toward ``compete``; only ``compete`` and an
undecided ``succession`` file an owner work item. The owner can say variant, noise or split, and
the baseline records the class and the decision. Classes are measured on a seeded synthetic
forking repository against targets recorded before the first measurement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cp1_support import OWNER_ACT

from prometheos.adoption import classes
from prometheos.app.reads import Caller
from prometheos.domain.models import PrometheosError

ROOT = Path(__file__).resolve().parents[1]
AGENT = Caller(actor="test", scope="agent")


def fam(
    paths: list[str], *, kind: str = "code", band: str = "undecided", current: Any = None
) -> dict:
    return {
        "members": [{"path": p} for p in paths],
        "kind": kind,
        "band": band,
        "current": current,
    }


def rows(paths: list[str], same: bool = False) -> dict[str, dict[str, str]]:
    return {p: {"sha256": ("a" if same else str(i)) * 64} for i, p in enumerate(paths)}


# ----- the rules ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "kind", "same", "expected"),
    [
        (["data/c.csv", "track2/c.csv"], "data", True, "identical"),
        (
            ["exports/slxdump/run1/b.xml", "exports/slxdump/run2/b.xml"],
            "other",
            False,
            "tool_output",
        ),
        (["logs/sim_20260105.log", "logs/sim_20260112.log"], "other", False, "run_log"),
        (["src/model.m", "old/model.m", "archive/model.m"], "code", False, "history"),
        (["track2/x.m", "old/x.m"], "code", False, "history"),
        (["reports/energy_v1.md", "reports/energy_v2.md"], "document", False, "succession"),
        (["track2/smoke.m", "track5/smoke.m", "track7/smoke.m"], "code", False, "variant"),
        (["models/L1/plant.mdl", "models/L2/plant.mdl"], "model", False, "lineage"),
        (["analysis/fit.m", "analysis/fit (1).m"], "code", False, "compete"),
    ],
)
def test_each_class_has_its_rule(paths: list[str], kind: str, same: bool, expected: str) -> None:
    assert classes.classify(fam(paths, kind=kind), rows(paths, same))["class"] == expected


@pytest.mark.parametrize(
    "paths",
    [
        ["scripts/calibrate.m", "tools/misc/calibrate.m"],  # different depths
        ["track2/sweep.m", "track5/sweep.m", "track5/extra/sweep.m"],  # one track holds two
        ["track2/run.m", "track2/run (1).m", "track5/run.m"],  # two in one folder
        ["a/x/run.m", "b/y/run.m"],  # two segments differ
        ["reports/energy.md", "reports/energy_v2.md"],  # a base without a version is no succession
        ["src/model.m", "lib/model.m", "old/model.m"],  # two live copies is no history
        ["vehicles/bus/x.m", "vehicles/van/x.m"],  # named contexts are not provably forks
    ],
)
def test_anything_that_fits_no_rule_exactly_competes(paths: list[str]) -> None:
    got = classes.classify(fam(paths), rows(paths))
    assert (got["class"], got["files_item"]) == ("compete", True)


def test_only_competition_and_an_undecided_succession_ask_the_owner() -> None:
    succession = ["reports/energy_v1.md", "reports/energy_v2.md"]
    undecided = classes.classify(fam(succession), rows(succession))
    assert (undecided["class"], undecided["files_item"]) == ("succession", True)
    decided = classes.classify(
        fam(succession, band="supported", current=succession[1]), rows(succession)
    )
    assert (decided["default"], decided["files_item"]) == (succession[1], False)
    for paths in (["track2/a.m", "track3/a.m"], ["data/c.csv", "x/c.csv"]):
        assert (
            classes.classify(fam(paths), rows(paths, same="data/" in paths[0]))["files_item"]
            is False
        )


# ----- the measurement ------------------------------------------------------------------


def test_the_targets_were_recorded_first_and_are_never_lowered() -> None:
    targets = json.loads((ROOT / "tools" / "adoption_class_targets.json").read_text("utf-8"))
    assert targets["seeds"] == [17, 18, 19, 20, 21]
    assert targets["compete_hidden"]["max"] == 0
    for cls, goal in targets["per_class"].items():
        assert goal["precision"] >= 0.9 and goal["recall"] >= 0.9, cls
    assert targets["per_class"]["compete"]["recall"] == 1.0


def _score(seed: int) -> dict[str, Any]:
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    import adoption_class_score

    return adoption_class_score.run([seed])


def test_one_seed_meets_every_target() -> None:
    report = _score(19)
    assert report["failures"] == [] and report["compete_hidden"] == 0


def test_a_looser_fork_rule_would_hide_competition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: without the 'each in its own folder, one segment apart' test, the traps that
    look like track forks become variants, and the measurement catches it."""
    monkeypatch.setattr(classes, "_one_segment_apart", lambda paths: True)
    report = _score(19)
    assert report["compete_hidden"] > 0 and report["failures"]


def test_the_corpus_is_deterministic(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    from adoption_fork_corpus import generate

    a = generate(tmp_path / "a", 17)
    b = generate(tmp_path / "b", 17)
    assert a == b
    read = {
        p.relative_to(tmp_path / "a").as_posix(): p.read_bytes()
        for p in (tmp_path / "a").rglob("*")
        if p.is_file()
    }
    assert read == {
        p.relative_to(tmp_path / "b").as_posix(): p.read_bytes()
        for p in (tmp_path / "b").rglob("*")
        if p.is_file()
    }


# ----- the plan, the decisions and the baseline -----------------------------------------


@pytest.fixture
def adopted(tmp_path: Path) -> dict[str, Any]:
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    from adoption_fork_corpus import generate

    from prometheos.app.service import Service

    root = tmp_path / "fork"
    golden = generate(root, 18)
    svc = Service(home=tmp_path / "home", actor="test")
    pid = svc.init_project(
        title="Fork adoption", objective="Adopt a forking repository.", roots={"corpus": str(root)}
    )["project_id"]
    gen = svc.adoption_scan(pid, AGENT, root_alias="corpus", mode="wait")["scan_gen"]
    return {"svc": svc, "pid": pid, "gen": gen, "golden": golden, "root": root}


def owner_steps(a: dict[str, Any]) -> list[str]:
    a["svc"].adoption_plan_create(a["pid"], AGENT, a["gen"])
    plan = a["svc"].adoption_plan(a["pid"], AGENT, a["gen"])
    return [
        s["step"]["family_ids"][0]
        for s in plan["steps"]
        if s["step"]["kind"] == "resolve_conflict" and s["state"] not in ("settled", "superseded")
    ]


def families_by_class(a: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    listing = a["svc"].adoption_families(a["pid"], AGENT, a["gen"], limit=500)["families"]
    for f in listing:
        out.setdefault(f["class"], []).append(f)
    return out


def test_only_competing_families_become_owner_items(adopted: dict[str, Any]) -> None:
    by_class = families_by_class(adopted)
    asked = set(owner_steps(adopted))
    needing = {f["family_id"] for fs in by_class.values() for f in fs if f["needs_owner"]}
    competing = {f["family_id"] for f in by_class["compete"] if f["needs_owner"]}
    assert asked == needing and competing <= asked
    for cls, items in by_class.items():
        if cls not in ("compete", "succession"):
            assert not any(f["family_id"] in asked for f in items), cls
    # The plan files work items for them; nothing else asks the owner.
    view = adopted["svc"].adoption_family(
        adopted["pid"], AGENT, adopted["gen"], next(iter(competing))
    )
    assert view["class"] == "compete" and view["needs_owner"] is True


def test_the_owner_says_variant_and_the_baseline_records_it(adopted: dict[str, Any]) -> None:
    svc, pid, gen = adopted["svc"], adopted["pid"], adopted["gen"]
    family = families_by_class(adopted)["compete"][0]
    assert family["family_id"] in owner_steps(adopted)
    # An agent can only ask (the decision is the owner's).
    with pytest.raises(PrometheosError) as err:
        svc.adoption_classify(pid, gen, family["family_id"], "variant", authority=None)
    assert err.value.code == "policy_denied"
    svc.adoption_classify(
        pid,
        gen,
        family["family_id"],
        "variant",
        "Each is right for its track.",
        authority=OWNER_ACT,
    )
    view = svc.adoption_family(pid, AGENT, gen, family["family_id"])
    assert (view["class_decision"], view["needs_owner"]) == ("variant", False)
    assert family["family_id"] not in owner_steps(adopted)
    accepted = svc.adoption_baseline_accept(pid, gen, "Classes checked.", authority=OWNER_ACT)
    payload = svc.adoption_baseline(pid, AGENT, accepted["baseline_id"])["payload"]
    recorded = {f["family_id"]: f for f in payload["families"]}
    assert recorded[family["family_id"]]["class_decision"] == "variant"
    assert {f["class"] for f in payload["families"]} >= {"variant", "identical", "compete"}


def test_a_decision_stops_applying_when_the_family_changes(adopted: dict[str, Any]) -> None:
    svc, pid, gen = adopted["svc"], adopted["pid"], adopted["gen"]
    family = next(f for f in families_by_class(adopted)["compete"] if f["key"].endswith("fit_drag"))
    svc.adoption_classify(pid, gen, family["family_id"], "noise", authority=OWNER_ACT)
    (adopted["root"] / "analysis" / "fit_drag (2).m").write_text("% a third copy\n", "utf-8")
    gen2 = svc.adoption_scan(pid, AGENT, root_alias="corpus", mode="wait")["scan_gen"]
    listing = svc.adoption_families(pid, AGENT, gen2, limit=500)["families"]
    again = next(f for f in listing if f["key"] == family["key"])
    assert (again["class_decision"], again["needs_owner"]) == (None, True)


def test_an_unknown_class_decision_is_refused(adopted: dict[str, Any]) -> None:
    family = families_by_class(adopted)["compete"][0]
    with pytest.raises(PrometheosError) as err:
        adopted["svc"].adoption_classify(
            adopted["pid"], adopted["gen"], family["family_id"], "current", authority=OWNER_ACT
        )
    assert err.value.code == "validation_error"


def test_migration_15_adds_the_class_decisions_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A06.1's one CP20 store migration: a schema-14 store gains adoption_class_decisions."""
    import sqlite3
    from contextlib import closing

    from cp1_support import Env

    from prometheos.store import project
    from prometheos.store.schema import MIGRATIONS

    assert MIGRATIONS[14].version == 15 and "class decisions" in MIGRATIONS[14].name
    with monkeypatch.context() as patch:
        patch.setattr(project, "MIGRATIONS", MIGRATIONS[:14])
        env = Env(tmp_path)
    # CP22's migration 16 (the intelligence layer) follows it on the way to the current schema.
    assert env.svc.migrate(env.project_id, authority=OWNER_ACT)["applied"] == [15, 16]
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "adoption_class_decisions" in tables


# ----- review cp20-4-a round 1 ----------------------------------------------------------


@pytest.mark.parametrize(
    "paths",
    [
        ["v1/model.m", "v2/model.m"],
        ["model_v2/model.m", "model_v3/model.m"],
        ["old1/model.m", "old2/model.m"],
        ["final1/model.m", "final2/model.m"],
        ["bak1/model.m", "bak2/model.m"],
        ["track2/x.m", "trackalpha/x.m"],  # a long suffix is a name, not a numbered context
        ["exports/slxdump/a/l.xml", "config/l.xml"],  # one generated member is not tool output
        ["runs/a.log", "notes/a.txt"],  # text outside a logs folder is no log
    ],
)
def test_versions_statuses_and_partial_rules_compete(paths: list[str]) -> None:
    """F1 and F3: version- or status-named folders, and families only partly matching a rule,
    ask the owner."""
    got = classes.classify(fam(paths), rows(paths))
    assert (got["class"], got["files_item"]) == ("compete", True), paths


def test_an_archive_word_with_a_number_is_still_an_archive() -> None:
    paths = ["src/model.m", "backup_2024/model.m", "old2/model.m"]
    got = classes.classify(fam(paths), rows(paths))
    assert (got["class"], got["default"]) == ("history", "src/model.m")


def test_a_kept_contradiction_or_a_ranked_current_off_the_default_asks_the_owner() -> None:
    """F2: whatever the class."""
    paths = ["src/model.m", "old/model.m"]
    quiet = classes.classify(fam(paths, band="supported", current="src/model.m"), rows(paths))
    assert quiet["files_item"] is False
    contradicted = fam(paths, band="supported", current="src/model.m") | {
        "conflicts": [{"kind": "contradicted_statement"}]
    }
    assert classes.classify(contradicted, rows(paths))["files_item"] is True
    off_default = fam(paths, band="supported", current="old/model.m")
    assert classes.classify(off_default, rows(paths))["files_item"] is True


def test_a_class_decision_can_be_undone(adopted: dict[str, Any]) -> None:
    """F6: 'revoked' ends the decision; the family asks again."""
    svc, pid, gen = adopted["svc"], adopted["pid"], adopted["gen"]
    family = families_by_class(adopted)["compete"][0]
    svc.adoption_classify(pid, gen, family["family_id"], "noise", authority=OWNER_ACT)
    assert svc.adoption_family(pid, AGENT, gen, family["family_id"])["needs_owner"] is False
    svc.adoption_classify(pid, gen, family["family_id"], "revoked", authority=OWNER_ACT)
    view = svc.adoption_family(pid, AGENT, gen, family["family_id"])
    assert (view["class_decision"], view["needs_owner"]) == (None, True)


# ----- review cp20-4-a round 2 ----------------------------------------------------------


@pytest.mark.parametrize(
    "paths",
    [
        ["modelv1/m.m", "modelv2/m.m"],
        ["build1/m.m", "build2/m.m"],
        ["try1/m.m", "try2/m.m"],
        ["gen1/m.m", "gen2/m.m"],
        ["rc1/m.m", "rc2/m.m"],
        ["V1/m.m", "V3/m.m"],  # vehicles or versions: the owner says which
        ["1/m.m", "2/m.m"],  # no word at all
        ["case_ab/m.m", "case_cd/m.m"],  # two-letter suffixes are names
        ["archive1/m.m", "archive2/m.m"],  # both archived: no live file, no context
        ["a/run_v1.m", "b/run_v2.m"],  # versions in two folders is no succession
        ["reports/r_v1.md", "reports/r_v1 (1).md"],  # one token twice is no succession
    ],
)
def test_only_named_contexts_and_exact_shapes_escape_competition(paths: list[str]) -> None:
    """R2-F1 and R2-F4: the context rule allows named contexts only; the other rules need their
    exact shape."""
    got = classes.classify(fam(paths), rows(paths))
    assert (got["class"], got["files_item"]) == ("compete", True), paths


@pytest.mark.parametrize(
    "paths",
    [
        ["track2/m.m", "track10/m.m"],
        ["case_a/m.m", "case_b/m.m"],
        ["scenario1/m.m", "scenario2/m.m"],
        ["cell3/m.m", "cell4/m.m"],
    ],
)
def test_named_contexts_are_variants(paths: list[str]) -> None:
    assert classes.classify(fam(paths), rows(paths))["class"] == "variant"


def test_identical_needs_every_digest() -> None:
    """R2-F4: a missing digest is not 'the same bytes'."""
    paths = ["data/c.csv", "track2/c.csv"]
    partial = {paths[0]: {"sha256": "a" * 64}, paths[1]: {"sha256": None}}
    assert classes.classify(fam(paths, kind="data"), partial)["class"] != "identical"


def test_a_settled_ranking_off_the_default_still_asks() -> None:
    """R2-F2: band supported, no conflicts, but the ranked current is the archived copy."""
    paths = ["src/metrics.m", "old/metrics.m"]
    got = classes.classify(fam(paths, band="supported", current="old/metrics.m"), rows(paths))
    assert (got["off_default"], got["files_item"]) == (True, True)


def test_undo_needs_a_decision_in_force(adopted: dict[str, Any]) -> None:
    """R2-F3: nothing to undo is refused, and nothing is recorded."""
    family = families_by_class(adopted)["compete"][0]
    with pytest.raises(PrometheosError) as err:
        adopted["svc"].adoption_classify(
            adopted["pid"], adopted["gen"], family["family_id"], "revoked", authority=OWNER_ACT
        )
    assert err.value.code == "validation_error"
    assert family["family_id"] in owner_steps(adopted)


def test_round_3_notes() -> None:
    """R3-N1 and R3-N3: config folders and different names compete; two missing digests are not
    identical."""
    for paths in (["config1/m.m", "config2/m.m"], ["track2/a.m", "track3/b.m"]):
        assert classes.classify(fam(paths), rows(paths))["class"] == "compete", paths
    paths = ["data/c.csv", "track2/c.csv"]
    missing = {p: {"sha256": None} for p in paths}
    assert classes.classify(fam(paths, kind="data"), missing)["class"] != "identical"
