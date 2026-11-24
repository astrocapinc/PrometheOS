"""C20-STATUS, completed (A06.1; decisions 0082 and 0106): every stale cause, stale before
corrected, the same-definition rule, paper readiness reading the derived status, and a mutation
check per rule (each test notices when its rule is removed).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from cp7_support import create, paper_body
from test_cp20_claim_status import assess, claim, imported, line_ev, receipt, status

from prometheos.app import claim_status
from prometheos.domain.ids import new_id


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "script.m").write_text("energy = 196.9;\n", "utf-8")
    (environment.research / "result.csv").write_text(
        "name = value\nenergy_MJ = 196.9\nenergy_MJ = 63.6\n", "utf-8"
    )
    return environment


def assumption(env: Env, text: str = "Placeholder damper 0.01.") -> tuple[str, dict[str, Any]]:
    eid = new_id("entry")
    body = {
        "title": "Ring damper", "text": text, "category": "assumption", "evidence": [],
        "claim_refs": [], "author_ref": None, "origin": "agent", "quantities": [],
        "review": "unreviewed",
    }  # fmt: skip
    env.apply([create("entry", body, eid)], caller=AGENT)
    return eid, body


def depends(env: Env, cid: str, target: str, rev: int = 1, claim_rev: int = 1) -> None:
    env.apply(
        [{"op": "relation_add", "source_ref": {"id": cid, "rev": claim_rev},
          "target_ref": {"id": target, "rev": rev}, "relation": "depends_on"}],
        caller=AGENT,
    )  # fmt: skip


def revise(env: Env, record_id: str, base_rev: int, body: dict[str, Any]) -> None:
    env.apply(
        [{"op": "revise", "id": record_id, "base_rev": base_rev, "sensitivity": "normal",
          "occurred_at": None, "body": body}],
        caller=AGENT,
    )  # fmt: skip


def codes(env: Env, cid: str) -> set[str]:
    return {c["code"] for c in status(env, cid)["causes"]}


def run_claim(env: Env) -> str:
    """A claim on an output a recorded run produced, with an agent's support."""
    return run_claim_files(env)[0]


def run_claim_files(env: Env) -> tuple[str, dict[str, dict[str, str]]]:
    files = imported(env, ["script.m", "result.csv"])
    receipt(env, [files["script.m"]], [files["result.csv"]])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    return cid, files


# ----- the stale causes ------------------------------------------------------------------


