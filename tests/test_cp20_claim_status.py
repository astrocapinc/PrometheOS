"""CP20.1 (A06.1; C20-STATUS; decision 0082): each claim's derived status.

One status per claim, with all its causes, by the precedence withdrawn, superseded, stale,
conflicting, settling, unverified, corrected, holds. holds and corrected name their basis,
decided by who recorded the assessment and never by its method field. The status is a pure,
rebuildable read: it changes no record and never promotes a claim.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env
from cp7_support import create
from mcp import Client
from test_cp18_events import store_digest

from prometheos.app import claim_status
from prometheos.app.reads import Caller
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server

HUB_OWNER = Caller(actor="hub-owner", scope="owner")
SCOPE = {
    "system": "Synthetic plant",
    "operating_regime": "Urban cycle",
    "comparator": "Same vehicle without the device",
    "model_fidelity": "Reduced model",
    "validation_level": "Re-derived from the stored artefact",
}


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "script.m").write_text("energy = 196.9;\n", "utf-8")
    (environment.research / "result.csv").write_text(
        "name = value\nenergy_MJ = 196.9\nenergy_MJ = 63.6\n", "utf-8"
    )
    (environment.research / "result.mat").write_bytes(b"MATLAB 5.0 MAT-file synthetic" * 20)
    return environment


def imported(env: Env, paths: list[str]) -> dict[str, dict[str, str]]:
    plan = env.import_files(paths)["plan"]
    return {
        i["relative_path"]: {
            "source_id": i["source_id"],
            "version_id": i["version_id"],
            "sha256": i["sha256"],
        }
        for i in plan["items"]
    }


def line_ev(item: dict[str, str], line: int) -> dict[str, Any]:
    return {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": line, "end_line": line},
        "quote_sha256": None,
    }


def claim(env: Env, evidence: list[dict[str, Any]], **extra: Any) -> str:
    cid = new_id("claim")
    body = {
        "statement": "The uphill energy is about 196.9 MJ on the stored cycle.",
        "scope": SCOPE,
        "evidence": evidence,
        "assessment_refs": [],
        "state": "proposed",
        "superseded_by": None,
    } | extra
    env.apply([create("claim", body, cid)], caller=AGENT)
    return cid


def assess(
    env: Env,
    cid: str,
    outcome: str = "supports",
    *,
    caller: Caller = AGENT,
    method: str = "reported_agent",
    checks: list[dict[str, Any]] | None = None,
) -> str:
    aid = new_id("assessment")
    body = {
        "claim_ref": {"id": cid, "rev": 1},
        "outcome": outcome,
        "rationale": "Re-derived from the stored artefact.",
        "checks": checks or [],
        "author_ref": None,
        "method": method,
        "scope_match": True,
    }
    env.apply([create("assessment", body, aid)], caller=caller)
    return aid


def status(env: Env, cid: str, caller: Caller = OWNER) -> dict[str, Any]:
    found = env.svc.claim_status(env.project_id, caller, claim_ids=[cid])["claims"]
    assert len(found) == 1
    return found[0]


def receipt(env: Env, inputs: list[dict[str, str]], outputs: list[dict[str, str]]) -> None:
    body = {
        "schema_version": 1,
        "observation": "reported",
        "reporter": "test agent",
        "command": "run('script.m')",
        "working_root": "research",
        "environment": {"os": "TestOS", "tools": [{"name": "MATLAB", "version": "R2025b"}]},
        "started_at": "2026-10-05T06:36:30Z",
        "ended_at": "2026-10-05T06:36:41Z",
        "exit_status": 0,
        "outcome": "succeeded",
        "inputs": [i | {"role": "script"} for i in inputs],
        "outputs": [o | {"role": "result"} for o in outputs],
        "limitations": ["synthetic test"],
    }
    env.svc.receipt_record(
        env.project_id,
        raw=json.dumps(body).encode("utf-8"),
        title="run 1",
        description="",
        request_id=new_request_id(),
    )


# ----- each status ---------------------------------------------------------------------


def test_unverified_until_an_assessment_and_holds_with_the_agents_basis(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    first = status(env, cid)
    assert (first["status"], first["basis"], first["usable"]) == ("unverified", None, True)
    assert [c["code"] for c in first["causes"]] == ["no_assessment"]
    assess(env, cid)
    after = status(env, cid)
    assert (after["status"], after["basis"]) == ("holds", "agent_reported")


def test_the_basis_comes_from_who_recorded_it_never_from_the_method_field(env: Env) -> None:
    """Review a06-1-r2 F2: an agent's assessment that declares itself the researcher's is still
    the agent's word; one written in the paired owner session is reviewed."""
    files = imported(env, ["result.csv"])
    by_agent = claim(env, [line_ev(files["result.csv"], 2)])
    # An agent cannot record a researcher-method assessment without the owner (existing guard)...
    with pytest.raises(PrometheosError, match="owner's approval"):
        assess(env, by_agent, method="researcher")
    # ...nor one labelled a deterministic check, which only PrometheOS produces (existing guard).
    with pytest.raises(PrometheosError, match="produced by PrometheOS"):
        assess(env, by_agent, method="deterministic_check")
    assess(env, by_agent)
    assert status(env, by_agent)["basis"] == "agent_reported"
    by_owner = claim(env, [line_ev(files["result.csv"], 2)])
    body = {
        "claim_ref": {"id": by_owner, "rev": 1},
        "outcome": "supports",
        "rationale": "I re-read it.",
        "checks": [],
        "author_ref": None,
        "method": "researcher",
        "scope_match": True,
    }
    env.apply_protected([create("assessment", body, new_id("assessment"))], caller=HUB_OWNER)
    assert status(env, by_owner)["basis"] == "reviewed"


