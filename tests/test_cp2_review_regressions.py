"""Regressions for the CP2 r1 independent review (reviewer-subagent-cp2-a)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry

from prometheos.app.reads import HIDDEN
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def ev(item: dict[str, Any], line: int = 1) -> dict[str, Any]:
    return {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": line, "end_line": line},
        "quote_sha256": None,
    }


# Blocking 2: raising a source's sensitivity propagates to versions and citing records.
def test_raising_source_sensitivity_propagates(env: Env) -> None:
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    note = new_id("entry")
    env.apply(
        [
            create_entry(
                note,
                title="Quotes the baseline",
                text="SIMPLIFIED-A says 2.40 kJ",
                evidence=[ev(item)],
            )
        ]
    )
    source = env.svc.get(env.project_id, OWNER, item["source_id"], None)
    env.apply_protected(
        [
            {
                "op": "revise",
                "id": item["source_id"],
                "base_rev": source["rev"],
                "sensitivity": "restricted",
                "occurred_at": None,
                "body": source["body"],
            }
        ],
        caller=OWNER,
    )
    for rid in (item["version_id"], note):
        assert env.svc.get(env.project_id, OWNER, rid, None)["sensitivity"] == "restricted"
        with pytest.raises(PrometheosError):
            env.svc.get(env.project_id, AGENT, rid, None)
    assert env.svc.search(env.project_id, AGENT, "SIMPLIFIED")["hits"] == []


# Major: transition reasons, waivers and owner-approved notes are stored.
def test_transition_reasons_are_recorded(env: Env) -> None:
    action = new_id("action")
    env.apply(
        [
            {
                "op": "create",
                "kind": "action",
                "id": action,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {
                    "title": "Run comparison",
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
            }
        ]
    )
    env.apply_protected(
        [
            {
                "op": "transition",
                "id": action,
                "base_rev": 1,
                "to": "ready",
                "reason": "Adopted at supervision",
                "evidence": [],
            }
        ]
    )
    env.apply(
        [
            {
                "op": "transition",
                "id": action,
                "base_rev": 2,
                "to": "in_progress",
                "reason": None,
                "evidence": [],
            }
        ]
    )
    env.apply_protected(
        [
            {
                "op": "transition",
                "id": action,
                "base_rev": 3,
                "to": "done",
                "reason": "Owner confirms it was done offline",
                "evidence": [],
            }
        ]
    )
    history = env.svc.history(env.project_id, AGENT, action)
    notes = [(n["from"], n["to"], n["reason"]) for n in history["transitions"]]
    assert notes == [
        ("proposed", "ready", "Adopted at supervision"),
        ("ready", "in_progress", None),
        ("in_progress", "done", "Owner confirms it was done offline"),
    ]
    assert history["transitions"][2]["protected_effects"] == ["completion without evidence"]
    assert [r["rev"] for r in history["revisions"]] == [1, 2, 3, 4]


# Major: agents cannot change research roots/settings or archive records.
def test_settings_and_archive_need_owner_decisions(env: Env) -> None:
    info = env.svc.project_info(env.project_id, OWNER)
    settings = dict(info["settings"])
    settings["allowed_roots"] = [
        *settings["allowed_roots"],
        {"alias": "elsewhere", "path": str(env.tmp)},
    ]
    with pytest.raises(PrometheosError) as err:
        env.apply(
            [{"op": "project_update", "base_rev": info["settings_rev"], "settings": settings}]
        )
    assert err.value.code == "approval_required"
    note = new_id("entry")
    env.apply([create_entry(note)])
    with pytest.raises(PrometheosError) as err:
        env.apply([{"op": "archive", "id": note, "base_rev": 1}])
    assert err.value.code == "approval_required"
    env.apply_protected([{"op": "archive", "id": note, "base_rev": 1}])


# Major: as-of views before a purge do not crash and mark purged content.
def test_as_of_views_after_purge(env: Env) -> None:
    item = env.import_files(["meeting.txt"], sensitivity="restricted")["plan"]["items"][0]
    early = env.seq()
    plan = env.svc.purge_plan(env.project_id, [item["source_id"]], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    for caller in (AGENT, OWNER):
        env.svc.context(env.project_id, caller, at_seq=early)
        env.svc.notebook(env.project_id, caller, at_seq=early)
        env.svc.next_steps(env.project_id, caller, at_seq=early)
    historical = env.svc.get(env.project_id, OWNER, item["version_id"], early)
    assert historical["body"] == {"purged": True}


# Major: purge planning is owner-only.
def test_purge_plan_requires_owner(env: Env) -> None:
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    with pytest.raises(PrometheosError) as err:
        env.svc.purge_plan(env.project_id, [item["source_id"]], AGENT)
    assert err.value.code == "policy_denied"


# Major: hidden record IDs are redacted from every agent-scope output.
def test_hidden_ids_never_appear_in_agent_outputs(env: Env) -> None:
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    visible_claim, hidden_claim = new_id("claim"), new_id("claim")
    scope = {
        k: "s"
        for k in ("system", "operating_regime", "comparator", "model_fidelity", "validation_level")
    }

    def claim(cid: str, sensitivity: str) -> dict[str, Any]:
        return {
            "op": "create",
            "kind": "claim",
            "id": cid,
            "base_rev": 0,
            "sensitivity": sensitivity,
            "occurred_at": None,
            "body": {
                "statement": f"Claim {sensitivity}",
                "scope": scope,
                "evidence": [ev(item)],
                "assessment_refs": [],
                "state": "proposed",
                "superseded_by": None,
            },
        }

    env.apply([claim(visible_claim, "normal"), claim(hidden_claim, "restricted")], caller=OWNER)
    correction = new_id("correction")
    env.apply(
        [
            {
                "op": "create",
                "kind": "correction",
                "id": correction,
                "base_rev": 0,
                "sensitivity": "restricted",
                "occurred_at": None,
                "body": {
                    "target_refs": [{"id": visible_claim, "rev": 1}],
                    "replacement_refs": [{"id": hidden_claim, "rev": 1}],
                    "reason": "Narrower restricted replacement",
                    "evidence": [],
                    "effects": [
                        {
                            "target_ref": {"id": visible_claim, "rev": 1},
                            "effect": "supersede",
                            "replacement_ref": {"id": hidden_claim, "rev": 1},
                            "replacement_text": None,
                        }
                    ],
                    "status": "proposed",
                    "applied_seq": None,
                },
            }
        ],
        caller=OWNER,
    )
    env.apply_protected([{"op": "correction_apply", "id": correction, "base_rev": 1}], caller=OWNER)
    outputs = json.dumps(
        [
            env.svc.context(env.project_id, AGENT),
            env.svc.next_steps(env.project_id, AGENT),
            env.svc.get(env.project_id, AGENT, visible_claim, None),
            env.svc.list_records(env.project_id, AGENT, kind="claim"),
            env.svc.history(env.project_id, AGENT, visible_claim),
        ]
    )
    assert hidden_claim not in outputs and correction not in outputs
    assert HIDDEN in outputs
    owner_view = env.svc.get(env.project_id, OWNER, visible_claim, None)
    assert owner_view["body"]["superseded_by"]["id"] == hidden_claim


# Major: owner review sees restricted records; replace_text inherits correction sensitivity.
def test_owner_review_and_replace_text_sensitivity(env: Env) -> None:
    note = new_id("entry")
    env.apply([create_entry(note, title="Plain note", text="original")])
    correction = new_id("correction")
    env.apply(
        [
            {
                "op": "create",
                "kind": "correction",
                "id": correction,
                "base_rev": 0,
                "sensitivity": "restricted",
                "occurred_at": None,
                "body": {
                    "target_refs": [{"id": note, "rev": 1}],
                    "replacement_refs": [],
                    "reason": "Uses restricted interview detail",
                    "evidence": [],
                    "effects": [
                        {
                            "target_ref": {"id": note, "rev": 1},
                            "effect": "replace_text",
                            "replacement_ref": None,
                            "replacement_text": "Restricted wording RW-77",
                        }
                    ],
                    "status": "proposed",
                    "applied_seq": None,
                },
            }
        ],
        caller=OWNER,
    )
    raw_ops = [{"op": "correction_apply", "id": correction, "base_rev": 1}]
    from cp1_support import request

    agent_preview = env.svc.review_request(
        env.project_id, request(env.project_id, raw_ops, env.seq())
    )
    assert agent_preview["would_apply"] is False  # agents cannot even see the correction
    owner_preview = env.svc.review_request(
        env.project_id, request(env.project_id, raw_ops, env.seq()), OWNER
    )
    assert owner_preview["would_apply"] is True
    env.apply_protected(raw_ops, caller=OWNER)
    target = env.svc.get(env.project_id, OWNER, note, None)
    assert (
        target["sensitivity"] == "restricted"
        and target["body"]["text"] == "Restricted wording RW-77"
    )
    assert "RW-77" not in json.dumps(env.svc.notebook(env.project_id, AGENT))


# Minor: item identity ignores quote digests; meeting capture retries replay.
def test_item_identity_and_capture_idempotency(env: Env) -> None:
    notes = (env.research / "meeting.txt").read_text("utf-8")
    rid = new_request_id()
    first = env.svc.capture_meeting(
        env.project_id, title="M", text=notes, held_at=None, request_id=rid
    )
    again = env.svc.capture_meeting(
        env.project_id, title="M", text=notes, held_at=None, request_id=rid
    )
    assert again["meeting_id"] == first["meeting_id"] and again["commit"]["replayed"] is True
    import hashlib

    line = notes.splitlines()[2]
    base = {
        "source_id": first["source_id"],
        "version_id": first["version_id"],
        "locator": {"type": "text", "start_line": 3, "end_line": 3},
    }

    def item(quote: str | None) -> dict[str, Any]:
        return {
            "op": "create",
            "kind": "item",
            "id": new_id("item"),
            "base_rev": 0,
            "sensitivity": "normal",
            "occurred_at": None,
            "body": {
                "meeting_id": first["meeting_id"],
                "type": "commitment",
                "text": line,
                "evidence": [dict(base, quote_sha256=quote)],
                "proposed_owner": None,
                "due_at": None,
                "status": "proposed",
                "action_id": None,
            },
        }

    env.apply([item(None)])
    with pytest.raises(PrometheosError) as err:
        env.apply([item(hashlib.sha256(line.encode()).hexdigest())])
    assert err.value.code == "revision_conflict"


# Minor: the agenda lists a prior adopted commitment with its completion evidence.
def test_agenda_shows_prior_commitment(env: Env) -> None:
    notes = (env.research / "meeting.txt").read_text("utf-8")
    first = env.svc.capture_meeting(env.project_id, title="First", text=notes, held_at=None)
    item, action = new_id("item"), new_id("action")
    env.apply(
        [
            {
                "op": "create",
                "kind": "item",
                "id": item,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {
                    "meeting_id": first["meeting_id"],
                    "type": "commitment",
                    "text": notes.splitlines()[2],
                    "evidence": [],
                    "proposed_owner": None,
                    "due_at": None,
                    "status": "proposed",
                    "action_id": None,
                },
            }
        ]
    )
    env.apply_protected(
        [
            {
                "op": "create",
                "kind": "action",
                "id": action,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {
                    "title": "Attach plant result",
                    "reason": "commitment",
                    "source_refs": [],
                    "owner_ref": None,
                    "due_at": None,
                    "dependencies": [],
                    "status": "proposed",
                    "blocker": None,
                    "completion_evidence": [],
                    "commitment_item_id": item,
                },
            },
            {
                "op": "transition",
                "id": action,
                "base_rev": 1,
                "to": "ready",
                "reason": "adopt",
                "evidence": [],
            },
            {
                "op": "transition",
                "id": item,
                "base_rev": 1,
                "to": "adopted",
                "reason": "adopt",
                "evidence": [],
            },
        ]
    )
    second = env.svc.capture_meeting(env.project_id, title="Second", text="Agenda\n", held_at=None)
    agenda = env.svc.agenda(env.project_id, AGENT, second["meeting_id"])
    assert [c["item_id"] for c in agenda["prior_commitments"]] == [item]
    assert agenda["prior_commitments"][0]["action_status"] == "ready"


def test_raising_sensitivity_is_an_owner_decision(env: Env) -> None:
    note = new_id("entry")
    env.apply([create_entry(note, title="Challenge to keep visible")])
    with pytest.raises(PrometheosError) as err:
        env.apply(
            [
                {
                    "op": "revise",
                    "id": note,
                    "base_rev": 1,
                    "sensitivity": "restricted",
                    "occurred_at": None,
                    "body": create_entry(title="x")["body"],
                }
            ]
        )
    assert err.value.code == "approval_required"