def test_a_changed_origin_makes_the_claim_stale(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    assert status(env, cid)["status"] == "holds"
    (env.research / "result.csv").write_text("energy_MJ = 63.6\n", "utf-8")
    env.svc.source_check(env.project_id, [files["result.csv"]["version_id"]], save=True,
                         caller=OWNER)  # fmt: skip
    got = status(env, cid)
    assert (got["status"], got["usable"]) == ("stale", False)
    assert "origin_changed" in codes(env, cid)


def test_a_run_older_than_a_revision_of_its_assumption_stays_stale_after_relinking(
    env: Env,
) -> None:
    """Relinking to the revised assumption clears ``dependency_changed``, but the run behind the
    evidence was computed under the old assumption."""
    cid = run_claim(env)
    eid, body = assumption(env)
    depends(env, cid, eid)
    assert status(env, cid)["status"] == "holds"
    revise(env, eid, 1, body | {"text": "Physical damper 3e-4."})
    depends(env, cid, eid, rev=2)  # the claim now names the revised assumption
    got = codes(env, cid)
    assert "dependency_changed" not in got and "run_predates_change" in got
    assert status(env, cid)["status"] == "stale"


def test_a_run_older_than_an_applied_correction_of_its_dependency_is_stale(env: Env) -> None:
    cid = run_claim(env)
    old, _ = assumption(env)
    new, _ = assumption(env, "Physical damper 3e-4.")
    depends(env, cid, old)
    assert status(env, cid)["status"] == "holds"
    corr = new_id("correction")
    env.apply([create("correction", {
        "target_refs": [{"id": old, "rev": 1}], "replacement_refs": [{"id": new, "rev": 1}],
        "reason": "The damper is the physical one.", "evidence": [],
        "effects": [{"target_ref": {"id": old, "rev": 1}, "effect": "supersede",
                     "replacement_ref": {"id": new, "rev": 1}, "replacement_text": None}],
        "status": "proposed", "applied_seq": None,
    }, corr)], caller=AGENT)  # fmt: skip
    env.apply_protected([{"op": "correction_apply", "id": corr, "base_rev": 1}])
    assert "run_predates_change" in codes(env, cid)
    assert status(env, cid)["status"] == "stale"


def test_without_a_recorded_run_a_dependency_change_is_only_the_binding(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    eid, body = assumption(env)
    depends(env, cid, eid)
    revise(env, eid, 1, body | {"text": "Physical damper 3e-4."})
    depends(env, cid, eid, rev=2)
    assert status(env, cid)["status"] == "holds"  # no run was computed under the old one


def adoption_claim(env: Env) -> tuple[str, Path]:
    """A claim on a file a script in the adopted folder writes (no recorded run): the producer
    edge comes from the adoption scan, mapped to the cited version by its digest."""
    old = 1_760_000_000
    script = env.research / "run_energy.m"
    script.write_text("T = table(196.9);\nwritetable(T, 'energy.csv');\n", "utf-8")
    out = env.research / "energy.csv"
    out.write_text("energy_MJ\n196.9\n", "utf-8")
    os.utime(script, (old, old))
    os.utime(out, (old + 60, old + 60))
    files = imported(env, ["energy.csv"])
    cid = claim(env, [line_ev(files["energy.csv"], 2)])
    assess(env, cid)
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    return cid, script


def confirm_outline(env: Env) -> None:
    gen = env.svc.adoption_scans(env.project_id, AGENT)["scans"][0]["scan_gen"]
    outline = env.svc.adoption_outline(env.project_id, AGENT, gen)
    env.svc.adoption_outline_confirm(env.project_id, gen, outline["sha256"], {},
                                     authority=OWNER_ACT)  # fmt: skip


def test_a_stale_candidate_the_owner_kept_makes_the_claim_stale(env: Env) -> None:
    """The script is newer than its output. A time alone proves nothing (R1-F1); once the owner
    keeps the stale candidate in the confirmed outline, the claim is stale."""
    cid, script = adoption_claim(env)
    assert status(env, cid)["status"] == "holds"
    os.utime(script, (1_760_000_600, 1_760_000_600))  # now newer than energy.csv
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    assert status(env, cid)["status"] == "holds"  # a modification time alone
    confirm_outline(env)
    got = status(env, cid)
    assert got["status"] == "stale" and "producer_changed" in codes(env, cid)
    cause = next(c for c in got["causes"] if c["code"] == "producer_changed")
    assert cause["ref"] == "research:run_energy.m" and "energy.csv" in cause["message"]
    assert "stale candidate you kept" in cause["message"]


def test_checkout_order_times_alone_never_make_a_claim_stale(env: Env) -> None:
    """Review cp20-a-r1 R1-F1 probe: identical bytes, the script one second newer."""
    t = 1_760_000_000
    script = env.research / "run_energy.m"
    script.write_text("T = table(196.9);\nwritetable(T, 'energy.csv');\n", "utf-8")
    out = env.research / "energy.csv"
    out.write_text("energy_MJ\n196.9\n", "utf-8")
    os.utime(out, (t, t))
    os.utime(script, (t + 1, t + 1))
    files = imported(env, ["energy.csv"])
    cid = claim(env, [line_ev(files["energy.csv"], 2)])
    assess(env, cid)
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    assert status(env, cid)["status"] == "holds"


def test_the_same_bytes_in_another_track_never_borrow_its_producer(env: Env) -> None:
    """Review cp20-a-r1 R1-F1 probe: track2's file is cited; track5 has the same bytes and its
    own changed script. Only track2's own producer edge counts."""
    t = 1_760_000_000
    for track in ("track2", "track5"):
        (env.research / track).mkdir()
        script = env.research / track / "run_energy.m"
        script.write_text(f"% {track}\nT = table(196.9);\nwritetable(T, 'energy.csv');\n", "utf-8")
        out = env.research / track / "energy.csv"
        out.write_text("energy_MJ\n196.9\n", "utf-8")
        os.utime(script, (t, t))
        os.utime(out, (t + 60, t + 60))
    files = imported(env, ["track2/energy.csv"])
    cid = claim(env, [line_ev(files["track2/energy.csv"], 2)])
    assess(env, cid)
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    confirm_outline(env)
    five = env.research / "track5" / "run_energy.m"
    five.write_text("% track5, changed\nT = table(241.3);\nwritetable(T, 'energy.csv');\n", "utf-8")
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    got = status(env, cid)
    assert got["status"] == "holds" and not any("track5" in c["message"] for c in got["causes"])


def test_a_review_only_revision_of_an_assumption_leaves_the_run_valid(env: Env) -> None:
    """Review cp20-a-r1 R1-F2 probe: the owner only marks the assumption reviewed."""
    cid = run_claim(env)
    eid, body = assumption(env)
    depends(env, cid, eid)
    env.apply_protected(
        [{"op": "revise", "id": eid, "base_rev": 1, "sensitivity": "normal",
          "occurred_at": None, "body": body | {"review": "reviewed"}}]
    )  # fmt: skip
    depends(env, cid, eid, rev=2)
    assert "run_predates_change" not in codes(env, cid)
    assert status(env, cid)["status"] == "holds"


def test_a_script_changed_since_the_confirmed_outline_makes_the_claim_stale(env: Env) -> None:
    cid, script = adoption_claim(env)
    confirm_outline(env)
    # The script's bytes change but its time is put back before the output's.
    script.write_text("T = table(63.6);\nwritetable(T, 'energy.csv');\n", "utf-8")
    os.utime(script, (1_760_000_000, 1_760_000_000))
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    cause = next(c for c in status(env, cid)["causes"] if c["code"] == "producer_changed")
    assert "since you confirmed the outline" in cause["message"]


# ----- precedence and the same-definition rule ------------------------------------------


def test_a_claim_both_stale_and_corrected_shows_stale(env: Env) -> None:
    files = imported(env, ["script.m", "result.csv"])
    receipt(env, [files["script.m"]], [files["result.csv"]])
    old = claim(env, [line_ev(files["result.csv"], 3)])
    new = claim(env, [line_ev(files["result.csv"], 2)])
    corr = new_id("correction")
    env.apply([create("correction", {
        "target_refs": [{"id": old, "rev": 1}], "replacement_refs": [{"id": new, "rev": 1}],
        "reason": "Replaced.", "evidence": [],
        "effects": [{"target_ref": {"id": old, "rev": 1}, "effect": "supersede",
                     "replacement_ref": {"id": new, "rev": 1}, "replacement_text": None}],
        "status": "proposed", "applied_seq": None,
    }, corr)], caller=AGENT)  # fmt: skip
    env.apply_protected([{"op": "correction_apply", "id": corr, "base_rev": 1}])
    assess(env, new)
    assert status(env, new)["status"] == "corrected"
    (env.research / "script.m").write_text("energy = 63.6;\n", "utf-8")
    imported(env, ["script.m"])
    got = status(env, new)
    assert (got["status"], got["basis"], got["usable"]) == ("stale", None, False)
    assert {"producer_newer", "replaced"} <= codes(env, new)  # both causes are kept


# ----- paper readiness reads the derived status -------------------------------------------


def test_paper_readiness_never_offers_a_stale_claim_and_lists_agent_support(env: Env) -> None:
    cid = run_claim(env)
    pid = new_id("paper")
    env.apply([create("paper", paper_body(claim_refs=[{"id": cid, "rev": 1}]), pid)])
    found = {f["code"] for f in env.svc.paper_check(env.project_id, OWNER, pid)["findings"]}
    assert "claim_agent_reported" in found and "claim_not_usable" not in found
    (env.research / "script.m").write_text("energy = 63.6;\n", "utf-8")
    imported(env, ["script.m"])
    findings = env.svc.paper_check(env.project_id, OWNER, pid)["findings"]
    (stale,) = [f for f in findings if f["code"] == "claim_not_usable"]
    assert stale["detail"] == {"status": "stale", "cause": "producer_newer"}
    assert "claim_agent_reported" not in {f["code"] for f in findings}


# ----- mutation checks: each rule, removed, changes what the tests above see ----------------


@pytest.mark.parametrize(
    ("scenario", "target", "attribute", "replacement"),
    [
        ("origin", claim_status, "_origin_changed", lambda *a: False),
        ("dependency", claim_status, "_changed_dependencies", lambda *a: []),
        ("run", claim_status, "_dependency_changes", lambda *a: []),
        ("adoption", claim_status._Index, "_adoption_producers", lambda self: {}),
    ],
)
def test_mutation_each_stale_cause_removed_and_the_claim_would_hold(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    target: Any,
    attribute: str,
    replacement: Any,
) -> None:
    if scenario == "origin":
        files = imported(env, ["result.csv"])
        cid = claim(env, [line_ev(files["result.csv"], 2)])
        assess(env, cid)
        (env.research / "result.csv").write_text("energy_MJ = 63.6\n", "utf-8")
        env.svc.source_check(env.project_id, [files["result.csv"]["version_id"]], save=True,
                             caller=OWNER)  # fmt: skip
    elif scenario in ("dependency", "run"):
        # Without a recorded run only the binding rule sees the change (the run rule would
        # otherwise see it too).
        if scenario == "run":
            cid = run_claim(env)
        else:
            files = imported(env, ["result.csv"])
            cid = claim(env, [line_ev(files["result.csv"], 2)])
            assess(env, cid)
        eid, body = assumption(env)
        depends(env, cid, eid)
        revise(env, eid, 1, body | {"text": "Physical damper 3e-4."})
        if scenario == "run":
            depends(env, cid, eid, rev=2)
    else:
        cid, script = adoption_claim(env)
        confirm_outline(env)
        script.write_text("T = table(63.6);\nwritetable(T, 'energy.csv');\n", "utf-8")
        env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    assert status(env, cid)["status"] == "stale"
    monkeypatch.setattr(target, attribute, replacement)
    assert status(env, cid)["status"] == "holds"


def test_mutation_without_the_same_definition_rule_two_definitions_would_conflict(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = imported(env, ["result.csv"])
    line2, line3 = line_ev(files["result.csv"], 2), line_ev(files["result.csv"], 3)

    def binding(ev: dict[str, Any], value: str, line: int, definition: str) -> dict[str, Any]:
        return {"decimal": value, "unit": "", "source_ref": ev,
                "source_pointer": f"text:{line}:12:{12 + len(value)}", "name": "uphill energy",
                "definition": definition}  # fmt: skip

    cid = claim(env, [line2], quantities=[binding(line2, "196.9", 2, "stored cycle")])
    env.apply([create("entry", {
        "title": "Ledger", "text": "Second reading.", "category": "finding", "evidence": [line3],
        "claim_refs": [{"id": cid, "rev": 1}], "author_ref": None, "origin": "agent",
        "quantities": [binding(line3, "63.6", 3, "published climb")], "review": "unreviewed",
    })], caller=AGENT)  # fmt: skip
    assess(env, cid)
    assert status(env, cid)["status"] == "holds"
    monkeypatch.setattr(claim_status, "_quantity_key", lambda q: (q["name"], None))
    assert status(env, cid)["status"] == "conflicting"


def test_the_new_stale_causes_get_a_planned_rerun_that_needs_the_owner(env: Env) -> None:
    cid, script = adoption_claim(env)
    confirm_outline(env)
    script.write_text("T = table(63.6);\nwritetable(T, 'energy.csv');\n", "utf-8")
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    env.svc.verification_propose(env.project_id, AGENT)
    checks = [c for c in env.svc.verification_plan(env.project_id, AGENT)["checks"]
              if c["claim_id"] == cid]  # fmt: skip
    (rerun,) = [c for c in checks if c["cause"] == "producer_changed"]
    assert rerun["kind"] == "rerun" and rerun["eligible"] is False
    # The map's one-line reason never names a file.
    rmap = env.svc.research_map(env.project_id, OWNER)
    assert "run_energy.m" not in str([q for d in rmap["deliverables"] for q in d["questions"]])


# ----- review cp20-a-r2 R2-F1: the outline speaks only about the bytes it recorded -----------


def rerun_and_cite(env: Env, value: str, when: int) -> str:
    out = env.research / "energy.csv"
    out.write_text(f"energy_MJ\n{value}\n", "utf-8")
    os.utime(out, (when, when))
    files = imported(env, ["energy.csv"])
    cid = claim(env, [line_ev(files["energy.csv"], 2)])
    assess(env, cid)
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    return cid


def test_after_the_planned_rerun_a_claim_on_the_new_output_holds(env: Env) -> None:
    old, script = adoption_claim(env)
    os.utime(script, (1_760_000_600, 1_760_000_600))
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    confirm_outline(env)  # the stale candidate is kept
    assert status(env, old)["status"] == "stale"
    new = rerun_and_cite(env, "196.95", 1_760_000_900)
    assert status(env, new)["status"] == "holds"


def test_after_a_changed_script_is_rerun_its_own_output_holds(env: Env) -> None:
    _old, script = adoption_claim(env)
    confirm_outline(env)
    script.write_text("T = table(63.6);\nwritetable(T, 'energy.csv');\n", "utf-8")
    os.utime(script, (1_760_000_600, 1_760_000_600))
    new = rerun_and_cite(env, "63.6", 1_760_000_900)
    assert status(env, new)["status"] == "holds"


def test_no_producer_edge_once_the_bound_file_has_other_bytes(env: Env) -> None:
    """The guard that the bound file still has the version's bytes: a claim on an older version
    is judged by the origin rule (a source check), never by the new file's producer."""
    cid, script = adoption_claim(env)
    confirm_outline(env)
    (env.research / "energy.csv").write_text("energy_MJ\n1.0\n", "utf-8")
    script.write_text("T = table(1.0);\nwritetable(T, 'energy.csv');\n", "utf-8")
    env.svc.adoption_scan(env.project_id, AGENT, root_alias="research", mode="wait")
    assert "producer_changed" not in codes(env, cid)
