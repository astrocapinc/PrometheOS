"""CP20.4 part 6c (A06.1; C20-ADOPT; decision 0106): adoption checks become verification actions
under the owner's budget, a baseline file is citable as a reference-only version that reads as
unverified until a source check confirms its bytes, and class decisions travel with the baseline
package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from test_cp20_claim_status import assess, claim
from test_cp20_outline import forking_folder
from test_cp20_verify import refused, transition

from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    forking_folder(environment.research)
    return environment


def scan(env: Env) -> int:
    return int(
        env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")["scan_gen"]
    )


def adoption_checks(env: Env) -> list[dict[str, Any]]:
    return [c for c in env.svc.verification_plan(env.project_id, AGENT)["checks"]
            if c["adoption_step"]]  # fmt: skip


def test_adoption_checks_are_verification_actions_inside_the_budget(env: Env) -> None:
    gen = scan(env)
    env.svc.adoption_plan_create(env.project_id, AGENT, gen)
    steps = {s["step"]["step_id"]: s["step"] for s in
             env.svc.adoption_plan(env.project_id, AGENT, gen)["steps"]}  # fmt: skip
    checks = adoption_checks(env)
    assert checks, "the plan's checks are verification actions"
    for check in checks:
        step = steps[check["adoption_step"]]
        assert step["actor"] != "owner" and step["kind"] in ("compare", "tool_extract", "rerun",
                                                             "recompute_verify")  # fmt: skip
        assert check["planned_by"] == "prometheos"
    # The other steps stay ordinary work items: owner decisions and proposals are no checks.
    as_checks = {c["adoption_step"] for c in checks}
    for step_id, step in steps.items():
        if step["actor"] == "owner" or step["kind"].endswith("_proposal"):
            assert step_id not in as_checks
    reruns = [c for c in checks if c["kind"] == "rerun"]
    assert reruns, "the producer of a result is rerun"
    assert all(not c["eligible"] and c["estimated_minutes"] is None for c in reruns)
    # A rerun needs the owner; an eligible read-only one an agent may start at once.
    assert refused(lambda: transition(env, reruns[0]["action_id"], "in_progress")).code == (
        "approval_required"
    )
    read_only = [c for c in checks if c["kind"] == "read_only" and c["eligible"]]
    assert read_only, "the plan holds an eligible read-only check (review cp20-a-r1 R1-F4)"
    transition(env, read_only[0]["action_id"], "in_progress")
    started = env.svc.get(env.project_id, OWNER, read_only[0]["action_id"], None)
    assert started["body"]["status"] == "in_progress"


def test_a_baseline_file_is_cited_by_its_scan_digest_and_unverified_until_checked(
    env: Env,
) -> None:
    gen = scan(env)
    baseline = env.svc.adoption_baseline_accept(env.project_id, gen, authority=OWNER_ACT)
    cited = env.svc.adoption_cite(env.project_id, AGENT, baseline["baseline_id"],
                                  "track2/energy.csv")  # fmt: skip
    assert (cited["created"], cited["confirmed"]) == (True, False)
    version = env.svc.get(env.project_id, OWNER, cited["version_id"], None)["body"]
    assert (version["storage"], version["availability"]) == ("external", "external")
    on_disk = (env.research / "track2" / "energy.csv").read_bytes()
    import hashlib

    assert version["sha256"] == hashlib.sha256(on_disk).hexdigest()  # the scan's digest
    assert not list((env.store_dir / "media").rglob("*")) if (env.store_dir / "media").exists() \
        else True  # nothing copied  # fmt: skip
    cid = claim(env, [{"source_id": cited["source_id"], "version_id": cited["version_id"],
                       "locator": {"type": "file"}, "quote_sha256": None}])  # fmt: skip
    assess(env, cid)
    first = env.svc.claim_status(env.project_id, OWNER, claim_ids=[cid])["claims"][0]
    assert first["status"] == "unverified"
    assert "evidence_missing" in {c["code"] for c in first["causes"]}
    env.svc.source_check(env.project_id, [cited["version_id"]], save=True, caller=OWNER)
    confirmed = env.svc.claim_status(env.project_id, OWNER, claim_ids=[cid])["claims"][0]
    assert confirmed["status"] == "holds"
    again = env.svc.adoption_cite(env.project_id, AGENT, baseline["baseline_id"],
                                  "track2/energy.csv")  # fmt: skip
    assert (again["version_id"], again["created"], again["confirmed"]) == (
        cited["version_id"],
        False,
        True,
    )
    # Changed bytes at the origin: a check no longer confirms them, and nothing is rebound.
    (env.research / "track2" / "energy.csv").write_text("kWh\n9\n", "utf-8")
    env.svc.source_check(env.project_id, [cited["version_id"]], save=True, caller=OWNER)
    after = env.svc.claim_status(env.project_id, OWNER, claim_ids=[cid])["claims"][0]
    assert after["status"] == "stale" and after["usable"] is False


def test_a_registered_file_with_other_bytes_is_never_rebound(env: Env) -> None:
    gen = scan(env)
    baseline = env.svc.adoption_baseline_accept(env.project_id, gen, authority=OWNER_ACT)
    (env.research / "track5" / "energy.csv").write_text("kWh\n7\n", "utf-8")
    env.import_files(["track5/energy.csv"])
    with pytest.raises(PrometheosError) as err:
        env.svc.adoption_cite(env.project_id, AGENT, baseline["baseline_id"], "track5/energy.csv")
    assert err.value.code == "revision_conflict"
    with pytest.raises(PrometheosError) as missing:
        env.svc.adoption_cite(env.project_id, AGENT, baseline["baseline_id"], "nope.csv")
    assert missing.value.code == "not_found"


def test_class_decisions_travel_with_the_baseline_package(env: Env, tmp_path: Path) -> None:
    gen = scan(env)
    listing = env.svc.adoption_families(env.project_id, AGENT, gen, limit=500)["families"]
    asking = [f for f in listing if f["needs_owner"] and f["class"] == "compete"]
    if not asking:
        # Make one: a named-context fork competes until the owner says variant.
        for ctx in ("bus", "van"):
            folder = env.research / "vehicles" / ctx
            folder.mkdir(parents=True)
            (folder / "fuel.csv").write_text(f"litres for the {ctx}\n", "utf-8")
        gen = scan(env)
        listing = env.svc.adoption_families(env.project_id, AGENT, gen, limit=500)["families"]
        asking = [f for f in listing if f["needs_owner"] and f["class"] == "compete"]
    family = asking[0]
    env.svc.adoption_classify(env.project_id, gen, family["family_id"], "variant",
                              authority=OWNER_ACT)  # fmt: skip
    baseline = env.svc.adoption_baseline_accept(env.project_id, gen, authority=OWNER_ACT)
    package = env.svc.adoption_baseline_export(env.project_id, AGENT, baseline["baseline_id"])
    assert [d["decision"] for d in package["class_decisions"]] == ["variant"]
    (tmp_path / "fresh").mkdir()
    fresh = Env(tmp_path / "fresh")
    done = fresh.svc.adoption_baseline_import(fresh.project_id, package, authority=OWNER_ACT)
    again = fresh.svc.adoption_families(fresh.project_id, AGENT, done["scan_gen"], limit=500)
    imported = next(f for f in again["families"] if f["key"] == family["key"])
    assert (imported["class_decision"], imported["needs_owner"]) == ("variant", False)


def test_a_credential_named_file_is_never_cited(env: Env) -> None:
    """Review cp20-a-r2 A9 and r3 A1."""
    (env.research / ".env").write_text("TOKEN=abc\n", "utf-8")
    gen = scan(env)
    baseline = env.svc.adoption_baseline_accept(env.project_id, gen, authority=OWNER_ACT)
    with pytest.raises(PrometheosError) as err:
        env.svc.adoption_cite(env.project_id, AGENT, baseline["baseline_id"], ".env")
    assert err.value.code == "policy_denied"
