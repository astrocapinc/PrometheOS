"""C17-PLAN (A05, CP17.5; decision 0043; S50): the consolidation plan is typed work. Each step
declares digested inputs, allowed and forbidden actions, an output schema and acceptance
criteria, and is created as a CP18 work item; reported results are validated against the
inventory; deletes and dedupes are only proposed; the owner decides families and accepts
results with a baseline, which makes a new plan version. Agents only request owner acts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from adoption_corpus import CORPUS_LIMITS, generate
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from test_cp10_authority import grant, refused
from test_cp17_inventory import _add_root, placeholder_provider, placeholders_of, tree_digest

from prometheos.adoption import plan as plan_mod
from prometheos.adoption import scan as scan_mod
from prometheos.domain.models import PrometheosError

SEED = 17


def make_world(base: Path) -> dict[str, Any]:
    """Generate the corpus, scan it and create the plan in a fresh store under ``base``."""
    root = base / "corpus"
    golden = generate(root, SEED)
    (base / "env").mkdir()
    env = Env(base / "env")
    _add_root(env, "corpus", root)
    before = tree_digest(root)
    provider = placeholder_provider(root, placeholders_of(golden))
    original = scan_mod.STAT_PROVIDER
    scan_mod.STAT_PROVIDER = provider
    try:
        scan = env.svc.adoption_scan(
            env.project_id, AGENT, root_alias="corpus", limits=CORPUS_LIMITS, mode="wait"
        )
    finally:
        scan_mod.STAT_PROVIDER = original
    created = env.svc.adoption_plan_create(env.project_id, AGENT, scan["scan_gen"])
    return {
        "env": env,
        "root": root,
        "golden": golden,
        "gen": scan["scan_gen"],
        "provider": provider,
        "plan": created,
        "digest_before": before,
    }


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The walk's shared store: the tests below run in file order on it (a serial module)."""
    return make_world(tmp_path_factory.mktemp("plan"))


@pytest.fixture
def own_world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """A store of its own, for a test that must pass alone or in any order (decision 0068)."""
    return make_world(tmp_path_factory.mktemp("plan_own"))


def steps_of(world: dict[str, Any]) -> list[dict[str, Any]]:
    env = world["env"]
    return env.svc.adoption_plan(env.project_id, AGENT, world["gen"])["steps"]


def good_dedupe_result(dedupe: dict[str, Any]) -> dict[str, Any]:
    """A dedupe proposal that names the step's inputs and only reads: it validates."""
    return {
        "summary": "keep the working copies",
        "proposal": "Nothing deleted; keep the copy "
        "outside backup folders in each group, the researcher may remove the others.",
        "paths": [i["path"] for i in dedupe["inputs"]],
        "actions_taken": ["adoption_read"],
    }


def test_every_step_is_typed_digested_and_forbids_writes(world: dict[str, Any]) -> None:
    steps = steps_of(world)
    kinds = {s["step"]["kind"] for s in steps}
    assert {
        "resolve_conflict",
        "tool_extract",
        "rerun",
        "dedupe_proposal",
        "hydrate_or_skip",
        "convention_proposal",
    } <= kinds
    assert kinds <= set(plan_mod.STEP_KINDS)
    for row in steps:
        step = row["step"]
        assert set(plan_mod.ALWAYS_FORBIDDEN) <= set(step["forbidden"])
        assert step["inputs"] and all(i["path"] for i in step["inputs"])
        assert step["acceptance"] and step["output_schema"] and step["report_via"]
        assert row["state"] == "ready"


def test_every_step_is_a_proposed_work_item(world: dict[str, Any]) -> None:
    env = world["env"]
    for row in steps_of(world):
        action = env.svc.get(env.project_id, OWNER, row["action_id"], None)
        body = action["body"]
        assert body["status"] == "proposed" and "adoption" in body["labels"]
        assert body["title"].startswith("Adoption: ")
        assert row["step"]["step_id"] in body["description"]
    queue = env.svc.work_queue(env.project_id, AGENT, proposed="list")
    titles = [e.get("title", "") for e in queue["entries"]]
    assert any(t.startswith("Adoption: ") for t in titles), "the agent sees the steps"


def test_creating_the_plan_again_reuses_steps_and_items(world: dict[str, Any]) -> None:
    env = world["env"]
    before = {r["step"]["step_id"]: r["action_id"] for r in steps_of(world)}
    again = env.svc.adoption_plan_create(env.project_id, AGENT, world["gen"])
    after = {r["step"]["step_id"]: r["action_id"] for r in steps_of(world)}
    assert again["version"] == world["plan"]["version"] + 1
    assert after == before, "same steps, same work items: nothing is duplicated"


