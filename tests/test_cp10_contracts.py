"""CP10.1 contract additions (A02 migration item (d), decision 0015).

Claim ``scope.objective`` (nullable; earlier claims read null), entry category
``assumption`` with ``depends_on`` relations, superseding an assumption through an
authorised correction (protected ``superseded_by``), the observe-only ``execution`` kind
cited by entries and assessments, and the error codes ``approval_required`` and
``capability_unavailable`` with closed details.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry, entry_body
from pydantic import ValidationError

from prometheos.domain.ids import new_id, new_operational_id, new_request_id
from prometheos.domain.models import EXIT_CODES, ErrorInfo, PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def create(kind: str, body: dict[str, Any], record_id: str | None = None) -> dict[str, Any]:
    return {
        "op": "create",
        "kind": kind,
        "id": record_id or new_id(kind),
        "base_rev": 0,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": body,
    }


def claim_body(**scope_extra: Any) -> dict[str, Any]:
    scope = {
        k: "synthetic"
        for k in ("system", "operating_regime", "comparator", "model_fidelity", "validation_level")
    }
    scope.update(scope_extra)
    return {
        "statement": "Candidate recovers more energy.",
        "scope": scope,
        "evidence": [],
        "assessment_refs": [],
        "state": "proposed",
        "superseded_by": None,
    }


def relation(source: str, target: str, name: str, source_rev: int = 1) -> dict[str, Any]:
    return {
        "op": "relation_add",
        "source_ref": {"id": source, "rev": source_rev},
        "target_ref": {"id": target, "rev": 1},
        "relation": name,
    }


def body_of(env: Env, record_id: str) -> dict[str, Any]:
    return dict(env.svc.get(env.project_id, OWNER, record_id, None)["body"])


def error(fn: Any) -> PrometheosError:
    with pytest.raises(PrometheosError) as caught:
        fn()
    return caught.value


# ----- claim objective ----------------------------------------------------------------


def test_claim_objective_is_recorded_and_earlier_claims_read_null(env: Env) -> None:
    with_objective, without = new_id("claim"), new_id("claim")
    env.apply(
        [
            create(
                "claim", claim_body(objective="Replace the baseline controller?"), with_objective
            ),
            create("claim", claim_body(), without),  # an old-shaped client body
        ]
    )
    assert body_of(env, with_objective)["scope"]["objective"] == "Replace the baseline controller?"
    assert body_of(env, without)["scope"]["objective"] is None

    # A claim stored before schema 7 has no objective key at all: rewrite one stored body
    # the way CP2-CP9 code wrote it, then read, revise and transition it.
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        row = conn.execute(
            "SELECT b.id, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id "
            "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.id = ?",
            (without,),
        ).fetchone()
        legacy = json.loads(row[1])
        del legacy["scope"]["objective"]
        conn.execute("UPDATE bodies SET encoded = ? WHERE id = ?", (json.dumps(legacy), row[0]))
        conn.commit()
    finally:
        conn.close()
    # Round 2 (R13): the stored body keeps its old shape; readers see objective = null.
    assert body_of(env, without)["scope"]["objective"] is None
    listed = env.svc.list_records(env.project_id, AGENT, kind="claim")["items"]
    assert {i["id"]: i["body"]["scope"]["objective"] for i in listed}[without] is None
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        stored = conn.execute("SELECT encoded FROM bodies WHERE id = ?", (row[0],)).fetchone()
    finally:
        conn.close()
    assert "objective" not in json.loads(stored[0])["scope"]
    revised = claim_body(objective="Now recorded")
    env.apply(
        [
            {
                "op": "revise",
                "id": without,
                "base_rev": 1,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": revised,
            }
        ]
    )
    assert body_of(env, without)["scope"]["objective"] == "Now recorded"
    env.apply(
        [
            {
                "op": "transition",
                "id": with_objective,
                "base_rev": 1,
                "to": "inconclusive",
                "reason": None,
                "evidence": [],
            }
        ]
    )
    assert body_of(env, with_objective)["state"] == "inconclusive"
    context = env.svc.context(env.project_id, AGENT)["text"]
    assert "objective=Replace the baseline controller?" in context


# ----- assumptions --------------------------------------------------------------------


def assumption(env: Env, title: str = "Drag constant") -> str:
    record_id = new_id("entry")
    env.apply([create_entry(record_id, title=title, category="assumption")])
    return record_id


def test_assumption_entries_and_depends_on_pairs(env: Env) -> None:
    drag = assumption(env)
    note = new_id("entry")
    claim, action = new_id("claim"), new_id("action")
    env.apply([create_entry(note, title="Plain note"), create("claim", claim_body(), claim)])
    listed = env.svc.list_records(env.project_id, AGENT, kind="entry")["items"]
    assert {r["body"]["category"] for r in listed} == {"assumption", "note"}

    # Claims and entries rely on assumptions.
    env.apply([relation(claim, drag, "depends_on"), relation(note, drag, "depends_on")])
    links = env.svc.links(env.project_id, AGENT, drag)
    assert {r["source"] for r in links["relations"] if r["relation"] == "depends_on"} == {
        claim,
        note,
    }

    # Only assumption entries are depended on; actions still depend only on actions.
    refused = error(lambda: env.apply([relation(claim, note, "depends_on")]))
    assert refused.code == "validation_error" and "assumption" in refused.message
    env.apply(
        [
            create(
                "action",
                {
                    "title": "Check drag",
                    "reason": "test",
                    "source_refs": [],
                    "owner_ref": None,
                    "due_at": None,
                    "dependencies": [],
                    "status": "proposed",
                    "blocker": None,
                    "completion_evidence": [],
                    "commitment_item_id": None,
                },
                action,
            )
        ]
    )
    assert error(lambda: env.apply([relation(action, drag, "depends_on")])).code == (
        "validation_error"
    )
    assert error(lambda: env.apply([relation(claim, action, "depends_on")])).code == (
        "validation_error"
    )


def test_superseded_by_is_protected_on_create_and_revise(env: Env) -> None:
    drag, other = assumption(env), assumption(env, "Other")
    forged = create_entry(
        title="Forged", category="assumption", superseded_by={"id": other, "rev": 1}
    )
    assert error(lambda: env.apply([forged])).code == "policy_denied"
    revise = {
        "op": "revise",
        "id": drag,
        "base_rev": 1,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": entry_body(
            title="Drag constant",
            category="assumption",
            superseded_by={"id": other, "rev": 1},
        ),
    }
    assert error(lambda: env.apply([revise])).code == "policy_denied"
    assert body_of(env, drag).get("superseded_by") is None


def revise_entry(record_id: str, base_rev: int, **body: Any) -> dict[str, Any]:
    return {
        "op": "revise",
        "id": record_id,
        "base_rev": base_rev,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": entry_body(**body),
    }


def test_assumption_category_is_fixed_at_creation(env: Env) -> None:
    """Round 2 (R1): leaving the category would break depends_on targets and supersession
    replacements; entering it would make an ordinary entry a depends_on or supersede target
    after the fact. Both are refused; other edits of an active assumption are allowed."""
    drag, note = assumption(env), new_id("entry")
    claim = new_id("claim")
    env.apply(
        [
            create_entry(note, title="Plain note"),
            create("claim", claim_body(), claim),
        ]
    )
    env.apply([relation(claim, drag, "depends_on")])
    out = error(lambda: env.apply([revise_entry(drag, 1, title="Drag", category="note")]))
    assert out.code == "policy_denied" and "fixed at creation" in out.message
    into = error(lambda: env.apply([revise_entry(note, 1, title="Note", category="assumption")]))
    assert into.code == "policy_denied"
    assert body_of(env, drag)["category"] == "assumption"
    assert body_of(env, note)["category"] == "note"
    env.apply(
        [revise_entry(drag, 1, title="Drag constant", text="Clarified", category="assumption")]
    )
    assert body_of(env, drag)["text"] == "Clarified"


def test_replacement_of_a_superseded_assumption_stays_an_assumption(env: Env) -> None:
    drag, replacement = assumption(env), assumption(env, "Speed-dependent drag")
    correction = supersede_correction(drag, 1, replacement)
    env.apply([correction])
    env.apply_protected([{"op": "correction_apply", "id": correction["id"], "base_rev": 1}])
    out = error(
        lambda: env.apply([revise_entry(replacement, 1, title="Now a finding", category="finding")])
    )
    assert out.code == "policy_denied"


def test_replace_text_cannot_alter_a_superseded_assumption(env: Env) -> None:
    drag, replacement = assumption(env), assumption(env, "Speed-dependent drag")
    first = supersede_correction(drag, 1, replacement)
    env.apply([first])
    env.apply_protected([{"op": "correction_apply", "id": first["id"], "base_rev": 1}])
    target = {"id": drag, "rev": 2}
    rewrite = create(
        "correction",
        {
            "target_refs": [target],
            "replacement_refs": [],
            "reason": "Reword the old assumption.",
            "evidence": [],
            "effects": [
                {
                    "target_ref": target,
                    "effect": "replace_text",
                    "replacement_ref": None,
                    "replacement_text": "Rewritten history",
                }
            ],
            "status": "proposed",
            "applied_seq": None,
        },
    )
    out = error(lambda: env.apply([rewrite]))
    assert out.code == "validation_error" and "superseded assumption" in out.message
    # An active assumption's text may still be corrected (a protected correction).
    active = assumption(env, "Active")
    target = {"id": active, "rev": 1}
    fix = create(
        "correction",
        {
            "target_refs": [target],
            "replacement_refs": [],
            "reason": "Typo.",
            "evidence": [],
            "effects": [
                {
                    "target_ref": target,
                    "effect": "replace_text",
                    "replacement_ref": None,
                    "replacement_text": "Corrected text",
                }
            ],
            "status": "proposed",
            "applied_seq": None,
        },
    )
    env.apply([fix])
    env.apply_protected([{"op": "correction_apply", "id": fix["id"], "base_rev": 1}])
    assert body_of(env, active)["text"] == "Corrected text"


def test_legacy_entries_read_superseded_by_null(env: Env) -> None:
    drag = assumption(env)
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        row = conn.execute(
            "SELECT b.id, b.encoded FROM records r JOIN revisions v ON v.record_id = r.id "
            "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.id = ?",
            (drag,),
        ).fetchone()
        legacy = json.loads(row[1])
        del legacy["superseded_by"]
        conn.execute("UPDATE bodies SET encoded = ? WHERE id = ?", (json.dumps(legacy), row[0]))
        conn.commit()
    finally:
        conn.close()
    assert body_of(env, drag)["superseded_by"] is None


def supersede_correction(target: str, target_rev: int, replacement: str) -> dict[str, Any]:
    t, r = {"id": target, "rev": target_rev}, {"id": replacement, "rev": 1}
    return create(
        "correction",
        {
            "target_refs": [t],
            "replacement_refs": [r],
            "reason": "Drag varies with speed in the high regime.",
            "evidence": [],
            "effects": [
                {
                    "target_ref": t,
                    "effect": "supersede",
                    "replacement_ref": r,
                    "replacement_text": None,
                }
            ],
            "status": "proposed",
            "applied_seq": None,
        },
    )


def test_superseding_an_assumption_needs_authority_and_flags_dependents(env: Env) -> None:
    drag, replacement = assumption(env), assumption(env, "Speed-dependent drag")
    claim = new_id("claim")
    env.apply([create("claim", claim_body(), claim), relation(claim, drag, "depends_on")])
    correction = supersede_correction(drag, 1, replacement)
    env.apply([correction])
    apply_op = [{"op": "correction_apply", "id": correction["id"], "base_rev": 1}]

    # An agent without an owner decision cannot apply it: since CP10.2 the refusal is a typed
    # approval_required (the authority is the same as claim supersession) and nothing commits.
    denied = error(lambda: env.apply(apply_op))
    assert denied.code == "approval_required"
    assert body_of(env, drag).get("superseded_by") is None

    seq_before = env.seq()
    result = env.apply_protected(apply_op)
    assert result["project_seq"] == seq_before + 1
    assert body_of(env, drag)["superseded_by"] == {"id": replacement, "rev": 1}
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        flags = conn.execute(
            "SELECT record_id, created_seq FROM view_flags WHERE cause_id = ?",
            (correction["id"],),
        ).fetchall()
    finally:
        conn.close()
    assert (claim, result["project_seq"]) in flags  # same commit as the supersession

    # The superseded assumption is now immutable, and cannot be superseded twice.
    revise = {
        "op": "revise",
        "id": drag,
        "base_rev": 2,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": entry_body(
            title="Edited", category="assumption", superseded_by={"id": replacement, "rev": 1}
        ),
    }
    assert error(lambda: env.apply([revise])).code == "policy_denied"
    third = assumption(env, "Third")
    assert error(lambda: env.apply([supersede_correction(drag, 2, third)])).code == (
        "validation_error"
    )


def test_only_assumptions_are_superseded_and_only_by_assumptions(env: Env) -> None:
    drag, note = assumption(env), new_id("entry")
    env.apply([create_entry(note, title="Plain note")])
    claim = new_id("claim")
    env.apply([create("claim", claim_body(), claim)])
    assert error(lambda: env.apply([supersede_correction(note, 1, drag)])).code == (
        "validation_error"
    )
    assert error(lambda: env.apply([supersede_correction(drag, 1, note)])).code == (
        "validation_error"
    )
    # Mixed kinds fail the closed model before the store is consulted.
    mixed = supersede_correction(drag, 1, claim)
    assert error(lambda: env.apply([mixed])).code == "validation_error"


# ----- executions ---------------------------------------------------------------------


def execution_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Plant sweep",
        "host_label": "lab workstation",
        "reporter": "test agent",
        "command_summary": "matlab -batch run_sweep",
        "working_root_alias": "research",
        "external_ref": "sweep-a",
        "expected_outputs": ["results/sweep.csv"],
        "state": "reported_running",
        "last_reported_at": "2026-10-06T09:00:00Z",
        "receipt_refs": [],
    }
    body.update(extra)
    return body


def receipt_version(env: Env) -> str:
    imported = env.import_files(["plant-result.md"])["plan"]["items"][0]
    receipt = {
        "schema_version": 1,
        "observation": "reported",
        "reporter": "test agent",
        "command": "run_sweep",
        "working_root": "research",
        "environment": {"os": "TestOS", "tools": []},
        "started_at": None,
        "ended_at": None,
        "exit_status": 0,
        "outcome": "succeeded",
        "inputs": [],
        "outputs": [
            {
                "source_id": imported["source_id"],
                "version_id": imported["version_id"],
                "sha256": imported["sha256"],
                "role": "result",
            }
        ],
        "limitations": [],
    }
    commit = env.svc.receipt_record(
        env.project_id,
        raw=json.dumps(receipt).encode("utf-8"),
        title="sweep receipt",
        description="",
        request_id=new_request_id(),
    )
    return next(r["id"] for r in commit["results"] if r["id"].startswith("version_"))


def test_execution_records_are_observe_only_and_cite_receipts(env: Env) -> None:
    execution = new_id("execution")
    env.apply([create("execution", execution_body(), execution)])
    assert body_of(env, execution)["state"] == "reported_running"

    version = receipt_version(env)
    completed = execution_body(
        state="reported_completed",
        last_reported_at="2026-10-06T11:00:00Z",
        receipt_refs=[{"id": version, "rev": 1}],
    )
    env.apply(
        [
            {
                "op": "revise",
                "id": execution,
                "base_rev": 1,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": completed,
            }
        ]
    )
    assert body_of(env, execution)["receipt_refs"] == [{"id": version, "rev": 1}]

    # A plain (non-receipt) version is not a receipt.
    plain = env.import_files(["baseline.md"])["plan"]["items"][0]["version_id"]
    bad = execution_body(receipt_refs=[{"id": plain, "rev": 1}])
    assert error(lambda: env.apply([create("execution", bad)])).code == "validation_error"

    # Entries and assessments cite an execution (the awaiting-interpretation queue is
    # derived from these citations in CP10.4); claims do not.
    note, claim = new_id("entry"), new_id("claim")
    env.apply(
        [
            create_entry(note, title="Interpretation", review="reviewed"),
            create("claim", claim_body(), claim),
        ]
    )
    # A reviewed entry citing an execution takes it out of the awaiting-interpretation queue, so
    # since CP10.4 (decision 0018) the citation is a protected effect: the owner's grant.
    assert error(lambda: env.apply([relation(note, execution, "cites")])).code == (
        "approval_required"
    )
    env.apply_protected([relation(note, execution, "cites")])
    assert error(lambda: env.apply([relation(claim, execution, "cites")])).code == (
        "validation_error"
    )
    hits = env.svc.search(env.project_id, AGENT, "sweep")["hits"]
    assert execution in {h["id"] for h in hits}


def test_execution_states_are_reported_never_inferred(env: Env) -> None:
    for body in (
        execution_body(state="failed"),
        execution_body(last_reported_at=None),
        execution_body(external_ref="https://u:p@cluster.example/job"),
    ):
        assert error(lambda b=body: env.apply([create("execution", b)])).code == (
            "validation_error"
        )
    env.apply([create("execution", execution_body(state="status_unknown", last_reported_at=None))])


# ----- error codes --------------------------------------------------------------------


def test_new_error_codes_have_exit_codes_and_closed_details() -> None:
    assert EXIT_CODES["approval_required"] == 4
    assert EXIT_CODES["capability_unavailable"] == 5
    approval = new_operational_id("apreq")
    details = {
        "approval_request_id": approval,
        "plan_sha256": "b" * 64,
        "required_scope": "exact_request",
        "owner_route": {"hub": f"/approvals/{approval}", "cli": "prometheos decision review"},
        "expires_at": "2026-10-06T12:00:00Z",
    }
    response = PrometheosError("approval_required", "needs the owner", details).to_response(3)
    assert response.error.retryable is False and response.error.details == details
    blocked = PrometheosError(
        "capability_unavailable", "not installed", {"capability": "latex", "blocker": "offline"}
    )
    assert blocked.exit_code == 5
    blocked.to_response()
    # A secret or an untyped detail never fits the closed details: such an error is a
    # server defect and degrades to a plain validation_error naming the intended code,
    # never a raw pydantic exception and never the malformed details (round 2, R12).
    for code, bad in (
        ("approval_required", {**details, "capability_secret": "pcap_x"}),
        ("approval_required", {}),
        ("capability_unavailable", {"capability": "latex", "blocker": "x", "key": "sk"}),
    ):
        degraded = PrometheosError(code, "m", bad).to_response()
        assert degraded.error.code == "validation_error"
        assert degraded.error.details == {"intended_code": code}
    with pytest.raises(ValidationError):  # the closed models themselves stay strict
        ErrorInfo(code="approval_required", message="m", retryable=False, details={})


def test_a02_errors_raised_inside_an_apply_operation_keep_code_and_details(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3 (N1): the apply loop adds op_index to ordinary errors only; the A02 codes
    keep their closed details and are never degraded to validation_error."""
    from prometheos.app import apply as apply_mod

    approval = new_operational_id("apreq")
    raised = {
        "approval_required": {
            "approval_request_id": approval,
            "plan_sha256": "c" * 64,
            "required_scope": "exact_request",
            "owner_route": {"hub": f"/approvals/{approval}", "cli": "prometheos decision review"},
            "expires_at": "2026-10-06T12:00:00Z",
        },
        "capability_unavailable": {"capability": "latex", "blocker": "not_installed"},
    }
    for code, details in raised.items():

        def refuse(*args: Any, c: str = code, d: dict[str, Any] = details) -> Any:
            raise PrometheosError(c, "raised inside the operation", dict(d))

        monkeypatch.setattr(apply_mod, "op_create", refuse)
        caught = error(lambda: env.apply([create_entry(title="x")]))
        assert caught.code == code and caught.details == details  # no op_index
        response = caught.to_response(env.seq()).model_dump(mode="json")
        assert response["error"]["code"] == code
        assert response["error"]["details"] == details
    monkeypatch.undo()
    # Ordinary errors still carry op_index.
    bad = create_entry(title="x")
    bad["body"]["category"] = "unknown"
    assert "op_index" in error(lambda: env.apply([bad])).details