def test_a_newer_producer_makes_the_evidence_stale(env: Env) -> None:
    files = imported(env, ["script.m", "result.csv"])
    receipt(env, [files["script.m"]], [files["result.csv"]])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    assert status(env, cid)["status"] == "holds"
    (env.research / "script.m").write_text("energy = 63.6;  % published climb\n", "utf-8")
    imported(env, ["script.m"])
    stale = status(env, cid)
    assert (stale["status"], stale["usable"], stale["basis"]) == ("stale", False, None)
    assert stale["causes"][0]["code"] == "producer_newer"
    assert "script.m" in stale["causes"][0]["message"]


def test_challenged_and_proposed_corrections_are_settling(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    assess(env, cid, "challenges")
    challenged = status(env, cid)
    assert challenged["status"] == "settling" and challenged["usable"] is True
    assert "challenged" in [c["code"] for c in challenged["causes"]]
    other = claim(env, [line_ev(files["result.csv"], 2)])
    correction = {
        "target_refs": [{"id": other, "rev": 1}],
        "replacement_refs": [],
        "reason": "Quote the published climb instead.",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": other, "rev": 1},
                "effect": "withdraw",
                "replacement_ref": None,
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    env.apply([create("correction", correction)], caller=AGENT)
    assert "correction_proposed" in [c["code"] for c in status(env, other)["causes"]]


def test_inconclusive_assessments_leave_the_claim_unverified_with_that_cause(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid, "inconclusive")
    got = status(env, cid)
    assert got["status"] == "unverified" and [c["code"] for c in got["causes"]] == ["inconclusive"]


def test_same_quantity_twice_conflicts_and_two_definitions_do_not(env: Env) -> None:
    files = imported(env, ["result.csv"])
    line2, line3 = line_ev(files["result.csv"], 2), line_ev(files["result.csv"], 3)

    def binding(ev: dict[str, Any], value: str, line: int, definition: str) -> dict[str, Any]:
        return {
            "decimal": value,
            "unit": "",
            "source_ref": ev,
            "source_pointer": f"text:{line}:12:{12 + len(value)}",
            "name": "uphill energy",
            "definition": definition,
        }

    def entry_for(cid: str, q: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": "Ledger",
            "text": "Second reading.",
            "category": "finding",
            "evidence": [line3],
            "claim_refs": [{"id": cid, "rev": 1}],
            "author_ref": None,
            "origin": "agent",
            "quantities": [q],
            "review": "unreviewed",
        }

    same = claim(env, [line2], quantities=[binding(line2, "196.9", 2, "stored cycle")])
    env.apply(
        [create("entry", entry_for(same, binding(line3, "63.6", 3, "stored cycle")))], caller=AGENT
    )
    assess(env, same)
    got = status(env, same)
    assert (got["status"], got["usable"]) == ("conflicting", True)
    assert "uphill energy is bound as 196.9" in got["causes"][0]["message"]
    two = claim(env, [line2], quantities=[binding(line2, "196.9", 2, "stored cycle")])
    env.apply(
        [create("entry", entry_for(two, binding(line3, "63.6", 3, "published climb")))],
        caller=AGENT,
    )
    assess(env, two)
    assert status(env, two)["status"] == "holds"  # two definitions are never a conflict


def test_superseded_and_withdrawn_are_never_usable_and_precede_everything(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    corr = new_id("correction")
    correction = {
        "target_refs": [{"id": cid, "rev": 1}],
        "replacement_refs": [],
        "reason": "The stored cycle applied the steepest grade everywhere.",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": cid, "rev": 1},
                "effect": "withdraw",
                "replacement_ref": None,
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    env.apply([create("correction", correction, corr)], caller=AGENT)
    env.apply_protected([{"op": "correction_apply", "id": corr, "base_rev": 1}])
    got = status(env, cid)
    assert (got["status"], got["usable"]) == ("withdrawn", False)


# ----- record additions ----------------------------------------------------------------


def test_a_summary_may_state_only_bound_numbers(env: Env) -> None:
    files = imported(env, ["result.csv"])
    ev = line_ev(files["result.csv"], 2)
    with pytest.raises(PrometheosError, match="summary states numbers"):
        claim(env, [ev], summary="Uphill energy is 196.9 MJ")
    q = {"decimal": "196.9", "unit": "", "source_ref": ev, "source_pointer": "text:2:12:17"}
    cid = claim(env, [ev], summary="Uphill energy is 196.9 on the stored cycle", quantities=[q])
    assert status(env, cid)["summary"] == "Uphill energy is 196.9 on the stored cycle"


def test_a_whole_file_reference_binds_a_digest_not_a_quote_or_a_quantity(env: Env) -> None:
    files = imported(env, ["result.mat"])
    mat = files["result.mat"]
    whole = {
        "source_id": mat["source_id"],
        "version_id": mat["version_id"],
        "locator": {"type": "file"},
        "quote_sha256": None,
    }
    cid = claim(env, [whole])
    assert status(env, cid)["status"] == "unverified"
    quoted = whole | {"quote_sha256": hashlib.sha256(b"x").hexdigest()}
    with pytest.raises(PrometheosError):
        claim(env, [quoted])
    q = {"decimal": "196.9", "unit": "MJ", "source_ref": whole, "source_pointer": "energy"}
    with pytest.raises(PrometheosError, match="text or JSON locator"):
        claim(env, [whole], quantities=[q])


def test_a_passing_check_with_an_expected_value_must_state_what_it_measured(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    good = {
        "name": "R1 re-derivation",
        "result": "pass",
        "evidence": [],
        "expected": "~200 MJ",
        "measured": "196.9 MJ",
        "tolerance": "order of magnitude",
    }
    assess(env, cid, checks=[good])
    with pytest.raises(PrometheosError):
        assess(env, cid, checks=[{k: v for k, v in good.items() if k != "measured"}])


# ----- a pure read, on every transport -------------------------------------------------


def test_the_status_read_changes_nothing_and_matches_through_mcp(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    before = store_digest(env.store_dir)
    service = env.svc.claim_status(env.project_id, AGENT)

    async def main() -> dict[str, Any]:
        async with Client(build_server(env.svc, env.project_id)) as client:
            reply = await client.call_tool("claim_status", {"claim_ids": [cid]})
            assert not reply.is_error, reply
            return dict(reply.structured_content or {})

    via_mcp = anyio.run(main)
    assert store_digest(env.store_dir) == before
    assert via_mcp["claims"] == service["claims"]
    assert service["counts"]["holds"] == 1
    record = env.svc.get(env.project_id, OWNER, cid, None)
    assert record["body"]["state"] == "proposed"  # never promoted


# ----- mutation checks: the tests above notice when a rule is removed ------------------


def test_mutation_without_the_producer_rule_the_stale_claim_would_hold(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = imported(env, ["script.m", "result.csv"])
    receipt(env, [files["script.m"]], [files["result.csv"]])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    (env.research / "script.m").write_text("energy = 63.6;\n", "utf-8")
    imported(env, ["script.m"])
    assert status(env, cid)["status"] == "stale"
    monkeypatch.setattr(claim_status._Index, "newer_version", lambda *a: None)
    assert status(env, cid)["status"] == "holds"


def test_mutation_precedence_decides_between_stale_and_holds(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = imported(env, ["script.m", "result.csv"])
    receipt(env, [files["script.m"]], [files["result.csv"]])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    (env.research / "script.m").write_text("energy = 63.6;\n", "utf-8")
    imported(env, ["script.m"])
    reordered = (*(s for s in claim_status.PRECEDENCE if s != "stale"), "stale")
    monkeypatch.setattr(claim_status, "PRECEDENCE", reordered)
    assert status(env, cid)["status"] == "holds"


# ----- review cp20-1-a ----------------------------------------------------------------


def test_hidden_records_never_shape_what_an_agent_reads(env: Env) -> None:
    """F1: a restricted entry, assessment or version changes nothing the agent sees, and its
    content never reaches the agent; the owner sees the full picture."""
    files = imported(env, ["result.csv"])
    line2, line3 = line_ev(files["result.csv"], 2), line_ev(files["result.csv"], 3)
    q2 = {
        "decimal": "196.9",
        "unit": "",
        "source_ref": line2,
        "source_pointer": "text:2:12:17",
        "name": "SECRET-limit",
        "definition": "d",
    }
    q3 = {
        "decimal": "63.6",
        "unit": "",
        "source_ref": line3,
        "source_pointer": "text:3:12:16",
        "name": "SECRET-limit",
        "definition": "d",
    }
    cid = claim(env, [line2], quantities=[q2])
    hidden_entry = {
        "title": "Restricted ledger",
        "text": "Second reading.",
        "category": "finding",
        "evidence": [line3],
        "claim_refs": [{"id": cid, "rev": 1}],
        "author_ref": None,
        "origin": "agent",
        "quantities": [q3],
        "review": "unreviewed",
    }
    env.apply([create("entry", hidden_entry, sensitivity="restricted")], caller=OWNER)
    assess(env, cid)
    challenge = {
        "claim_ref": {"id": cid, "rev": 1},
        "outcome": "challenges",
        "rationale": "Restricted re-reading.",
        "checks": [],
        "author_ref": None,
        "method": "reported_agent",
        "scope_match": True,
    }
    env.apply([create("assessment", challenge, sensitivity="restricted")], caller=OWNER)
    agent = env.svc.claim_status(env.project_id, AGENT, claim_ids=[cid])
    assert agent["claims"][0]["status"] == "holds"
    assert "SECRET" not in json.dumps(agent) and "63.6" not in json.dumps(agent)
    owner = status(env, cid, OWNER)
    assert owner["status"] == "conflicting"
    assert {"same_quantity_two_values", "challenged"} <= {c["code"] for c in owner["causes"]}


def test_an_agent_assessment_bundled_with_an_owner_decision_stays_the_agents(env: Env) -> None:
    """F3: the basis is the assessment's own, not the commit's."""
    files = imported(env, ["result.csv"])
    agents, owners = (claim(env, [line_ev(files["result.csv"], 2)]) for _ in range(2))

    def body(cid: str, method: str) -> dict[str, Any]:
        return {
            "claim_ref": {"id": cid, "rev": 1},
            "outcome": "supports",
            "rationale": "x",
            "checks": [],
            "author_ref": None,
            "method": method,
            "scope_match": True,
        }

    env.apply_protected(
        [
            create("assessment", body(agents, "reported_agent")),
            create("assessment", body(owners, "researcher")),
        ]
    )
    assert status(env, agents)["basis"] == "agent_reported"
    assert status(env, owners)["basis"] == "reviewed"


def test_a_changed_dependency_stays_stale_after_the_claim_is_revised(env: Env) -> None:
    """F5: derived from the depends_on relation, not from a view flag a revise clears."""
    files = imported(env, ["result.csv"])
    ev = line_ev(files["result.csv"], 2)
    assumption = new_id("entry")
    damper = {
        "title": "Ring damper",
        "text": "Placeholder damper 0.01.",
        "category": "assumption",
        "evidence": [],
        "claim_refs": [],
        "author_ref": None,
        "origin": "agent",
        "quantities": [],
        "review": "unreviewed",
    }
    env.apply([create("entry", damper, assumption)], caller=AGENT)
    cid = claim(env, [ev])
    env.apply(
        [
            {
                "op": "relation_add",
                "source_ref": {"id": cid, "rev": 1},
                "target_ref": {"id": assumption, "rev": 1},
                "relation": "depends_on",
            }
        ],
        caller=AGENT,
    )
    assess(env, cid)
    assert status(env, cid)["status"] == "holds"
    env.apply(
        [
            {
                "op": "revise",
                "id": assumption,
                "base_rev": 1,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": damper | {"text": "Physical damper 3e-4."},
            }
        ],
        caller=AGENT,
    )
    assert status(env, cid)["status"] == "stale"
    current = env.svc.get(env.project_id, OWNER, cid, None)
    env.apply(
        [
            {
                "op": "revise",
                "id": cid,
                "base_rev": 1,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": current["body"] | {"statement": "The uphill energy is about 197 MJ."},
            }
        ],
        caller=AGENT,
    )
    after = status(env, cid)
    assert after["status"] == "stale"
    assert "revised_since" in {c["code"] for c in after["causes"]}  # F6: the old check is old


def test_only_the_owner_or_a_decided_correction_settles_a_challenge(env: Env) -> None:
    """F7: an agent's later support does not clear a challenge; the owner's does."""
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid, "challenges")
    assess(env, cid)
    assert status(env, cid)["status"] == "settling"
    body = {
        "claim_ref": {"id": cid, "rev": 1},
        "outcome": "supports",
        "rationale": "I checked.",
        "checks": [],
        "author_ref": None,
        "method": "researcher",
        "scope_match": True,
    }
    env.apply_protected([create("assessment", body)])
    settled = status(env, cid)
    assert (settled["status"], settled["basis"]) == ("holds", "reviewed")


def test_an_applied_supersession_makes_the_replacement_corrected(env: Env) -> None:
    files = imported(env, ["result.csv"])
    old = claim(env, [line_ev(files["result.csv"], 2)])
    new = claim(env, [line_ev(files["result.csv"], 3)])
    corr = new_id("correction")
    correction = {
        "target_refs": [{"id": old, "rev": 1}],
        "replacement_refs": [{"id": new, "rev": 1}],
        "reason": "The published climb is 63.6 MJ.",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": old, "rev": 1},
                "effect": "supersede",
                "replacement_ref": {"id": new, "rev": 1},
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    env.apply([create("correction", correction, corr)], caller=AGENT)
    env.apply_protected([{"op": "correction_apply", "id": corr, "base_rev": 1}])
    assess(env, new)
    assert (status(env, old)["status"], status(env, old)["usable"]) == ("superseded", False)
    replaced = status(env, new)
    assert (replaced["status"], replaced["basis"]) == ("corrected", "agent_reported")


def test_returning_to_the_same_bytes_is_not_newer(env: Env) -> None:
    files = imported(env, ["script.m", "result.csv"])
    receipt(env, [files["script.m"]], [files["result.csv"]])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    original = (env.research / "script.m").read_text("utf-8")
    (env.research / "script.m").write_text("energy = 63.6;\n", "utf-8")
    imported(env, ["script.m"])
    assert status(env, cid)["status"] == "stale"
    (env.research / "script.m").write_text(original, "utf-8")
    imported(env, ["script.m"])
    assert status(env, cid)["status"] == "holds"


# ----- review cp20-1-a round 2 -------------------------------------------------------


def promote(env: Env, cid: str, rev: int = 1) -> None:
    env.apply_protected(
        [
            {
                "op": "transition",
                "id": cid,
                "base_rev": rev,
                "to": "supported_within_conditions",
                "reason": "Reproduced within tolerance.",
                "evidence": [],
            }
        ]
    )


def test_the_owners_promotion_keeps_the_claim_holding_and_settles_a_challenge(env: Env) -> None:
    """R2-F1: a promotion bumps the revision but changes nothing an assessment relied on.
    R2-F3: it also settles an earlier challenge."""
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid, "challenges")
    assess(env, cid)
    assert status(env, cid)["status"] == "settling"
    promote(env, cid)
    got = status(env, cid)
    assert (got["status"], got["basis"]) == ("holds", "reviewed")
    assert "revised_since" not in {c["code"] for c in got["causes"]}


def test_relinking_to_the_revised_assumption_clears_the_stale_flag(env: Env) -> None:
    """R2-F2: the latest binding per dependency counts."""
    files = imported(env, ["result.csv"])
    assumption = new_id("entry")
    damper = {
        "title": "Ring damper",
        "text": "Placeholder damper 0.01.",
        "category": "assumption",
        "evidence": [],
        "claim_refs": [],
        "author_ref": None,
        "origin": "agent",
        "quantities": [],
        "review": "unreviewed",
    }
    env.apply([create("entry", damper, assumption)], caller=AGENT)
    cid = claim(env, [line_ev(files["result.csv"], 2)])

    def link(rev: int) -> None:
        env.apply(
            [
                {
                    "op": "relation_add",
                    "source_ref": {"id": cid, "rev": 1},
                    "target_ref": {"id": assumption, "rev": rev},
                    "relation": "depends_on",
                }
            ],
            caller=AGENT,
        )

    link(1)
    assess(env, cid)
    env.apply(
        [
            {
                "op": "revise",
                "id": assumption,
                "base_rev": 1,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": damper | {"text": "Physical damper 3e-4."},
            }
        ],
        caller=AGENT,
    )
    assert status(env, cid)["status"] == "stale"
    link(2)
    assert status(env, cid)["status"] == "holds"


def test_an_agent_cannot_revise_an_owners_assessment_into_its_own_basis(env: Env) -> None:
    """R2-F5: the basis reads the stored method only because writing researcher is protected on
    revise as well as on create."""
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    aid = new_id("assessment")
    body = {
        "claim_ref": {"id": cid, "rev": 1},
        "outcome": "supports",
        "rationale": "I checked.",
        "checks": [],
        "author_ref": None,
        "method": "researcher",
        "scope_match": True,
    }
    env.apply_protected([create("assessment", body, aid)])
    with pytest.raises(PrometheosError, match="owner's approval"):
        env.apply(
            [
                {
                    "op": "revise",
                    "id": aid,
                    "base_rev": 1,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": body | {"outcome": "challenges"},
                }
            ],
            caller=AGENT,
        )
    assert status(env, cid)["basis"] == "reviewed"


def test_a_claim_stored_before_cp10_is_read_the_same_way_when_compared(env: Env) -> None:
    """Review r3 F1: an old revision's body is normalised before its content is compared, so a
    claim stored without scope.objective (pre-CP10) is not read as revised."""
    import sqlite3

    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    from contextlib import closing

    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn, conn:
        body_id, encoded = conn.execute(
            "SELECT b.id, b.encoded FROM revisions v JOIN bodies b ON b.id = v.body_id "
            "WHERE v.record_id = ? AND v.rev = 1",
            (cid,),
        ).fetchone()
        stored = json.loads(encoded)
        stored["scope"].pop("objective", None)
        legacy = json.dumps(stored, separators=(",", ":"), sort_keys=True)
        conn.execute("UPDATE bodies SET encoded = ? WHERE id = ?", (legacy, body_id))
    got = status(env, cid)
    assert (got["status"], got["basis"]) == ("holds", "agent_reported")


def test_tampered_evidence_leaves_the_claim_unverified(env: Env) -> None:
    """Review r4 advisory: holds needs a cited version retained intact (computed_availability)."""
    import sqlite3
    from contextlib import closing

    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    assert status(env, cid)["status"] == "holds"
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn, conn:
        conn.execute(
            "UPDATE payloads SET data = ? WHERE sha256 = ?",
            (b"tampered", files["result.csv"]["sha256"]),
        )
    got = status(env, cid)
    assert got["status"] == "unverified"
    assert "evidence_missing" in {c["code"] for c in got["causes"]}


def test_the_http_read_filters_by_claim(env: Env) -> None:
    """Found in CP20.3: the hub route ignored claim_id and returned every claim."""
    from hub_support import running_hub

    files = imported(env, ["result.csv"])
    claim(env, [line_ev(files["result.csv"], 2)])
    wanted = claim(env, [line_ev(files["result.csv"], 3)])
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        reply = hub.request("GET", f"/api/v1/knowledge/claim-status?claim_id={wanted}")
        assert reply.status == 200, reply.body
        assert [c["claim_id"] for c in reply.json()["claims"]] == [wanted]