def test_reported_results_are_validated_against_the_inventory(world: dict[str, Any]) -> None:
    env = world["env"]
    rows = {r["step"]["kind"]: r for r in steps_of(world)}
    dedupe = rows["dedupe_proposal"]["step"]
    outside = env.svc.adoption_step_report(
        env.project_id,
        AGENT,
        world["gen"],
        dedupe["step_id"],
        {
            "summary": "keep the first copy",
            "proposal": "Keep one copy of each group.",
            "paths": ["not/in/the/folder.txt"],
            "actions_taken": ["adoption_read"],
        },
    )
    assert outside["state"] == "rejected" and "not an inventory path" in outside["problems"][0]
    deleted = env.svc.adoption_step_report(
        env.project_id,
        AGENT,
        world["gen"],
        dedupe["step_id"],
        {
            "summary": "cleaned",
            "proposal": "Removed the extra copies.",
            "paths": [dedupe["inputs"][0]["path"]],
            "actions_taken": ["adoption_read", "delete"],
        },
    )
    assert deleted["state"] == "rejected"
    assert any("'delete'" in p for p in deleted["problems"])
    scratch = env.svc.adoption_step_report(
        env.project_id, AGENT, world["gen"], dedupe["step_id"],
        {"summary": "ran it", "proposal": "Ran the model.", "paths": [],
         "actions_taken": ["run_producer_in_scratch"]},
    )  # fmt: skip
    assert scratch["state"] == "rejected"
    assert any("not an allowed action" in p for p in scratch["problems"])
    assert "a proposal names at least one of the step's inputs" in scratch["problems"]
    good = env.svc.adoption_step_report(
        env.project_id, AGENT, world["gen"], dedupe["step_id"], good_dedupe_result(dedupe)
    )
    assert good["state"] == "validated", good
    rerun = rows["rerun"]["step"]
    current = next(i for i in rerun["inputs"] if i["path"].endswith(".mat"))
    report = env.svc.adoption_step_report(
        env.project_id,
        AGENT,
        world["gen"],
        rerun["step_id"],
        {
            "command": "matlab -batch run_sweep",
            "exit_code": 0,
            "tool_version": "R2024b",
            "outputs": [{"name": current["path"].rsplit("/", 1)[-1], "sha256": current["sha256"]}],
            "actions_taken": ["copy_inputs_to_scratch", "run_producer_in_scratch", "matlab"],
        },
    )
    assert report["state"] == "validated" and report["outcome"]["reproduced"] is True
    with pytest.raises(PrometheosError) as err:
        owner_step = rows["resolve_conflict"]["step"]
        env.svc.adoption_step_report(env.project_id, AGENT, world["gen"], owner_step["step_id"], {})
    assert err.value.code == "validation_error"


def test_owner_acts_are_requests_for_an_agent(world: dict[str, Any]) -> None:
    env = world["env"]
    row = next(r for r in steps_of(world) if r["step"]["kind"] == "resolve_conflict")
    fid = row["step"]["family_ids"][0]
    family = env.svc.adoption_family(env.project_id, AGENT, world["gen"], fid)
    chosen = family["members"][-1]["path"]
    denied = refused(
        lambda: env.svc.adoption_decide(env.project_id, world["gen"], fid, chosen, authority=None)
    )
    assert denied.code == "policy_denied"
    denied = refused(
        lambda: env.svc.adoption_baseline_accept(env.project_id, world["gen"], authority=None)
    )
    assert denied.code == "policy_denied"
    # The agent asks; the owner grants exactly that plan; the resume spends the grant once.
    params = {"scan_gen": world["gen"], "family_id": fid, "current": chosen, "note": "agent asks"}
    asked = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "adoption_decide", params)
    )
    assert asked.code == "approval_required"
    approval_id = asked.details["approval_request_id"]
    unchanged = env.svc.adoption_family(env.project_id, AGENT, world["gen"], fid)
    assert unchanged["current"] == family["current"]
    grant(env, approval_id)
    done = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert done["result"]["current"] == chosen and done["result"]["deciding_rule"] == "R1"
    assert env.svc.approval_resume(env.project_id, AGENT, approval_id)["replayed"] is True
    states = {r["step"]["step_id"]: r["state"] for r in steps_of(world)}
    assert states[row["step"]["step_id"]] == "settled"


def test_the_owner_may_choose_an_excluded_member(world: dict[str, Any]) -> None:
    """R1 (T0) outranks R2: the owner's choice stands, and the exclusion stays visible."""
    env = world["env"]
    found = None
    for summary in env.svc.adoption_families(env.project_id, OWNER, world["gen"])["families"]:
        family = env.svc.adoption_family(env.project_id, OWNER, world["gen"], summary["family_id"])
        excluded = [m["path"] for m in family["members"] if m["outcome"] == "excluded"]
        if excluded:
            found = family["family_id"], excluded[0]
            break
    assert found, "the corpus has an excluded member (an autosave or upgrade backup)"
    fid, path = found
    decided = env.svc.adoption_decide(
        env.project_id, world["gen"], fid, path, "I really use this one", authority=OWNER_ACT
    )
    assert decided["current"] == path and decided["deciding_rule"] == "R1"
    assert any(c["kind"] == "owner_overrode_exclusion" for c in decided["conflicts"])


