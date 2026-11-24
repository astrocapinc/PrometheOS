"""C02-CONTEXT: limited context keeps critical warnings and reports omissions honestly (S09)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry

from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_budget_bounds_and_manifest(env: Env) -> None:
    env.apply([create_entry(title=f"Background {i}", text="y" * 400) for i in range(40)])
    for budget in (2000, 5000, 12000):
        ctx = env.svc.context(env.project_id, AGENT, budget=budget)
        assert len(ctx["text"]) <= budget
        manifest = ctx["manifest"]
        included = sum(len(s["included"]) for s in manifest["sections"])
        excluded = sum(s["excluded_count"] for s in manifest["sections"])
        assert included + excluded >= 40
        assert manifest["project_seq"] == env.seq()
        assert manifest["generation"] == "deterministic"
    for bad in (1999, 64001):
        with pytest.raises(PrometheosError):
            env.svc.context(env.project_id, AGENT, budget=bad)


def test_context_is_deterministic_for_a_snapshot(env: Env) -> None:
    env.apply([create_entry(title="Only note")])
    a = env.svc.context(env.project_id, AGENT)
    b = env.svc.context(env.project_id, AGENT)
    a["manifest"].pop("generated_at")
    b["manifest"].pop("generated_at")
    assert a == b


def test_missing_and_changed_sources_are_reported(env: Env) -> None:
    ref = env.import_files(["artifact.bin"], retention="reference")["plan"]["items"][0]
    copy = env.import_files(["baseline.md"], retention="copy")["plan"]["items"][0]
    (env.research / "artifact.bin").unlink()
    (env.research / "baseline.md").write_text("edited\n", encoding="utf-8")
    env.svc.source_check(env.project_id, [ref["version_id"], copy["version_id"]], save=True)
    ctx = env.svc.context(env.project_id, AGENT)
    cov = ctx["manifest"]["coverage"]
    assert cov["missing_versions"] == [ref["version_id"]]
    assert cov["changed_origins"] == [copy["version_id"]]
    assert "cannot be verified or quoted" in ctx["text"]
    assert "differs from cited snapshot" in ctx["text"]


def test_critical_overflow_is_flagged_incomplete(env: Env) -> None:
    # Many unavailable roots make the coverage section larger than a tiny budget.
    info = env.svc.project_info(env.project_id, OWNER)
    settings = info["settings"]
    settings["allowed_roots"] += [
        {"alias": f"gone{i}", "path": str(env.tmp / f"gone-{i}")} for i in range(45)
    ]
    env.apply_protected(
        [{"op": "project_update", "base_rev": info["settings_rev"], "settings": settings}]
    )
    ctx = env.svc.context(env.project_id, AGENT, budget=2000)
    assert ctx["manifest"]["incomplete"] is True
    assert "CONTEXT INCOMPLETE" in ctx["text"]
    assert len(ctx["text"]) <= 2000


def test_as_known_context_and_hidden_records(env: Env) -> None:
    env.apply([create_entry(title="Early note")])
    early = env.seq()
    env.apply([create_entry(title="Later note")])
    env.apply([create_entry(title="Secret-XYZ", sensitivity="private")], caller=OWNER)
    ctx_then = env.svc.context(env.project_id, AGENT, at_seq=early)
    assert "Early note" in ctx_then["text"] and "Later note" not in ctx_then["text"]
    now = env.svc.context(env.project_id, AGENT)
    assert "Secret-XYZ" not in json.dumps(now)
    assert "never included" in now["text"]
