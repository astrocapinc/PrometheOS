"""CP2.4: the KERS-inspired synthetic journey (S05-S09) against real stores.

Synthetic fixtures only; nothing here validates a controller or a paper claim.
The reviewer should read the asserted *content* (statements, scopes, rendered
context), not just counts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, FULL, OWNER, Env, create_entry, request

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError

SCOPE_SIMPLIFIED = {
    "system": "synthetic braking event SIMPLIFIED-A",
    "operating_regime": "one constant-deceleration event",
    "comparator": "synthetic reference controller, same event",
    "model_fidelity": "algebraic energy balance; no actuator dynamics",
    "validation_level": "none (invented test numbers)",
}
SCOPE_PLANT = {
    "system": "synthetic plant event PLANT-B",
    "operating_regime": "variable-demand event",
    "comparator": "reference controller under the same constraints",
    "model_fidelity": "dynamic plant with actuator constraints",
    "validation_level": "none (invented test numbers)",
}
SCOPE_OVERBROAD = {
    "system": "candidate controller",
    "operating_regime": "across operating conditions",
    "comparator": "reference controller",
    "model_fidelity": "unspecified",
    "validation_level": "unspecified",
}


class Journey:
    def __init__(self, env: Env) -> None:
        self.env = env
        self.svc = env.svc
        self.pid = env.project_id

    def grant_for(
        self, ops: list[dict[str, Any]], expected: int | None = None
    ) -> tuple[bytes, str]:
        seq = self.env.seq() if expected is None else expected
        raw = request(self.pid, ops, seq)
        preview = self.svc.review_request(self.pid, raw)
        grant = self.svc.grant(
            self.pid,
            operation="exact_request",
            digest=preview["digest"],
            label="SYNTHETIC-TEST owner decision",
            synthetic=True,
        )
        return raw, grant.secret

    def apply_protected(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        raw, secret = self.grant_for(ops)
        return self.svc.apply(self.pid, raw, capability=secret)

    def get(self, rid: str) -> dict[str, Any]:
        return self.svc.get(self.pid, OWNER, rid, None)


def text_evidence(item: dict[str, Any], line: int, quote: str | None = None) -> dict[str, Any]:
    return {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": line, "end_line": line},
        "quote_sha256": hashlib.sha256(quote.encode()).hexdigest() if quote else None,
    }


def line_of(path: Path, needle: str) -> tuple[int, str]:
    for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if needle in line:
            return number, line
    raise AssertionError(needle)


def claim_op(
    cid: str, statement: str, scope: dict[str, str], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "op": "create",
        "kind": "claim",
        "id": cid,
        "base_rev": 0,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": {
            "statement": statement,
            "scope": scope,
            "evidence": evidence,
            "assessment_refs": [],
            "state": "proposed",
            "superseded_by": None,
        },
    }


@pytest.fixture
def journey(tmp_path: Path) -> Journey:
    return Journey(Env(tmp_path))


def test_kers_journey(journey: Journey) -> None:
    env, svc, pid = journey.env, journey.svc, journey.pid
    imported = env.import_files(["baseline.md", "plant-result.md"], retention="copy")
    base, plant = imported["plan"]["items"]
    base_line, base_text = line_of(
        env.research / "baseline.md", "Recovered energy with the candidate"
    )
    plant_line, plant_text = line_of(
        env.research / "plant-result.md", "Recovered energy with the candidate"
    )

    # --- C02-NUMERIC: exact bound quantities -------------------------------------------
    start = base_text.index("2.40 kJ")
    ev = text_evidence(base, base_line, base_text)
    good_q = {
        "decimal": "2.40",
        "unit": "kJ",
        "source_ref": ev,
        "source_pointer": f"text:{base_line}:{start}:{start + 7}",
    }
    finding = new_id("entry")
    env.apply(
        [
            create_entry(
                finding,
                title="Simplified comparison",
                category="finding",
                text="Candidate 2.40 kJ in SIMPLIFIED-A.",
                evidence=[ev],
                quantities=[good_q],
            )
        ]
    )
    for decimal in ("2.41", "2.4", "2.400"):
        with pytest.raises(PrometheosError) as err:
            env.apply([create_entry(quantities=[dict(good_q, decimal=decimal)], evidence=[ev])])
        assert err.value.code == "validation_error", decimal
    stale = dict(good_q, source_ref=text_evidence(plant, base_line))
    with pytest.raises(PrometheosError):
        env.apply([create_entry(quantities=[stale])])
    small = base_text.index("2.00 kJ") if "2.00 kJ" in base_text else None
    assert small is None  # reference value sits on its own line in the fixture

    # --- C02-CORRECTION: two scoped findings, protected support and correction ----------
    claim_a, claim_b, claim_o, claim_n = (new_id("claim") for _ in range(4))
    env.apply(
        [
            claim_op(
                claim_a,
                "The candidate recovered more energy than the reference (2.40 kJ vs 2.00 kJ).",
                SCOPE_SIMPLIFIED,
                [ev],
            ),
            claim_op(
                claim_b,
                "The candidate recovered less energy than the reference (1.80 kJ vs 1.90 kJ).",
                SCOPE_PLANT,
                [text_evidence(plant, plant_line, plant_text)],
            ),
            claim_op(
                claim_o,
                "The candidate improves energy recovery across operating conditions.",
                SCOPE_OVERBROAD,
                [ev],
            ),
        ]
    )
    support = new_id("assessment")
    env.apply(
        [
            {
                "op": "create",
                "kind": "assessment",
                "id": support,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {
                    "claim_ref": {"id": claim_a, "rev": 1},
                    "outcome": "supports",
                    "rationale": "Bound quantities match within the stated synthetic regime.",
                    "checks": [{"name": "quantity binding", "result": "pass", "evidence": [ev]}],
                    "author_ref": None,
                    "method": "reported_agent",
                    "scope_match": True,
                },
            }
        ]
    )
    promote = [
        {
            "op": "transition",
            "id": claim_a,
            "base_rev": 1,
            "to": "supported_within_conditions",
            "reason": "Owner reviewed the binding.",
            "evidence": [],
        }
    ]
    with pytest.raises(PrometheosError) as err:
        env.apply(promote)
    assert err.value.code == "approval_required"  # agent cannot self-authorise
    # A capability for a different proposal does not transfer.
    _, other_secret = journey.grant_for([dict(promote[0], reason="different text")])
    with pytest.raises(PrometheosError) as err:
        svc.apply(pid, request(pid, promote, env.seq()), capability=other_secret)
    assert err.value.code == "policy_denied"
    journey.apply_protected(promote)
    assert journey.get(claim_a)["body"]["state"] == "supported_within_conditions"

    challenge = new_id("assessment")
    env.apply(
        [
            {
                "op": "create",
                "kind": "assessment",
                "id": challenge,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {
                    "claim_ref": {"id": claim_o, "rev": 1},
                    "outcome": "challenges",
                    "rationale": "PLANT-B shows lower recovery under actuator constraints.",
                    "checks": [],
                    "author_ref": None,
                    "method": "reported_agent",
                    "scope_match": False,
                },
            }
        ]
    )
    conclusion = new_id("entry")
    env.apply(
        [
            create_entry(
                conclusion,
                title="Draft conclusion",
                category="decision",
                text="We will argue the candidate is better overall.",
                claim_refs=[{"id": claim_o, "rev": 1}],
            )
        ]
    )
    env.apply(
        [
            claim_op(
                claim_n,
                "In the simplified constant-deceleration event only, the candidate "
                "recovered more energy than the reference.",
                SCOPE_SIMPLIFIED,
                [ev],
            )
        ]
    )
    before = svc.context(pid, AGENT)["text"]
    assert "HISTORICAL" not in before and "UNRESOLVED" in before

    correction = new_id("correction")
    env.apply(
        [
            {
                "op": "create",
                "kind": "correction",
                "id": correction,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {
                    "target_refs": [{"id": claim_o, "rev": 1}],
                    "replacement_refs": [{"id": claim_n, "rev": 1}],
                    "reason": "Overbroad inference: PLANT-B contradicts it outside SIMPLIFIED-A.",
                    "evidence": [text_evidence(plant, plant_line)],
                    "effects": [
                        {
                            "target_ref": {"id": claim_o, "rev": 1},
                            "effect": "supersede",
                            "replacement_ref": {"id": claim_n, "rev": 1},
                            "replacement_text": None,
                        }
                    ],
                    "status": "proposed",
                    "applied_seq": None,
                },
            }
        ]
    )
    # A proposal alone changes no effective truth.
    assert journey.get(claim_o)["body"]["state"] == "proposed"
    assert "APPLIED CORRECTION" not in svc.context(pid, AGENT)["text"]
    apply_op = [{"op": "correction_apply", "id": correction, "base_rev": 1}]
    with pytest.raises(PrometheosError) as err:
        env.apply(apply_op)
    assert err.value.code == "approval_required"
    applied = journey.apply_protected(apply_op)
    o_now = journey.get(claim_o)["body"]
    assert o_now["state"] == "superseded" and o_now["superseded_by"]["id"] == claim_n
    corr = journey.get(correction)["body"]
    assert corr["status"] == "applied" and corr["applied_seq"] == applied["project_seq"]
    ctx = svc.context(pid, AGENT)
    assert "APPLIED CORRECTION" in ctx["text"]
    assert "HISTORICAL (superseded" in ctx["text"]
    assert "SIMPLIFIED-A" in ctx["text"] and "PLANT-B" in ctx["text"]  # both scopes kept distinct
    flagged = next(s for s in ctx["manifest"]["sections"] if s["name"] == "needs_review")
    assert conclusion in flagged["included"]  # dependent marked, not rewritten
    assert (
        journey.get(conclusion)["body"]["text"] == "We will argue the candidate is better overall."
    )
    # History stays inspectable as known before the correction.
    assert svc.get(pid, OWNER, claim_o, applied["project_seq"] - 1)["body"]["state"] == "proposed"

    # --- C02-MEETING: late-entered notes, proposals and one adopted action ---------------
    seq_before_meeting = env.seq()
    notes = (env.research / "meeting.txt").read_text("utf-8")
    captured = svc.capture_meeting(
        pid, title="Supervision 2026-09-28", text=notes, held_at="2026-09-28T10:00:00Z"
    )
    mtg, mver, msrc = captured["meeting_id"], captured["version_id"], captured["source_id"]
    assert "Supervision" not in svc.context(pid, AGENT, at_seq=seq_before_meeting)["text"]
    mitem = {"source_id": msrc, "version_id": mver}

    def item_op(iid: str, typ: str, line: int) -> dict[str, Any]:
        text = notes.splitlines()[line - 1]
        return {
            "op": "create",
            "kind": "item",
            "id": iid,
            "base_rev": 0,
            "sensitivity": "normal",
            "occurred_at": "2026-09-28T10:00:00Z",
            "body": {
                "meeting_id": mtg,
                "type": typ,
                "text": text,
                "evidence": [
                    dict(
                        mitem,
                        locator={"type": "text", "start_line": line, "end_line": line},
                        quote_sha256=None,
                    )
                ],
                "proposed_owner": None,
                "due_at": None,
                "status": "proposed",
                "action_id": None,
            },
        }

    suggestion, commitment, question = new_id("item"), new_id("item"), new_id("item")
    ops = [
        item_op(suggestion, "suggestion", 2),
        item_op(commitment, "commitment", 3),
        item_op(question, "question", 4),
    ]
    env.apply(ops)
    first = svc.reconcile(pid, AGENT, mtg)
    with pytest.raises(PrometheosError) as err:  # exact re-import of the same locators
        env.apply([dict(o, id=new_id("item")) for o in ops])
    assert err.value.code == "revision_conflict"
    assert svc.reconcile(pid, AGENT, mtg)["items"] == first["items"]
    action = new_id("action")
    adopt = [
        {
            "op": "create",
            "kind": "action",
            "id": action,
            "base_rev": 0,
            "sensitivity": "normal",
            "occurred_at": None,
            "body": {
                "title": "Attach the constrained-plant result to the notebook",
                "reason": "Researcher committed to it at the 2026-09-28 supervision.",
                "source_refs": [{"id": commitment, "rev": 1}],
                "owner_ref": None,
                "due_at": None,
                "dependencies": [],
                "status": "proposed",
                "blocker": None,
                "completion_evidence": [],
                "commitment_item_id": commitment,
            },
        },
        {
            "op": "transition",
            "id": action,
            "base_rev": 1,
            "to": "ready",
            "reason": "Adopted from meeting commitment",
            "evidence": [],
        },
        {
            "op": "transition",
            "id": commitment,
            "base_rev": 1,
            "to": "adopted",
            "reason": "Owner adopted the commitment",
            "evidence": [],
        },
    ]
    with pytest.raises(PrometheosError) as err:
        env.apply(adopt)
    assert err.value.code == "approval_required"
    journey.apply_protected(adopt)
    actions = svc.list_records(pid, OWNER, kind="action")["items"]
    assert [a["id"] for a in actions] == [action]
    assert journey.get(commitment)["body"]["action_id"] == action
    assert journey.get(suggestion)["body"]["status"] == "proposed"
    q = journey.get(question)["body"]
    assert q["proposed_owner"] is None and q["due_at"] is None
    with pytest.raises(PrometheosError):
        env.apply(
            [
                {
                    "op": "create",
                    "kind": "action",
                    "id": new_id("action"),
                    "base_rev": 0,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": dict(adopt[0]["body"]),
                }
            ]
        )  # second action for the same item
    recon = svc.reconcile(pid, AGENT, mtg)["items"]
    assert {r["item_id"]: r["owner"] for r in recon}[question] == "unknown"

    # --- C02-CONTEXT: limited budget keeps corrections and coverage ----------------------
    info = svc.project_info(pid, OWNER)
    settings = info["settings"]
    settings["allowed_roots"].append({"alias": "archive", "path": str(env.tmp / "unplugged-drive")})
    env.apply_protected(
        [{"op": "project_update", "base_rev": info["settings_rev"], "settings": settings}]
    )
    env.apply([create_entry(title=f"Background note {i}", text="x" * 300) for i in range(60)])
    small = svc.context(pid, AGENT, budget=2000)
    text, manifest = small["text"], small["manifest"]
    assert len(text) <= 2000
    assert "APPLIED CORRECTION" in text
    assert "research root 'archive' is unavailable" in text
    assert manifest["incomplete"] is False
    background = next(s for s in manifest["sections"] if s["name"] == "background")
    assert background["excluded_count"] > 0
    assert "not a whole-project view" in text
    assert manifest["coverage"]["unavailable_roots"] == ["archive"]

    # --- C02-PRIVACY: restricted canary never leaves agent scope; purge removes it -------
    canary = "CANARY-7Q4Z-PRIVATE"
    secret_file = env.research / "interview.txt"
    secret_file.write_text(f"Participant said {canary} during the interview.\n", "utf-8")
    restricted = env.import_files(["interview.txt"], sensitivity="restricted")["plan"]["items"][0]
    derived = new_id("entry")
    env.apply(
        [
            create_entry(
                derived,
                sensitivity="restricted",
                title="Interview summary",
                text=f"Summary quoting {canary}.",
                evidence=[text_evidence(restricted, 1)],
            )
        ],
        caller=OWNER,  # agents cannot cite evidence they cannot see
    )
    for view in (
        svc.context(pid, AGENT),
        svc.search(pid, AGENT, canary)["hits"],
        svc.notebook(pid, AGENT),
        svc.list_records(pid, AGENT, limit=200),
        svc.next_steps(pid, AGENT),
    ):
        assert canary not in json.dumps(view)
    with pytest.raises(PrometheosError):
        svc.get(pid, AGENT, derived, None)
    assert canary in json.dumps(svc.search(pid, OWNER, canary))
    backup = svc.backup(pid, env.tmp / "before-purge.zip", FULL)
    plan = svc.purge_plan(pid, [restricted["source_id"]], OWNER)
    assert derived in plan["closure"] and restricted["version_id"] in plan["closure"]
    with pytest.raises(PrometheosError):
        svc.purge_commit(pid, plan["plan_id"], None)
    grant = svc.grant(
        pid,
        operation="purge",
        digest=plan["plan_sha256"],
        label="SYNTHETIC-TEST purge",
        synthetic=True,
    )
    receipt = svc.purge_commit(pid, plan["plan_id"], grant.secret)["purge_receipt"]
    assert receipt["records_purged"] == len(plan["closure"])
    assert any("Backups" in lim for lim in receipt["limitations"])
    assert canary.encode() not in (env.store_dir / "record.sqlite").read_bytes()
    for view in (
        svc.search(pid, OWNER, canary)["hits"],
        svc.notebook(pid, OWNER),
        svc.list_records(pid, OWNER, limit=200, include_archived=True),
        svc.get(pid, OWNER, derived, None),
    ):
        assert canary not in json.dumps(view)
    for path in env.store_dir.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert canary.encode() not in data and b"7q4z" not in data.lower(), path
    assert backup["backup"]  # the earlier backup still exists; the receipt says so
