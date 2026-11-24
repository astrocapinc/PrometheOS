"""C17-LINEAGE (A05, CP17.3; decision 0041): families and ranked current members match the
golden labels of the seeded corpora at the targets recorded before the first measurement
(``tools/adoption_targets.json``, committed at f2d3ebb before fusion existed); every planted
trap is detected or surfaced; contradictions are kept; nothing is auto-applied; no filesystem
time decides. Targeted mutations show the measurement would catch the method going wrong."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from adoption_score import TARGETS, run

from prometheos.adoption import analyzers, fusion
from prometheos.adoption import inventory as inv

# The targets as first recorded (f2d3ebb). They may only ever be raised.
RECORDED = {
    "family_pairwise_precision": {"min": 0.95},
    "family_pairwise_recall": {"min": 0.9},
    "current_precision": {"min": 0.98},
    "current_coverage": {"min": 0.8},
    "traps_handled": {"min": 1.0},
    "auto_applied_supersessions": {"max": 0},
    "mtime_decisions": {"max": 0},
}
RECORDED_SEEDS = [17, 18, 19, 20, 21]


def test_targets_were_never_lowered() -> None:
    current = json.loads(Path(TARGETS).read_text("utf-8"))
    assert current["seeds"] == RECORDED_SEEDS
    assert set(current["targets"]) == set(RECORDED)
    for name, bound in RECORDED.items():
        now = current["targets"][name]
        if "min" in bound:
            assert now["min"] >= bound["min"], name
        if "max" in bound:
            assert now["max"] <= bound["max"], name


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return run()


def test_the_recorded_targets_are_met(report: dict[str, Any]) -> None:
    assert report["failures"] == [], report["metrics"]
    for seed, measured in report["per_seed"].items():
        missed = [t for t, ok in measured["traps"].items() if not ok]
        assert not missed, (seed, missed)


def test_every_trap_has_a_check(report: dict[str, Any]) -> None:
    from adoption_score import TRAP_CHECKS

    for measured in report["per_seed"].values():
        assert set(measured["traps"]) <= set(TRAP_CHECKS)


# ----- targeted mutations: the measurement catches a broken method ------------------------


def _fails(seed: int = 17) -> list[str]:
    result = run([seed])
    missed = [t for t, ok in result["per_seed"][seed]["traps"].items() if not ok]
    return result["failures"] + [f"trap {t}" for t in missed]


def test_mutation_ranking_by_file_time_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    def by_mtime(paths: list[str], index: fusion.Index, **_: Any) -> fusion.Ranked:
        classes = fusion._classes(paths, index)
        newest = max(classes, key=lambda c: index.rows[c.rep].mtime_us or 0)
        newest.reasons.append(fusion._reason("R8", "T7", "newest modification time", []))
        return fusion.Ranked(newest.rep, "weak", "R8", classes, [])

    monkeypatch.setattr(fusion, "rank", by_mtime)
    failures = _fails()
    assert any("mtime" in f or "current" in f for f in failures), failures


def test_mutation_without_exclusions_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    original = fusion.Index.related

    def related(self: fusion.Index, path: str, predicate: str) -> Any:
        if predicate in ("autosave_of", "backup_of"):
            return []
        return original(self, path, predicate)

    monkeypatch.setattr(fusion.Index, "related", related)
    failures = _fails()
    assert "trap autosave" in failures and "trap upgrade_backup" in failures, failures


def test_mutation_dropping_contradictions_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    original = fusion.rank

    def silent(*args: Any, **kwargs: Any) -> fusion.Ranked:
        ranked = original(*args, **kwargs)
        return fusion.Ranked(ranked.current, ranked.band, ranked.rule, ranked.candidates, [])

    monkeypatch.setattr(fusion, "rank", silent)
    assert "trap contradiction" in _fails()


def test_mutation_opening_placeholders_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inv, "is_placeholder", lambda st: False)
    assert "trap placeholder" in _fails()


def test_mutation_reading_hidden_text_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        analyzers,
        "visible_text",
        lambda text: [
            analyzers.Seen(i, line, False, False) for i, line in enumerate(text.splitlines(), 1)
        ],
    )
    assert "trap injection" in _fails()


def test_mutation_naming_final_as_current_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    original = fusion.rank

    def trusts_names(paths: list[str], index: fusion.Index, **kwargs: Any) -> fusion.Ranked:
        named = [p for p in paths if "final" in p.lower() or "best" in p.lower()]
        if named:
            classes = fusion._classes(paths, index)
            winner = next(c for c in classes if named[0] in c.paths)
            return fusion.Ranked(winner.rep, "supported", "R5", classes, [])
        return original(paths, index, **kwargs)

    monkeypatch.setattr(fusion, "rank", trusts_names)
    failures = _fails()
    assert "trap final_name" in failures or "trap best_name" in failures, failures


def test_a_scan_without_ranking_stores_no_families(tmp_path: Path) -> None:
    """A real folder is never ranked before the owner's blind labels are frozen: the dogfood
    tool scans with ``rank=False`` (inventory and analyzers only)."""
    from cp1_support import AGENT, Env

    env = Env(tmp_path)
    (env.research / "sweep_log_v1.md").write_text("one\n", "utf-8")
    (env.research / "sweep_log_v2.md").write_text("two\n", "utf-8")
    plain = env.svc.adoption_scan(
        env.project_id, AGENT, root_alias="research", rank=False, mode="wait"
    )
    assert plain["config"]["rank"] is False and plain["analysis"]["families"] == 0
    assert env.svc.adoption_families(env.project_id, AGENT, plain["scan_gen"])["total"] == 0
    ranked = env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    assert env.svc.adoption_families(env.project_id, AGENT, ranked["scan_gen"])["total"] >= 1


def test_mutation_showing_link_definitions_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review cp17-234-a r2 R2-4: a link definition ([//]: # ...) never renders, so reading it
    as a statement must fail the hidden_markdown trap."""
    import re

    monkeypatch.setattr(analyzers, "_LINK_DEFINITION", re.compile(r"(?!x)x"))
    assert "trap hidden_markdown" in _fails()


def test_mutation_keeping_invisible_characters_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review cp17-234-a r2 R2-4: Unicode tag characters must be removed (and flagged)."""
    monkeypatch.setattr(analyzers, "strip_invisible", lambda text: (text, 0))
    assert "trap tag_characters" in _fails()