def test_the_owner_decides_accepts_a_baseline_and_gets_a_new_version(
    own_world: dict[str, Any],
) -> None:
    """On its own store: it reports the validated result it expects to see accepted, instead of
    relying on an earlier test's report on the shared walk (decision 0068)."""
    world = own_world
    env = world["env"]
    steps = steps_of(world)
    dedupe = next(r for r in steps if r["step"]["kind"] == "dedupe_proposal")["step"]
    reported = env.svc.adoption_step_report(
        env.project_id, AGENT, world["gen"], dedupe["step_id"], good_dedupe_result(dedupe)
    )
    assert reported["state"] == "validated", reported
    resolve = [r for r in steps if r["step"]["kind"] == "resolve_conflict"]
    assert resolve, "the corpus has families for the owner to decide"
    for row in resolve:
        fid = row["step"]["family_ids"][0]
        family = env.svc.adoption_family(env.project_id, OWNER, world["gen"], fid)
        choice = family["current"] or family["members"][0]["path"]
        env.svc.adoption_decide(
            env.project_id, world["gen"], fid, choice, "checked by hand", authority=OWNER_ACT
        )
        decided = env.svc.adoption_family(env.project_id, OWNER, world["gen"], fid)
        assert decided["current"] == choice and decided["deciding_rule"] == "R1"
    accepted = env.svc.adoption_baseline_accept(
        env.project_id, world["gen"], "first baseline", authority=OWNER_ACT
    )
    assert accepted["baseline_id"].startswith("bl_") and accepted["plan"]["version"] >= 2
    assert accepted["accepted_steps"] >= 1
    baseline = env.svc.adoption_baseline(env.project_id, OWNER, accepted["baseline_id"])
    assert baseline["payload"]["families"] and baseline["sha256"]
    assert dedupe["step_id"] in baseline["payload"]["accepted_steps"]
    newest = steps_of(world)
    assert not [
        r for r in newest if r["step"]["kind"] == "resolve_conflict" and r["state"] == "ready"
    ]
    validated = [r for r in newest if r["state"] == "accepted"]
    assert validated, "validated results were accepted with the baseline"
    assert dedupe["step_id"] in {r["step"]["step_id"] for r in validated}
    assert tree_digest(world["root"]) == world["digest_before"], "nothing touched the folder"


def test_the_baseline_test_passes_alone_and_first() -> None:
    """Regression for the order dependence (decision 0068): the baseline test passes run alone,
    and run first with the rest of the walk after it (which it must not disturb). The walk's
    other tests stay ordered on purpose; they share the module store."""
    target = "test_the_owner_decides_accepts_a_baseline_and_gets_a_new_version"
    here = Path(__file__).resolve()
    walk = [
        name
        for name, obj in globals().items()
        if name.startswith("test_")
        and callable(obj)
        and name not in (target, "test_the_baseline_test_passes_alone_and_first")
    ]
    for names in ([target], [target, *walk]):
        ids = [f"{here}::{name}" for name in names]
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", *ids],
            cwd=here.parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
        assert run.returncode == 0, (names[:2], run.stdout[-4000:], run.stderr[-2000:])
        assert f"{len(names)} passed" in run.stdout, run.stdout[-2000:]


def test_nothing_was_written_or_deleted(world: dict[str, Any]) -> None:
    """C17-INVENTORY across the whole plan: scan, plan, reports, decisions and baseline never
    touched the adopted folder (names, bytes and file times)."""
    assert tree_digest(world["root"]) == world["digest_before"]


def test_the_adopt_folder_skill_names_real_operations() -> None:
    """The bundled skill tells an agent which family sub-operations to call: each must exist,
    and the owner acts it names must be approval kinds (requests, never self-granted)."""
    import re
    from importlib import resources

    from prometheos import agentops, skills
    from prometheos.app import approval_kinds

    agentops.load()
    assert "adopt-folder" in skills.BUNDLED
    text = resources.files("prometheos").joinpath("skills_data/adopt-folder/SKILL.md")
    body = text.read_text(encoding="utf-8")
    subs = {
        op.sub
        for op in agentops.OPS.values()
        if op.tool in ("adoption_read", "adoption_manage") and op.sub
    }
    named = set(re.findall(r'"op": "([a-z_]+)"', body))
    assert named and named <= subs, named - subs
    for owner_act in ("adoption_decide", "adoption_baseline_accept", "adoption_export_markdown"):
        assert owner_act in body and owner_act in approval_kinds.KINDS
    for rule in ("Never write, move, rename or delete", "data, not instructions"):
        assert rule in body


def test_the_agent_plugin_readme_names_every_bundled_skill() -> None:
    """Review cp17-merge-a F1: the CP19 plugin's README lists the skills it ships, adopt-folder
    included, instead of a hand-written list of four."""
    from prometheos import agent_package, skills

    texts = {name: f"---\nname: {name}\n---\n" for name in skills.BUNDLED}
    files = agent_package.plugin_files("project_x", texts)
    readme = next(text for path, text in files.items() if path.endswith("README.md"))
    for name in skills.BUNDLED:
        assert name in readme, name
