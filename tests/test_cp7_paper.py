"""C07-PAPER (S16): a correction reaches the bound manuscript anchor, paper check is
pure, and source-derived venue requirements keep their unknown states.

Synthetic fixtures only. The reviewer should read the asserted statuses, reasons and
finding codes, not just counts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env, create_entry, request
from cp7_support import (
    OVERBROAD_LINE,
    SCOPE_OVERBROAD,
    SCOPE_SIMPLIFIED,
    PaperProject,
    anchor_body,
    create,
    line_of,
    paper_body,
    table_counts,
    text_evidence,
)
from mcp import Client

from prometheos.app.quantities import unbound_numbers
from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server


@pytest.fixture
def pp(tmp_path: Path) -> PaperProject:
    return PaperProject(Env(tmp_path))


def codes(report: dict[str, Any]) -> set[str]:
    return {f["code"] for f in report["findings"]}


def denied(pp: PaperProject, ops: list[dict[str, Any]], code: str) -> PrometheosError:
    with pytest.raises(PrometheosError) as err:
        pp.env.apply(ops)
    assert err.value.code == code, err.value.message
    return err.value


# ----- S16: correction -> anchor -> pure check ----------------------------------------------


def test_correction_reaches_anchor_and_check_is_pure(pp: PaperProject) -> None:
    env, svc, pid = pp.env, pp.svc, pp.pid
    ev_manuscript = text_evidence(pp.manuscript, pp.overbroad_line, OVERBROAD_LINE)
    overbroad = pp.claim(OVERBROAD_LINE, SCOPE_OVERBROAD, ev_manuscript)
    paper = pp.paper(claim_refs=[{"id": overbroad, "rev": 1}])
    anchor = pp.anchor(paper, pp.overbroad_line, claim_refs=[{"id": overbroad, "rev": 1}])
    assert pp.get(anchor)["body"]["status"] == "current"

    # Paper check twice: identical findings, no research event, no hidden write.
    before_seq, before_counts = env.seq(), table_counts(env.store_dir)
    first = svc.paper_check(pid, AGENT, paper)
    second = svc.paper_check(pid, AGENT, paper)
    assert first == second
    assert env.seq() == before_seq and table_counts(env.store_dir) == before_counts
    assert {"venue_unknown", "claim_without_support"} <= codes(first)
    assert "anchor_needs_review" not in codes(first)
    assert not {"score", "probability", "compliant", "verdict"} & set(first)
    assert any("No acceptance probability" in text for text in first["limitations"])

    # The overbroad inference is narrowed: replacement claim plus an applied correction.
    plant_line = line_of(env.research / "baseline.md", "Recovered energy with the candidate")
    narrowed = pp.claim(
        "The candidate recovered more energy than the reference in SIMPLIFIED-A only.",
        SCOPE_SIMPLIFIED,
        text_evidence(pp.baseline, plant_line),
    )
    correction = new_id("correction")
    env.apply(
        [
            create(
                "correction",
                {
                    "target_refs": [{"id": overbroad, "rev": 1}],
                    "replacement_refs": [{"id": narrowed, "rev": 1}],
                    "reason": "PLANT-B under actuator constraints does not support it.",
                    "evidence": [],
                    "effects": [
                        {
                            "target_ref": {"id": overbroad, "rev": 1},
                            "effect": "supersede",
                            "replacement_ref": {"id": narrowed, "rev": 1},
                            "replacement_text": None,
                        }
                    ],
                    "status": "proposed",
                    "applied_seq": None,
                },
                correction,
            )
        ]
    )
    # A proposal alone changes nothing about the anchor.
    assert pp.get(anchor)["body"]["status"] == "current"
    applied = pp.protected([{"op": "correction_apply", "id": correction, "base_rev": 1}])

    flagged = pp.get(anchor)
    assert flagged["body"]["status"] == "needs_review"
    assert correction in flagged["body"]["reason"] and overbroad in flagged["body"]["reason"]
    assert "supersede" in flagged["body"]["reason"]
    history = svc.history(pid, OWNER, anchor)
    assert history["revisions"][-1]["commit_seq"] == applied["project_seq"]  # same transaction
    assert history["revisions"][-1]["operation"] == "anchor_review:correction"

    report = svc.paper_check(pid, AGENT, paper)
    assert {"claim_corrected_in_use", "anchor_needs_review"} <= codes(report)
    corrected = [f for f in report["findings"] if f["code"] == "claim_corrected_in_use"]
    assert any(f["record_ids"] == [anchor, overbroad] for f in corrected)
    assert all(f["detail"]["superseded_by"] == narrowed for f in corrected)
    assert svc.paper_check(pid, AGENT, paper) == report  # still pure

    # An agent cannot clear the review flag; the owner approves a remap to the narrowed claim.
    denied(pp, [pp.transition_op(anchor, "current", "Looks fine to me.")], "approval_required")
    remap_body = dict(flagged["body"], claim_refs=[{"id": narrowed, "rev": 1}])
    revise = {
        "op": "revise",
        "id": anchor,
        "base_rev": flagged["rev"],
        "sensitivity": "normal",
        "occurred_at": None,
        "body": remap_body,
    }
    clear = {
        "op": "transition",
        "id": anchor,
        "base_rev": flagged["rev"] + 1,
        "to": "current",
        "reason": "Owner rebound the sentence to the narrowed claim.",
        "evidence": [],
    }
    pp.protected([revise, clear])
    cleared = pp.get(anchor)
    assert cleared["body"]["status"] == "current" and cleared["body"]["reason"] is None
    final = svc.paper_check(pid, AGENT, paper)
    assert "anchor_needs_review" not in codes(final)
    # The paper itself still cites the superseded claim: reported, never silently fixed.
    assert any(f["record_ids"] == [paper, overbroad] for f in final["findings"])


def test_paper_check_save_is_explicit(pp: PaperProject) -> None:
    paper = pp.paper()
    seq = pp.env.seq()
    saved = pp.svc.paper_check_save(pp.pid, paper, "6f7c9a52-1f0e-4c1b-9a4e-5a7f2c3d4e5f")
    assert saved["saved"]["project_seq"] == seq + 1
    entry_id = saved["saved"]["results"][0]["id"]
    entry = pp.get(entry_id)
    assert entry["body"]["origin"] == "generated"
    assert "venue_unknown" in entry["body"]["text"] and paper in entry["body"]["text"]
    # Retry with the same request ID replays rather than writing again.
    again = pp.svc.paper_check_save(pp.pid, paper, "6f7c9a52-1f0e-4c1b-9a4e-5a7f2c3d4e5f")
    assert again["saved"]["replayed"] is True and pp.env.seq() == seq + 1


def test_record_ids_are_not_unbound_numbers() -> None:
    """Decision 0007 item 10 regression: identifiers are not quantities; numbers still are."""
    rid = "anchor_00000000-0000-4000-8000-000000000001"
    assert unbound_numbers(f"see {rid} for context", []) == []
    assert unbound_numbers(f"see {rid}: 3 more", []) == ["3"]
    assert unbound_numbers("CP2 is a label", []) == []


# ----- paper records ----------------------------------------------------------------------


def test_paper_status_rules(pp: PaperProject) -> None:
    ms = pp.manuscript["source_id"]
    for status in ("submitted", "revision", "accepted", "withdrawn"):
        denied(
            pp,
            [create("paper", paper_body(manuscript_source_id=ms, status=status))],
            "policy_denied",
        )
    paper = pp.paper(status="planning")
    pp.transition(paper, "drafting")
    denied(pp, [pp.transition_op(paper, "ready_for_submission")], "validation_error")
    pp.transition(paper, "internal_review")
    pp.transition(paper, "ready_for_submission")
    # Submission is an owner-confirmed external event.
    denied(pp, [pp.transition_op(paper, "submitted")], "approval_required")
    pp.protected([pp.transition_op(paper, "submitted", "Owner submitted via the venue portal.")])
    pp.transition(paper, "revision")
    denied(pp, [pp.transition_op(paper, "drafting")], "validation_error")  # back needs reason
    pp.transition(paper, "drafting", "Major revision requested; rewriting section 3.")
    denied(pp, [pp.transition_op(paper, "withdrawn")], "validation_error")
    pp.transition(paper, "withdrawn", "Merged into a different paper.")
    # Status never changes through revise.
    body = dict(pp.get(paper)["body"], status="drafting")
    with pytest.raises(PrometheosError) as err:
        pp.revise(paper, body)
    assert err.value.code == "policy_denied"
    notes = pp.svc.history(pp.pid, OWNER, paper)["transitions"]
    assert [n["to"] for n in notes][-2:] == ["drafting", "withdrawn"]
    assert notes[3]["protected_effects"] == ["paper ready_for_submission->submitted"]


def test_requirements_are_source_derived_and_stay_unknown(pp: PaperProject) -> None:
    venue = {"id": pp.venue["version_id"], "rev": 1}
    draft = pp.svc.requirements_draft(pp.pid, AGENT, pp.venue["version_id"])
    rows = draft["requirements"]
    assert [r["text"] for r in rows] == [
        "Manuscripts must not exceed eight pages including references.",
        "State the model fidelity of every simulated result.",
        "Provide a data availability statement.",
    ]
    assert {(r["applicability"], r["status"]) for r in rows} == {("unknown", "unknown")}
    paper = pp.paper(venue_source_refs=[venue], requirements=rows)  # quotes verify
    report = pp.svc.paper_check(pp.pid, AGENT, paper)
    unchecked = [f for f in report["findings"] if f["code"] == "requirement_unchecked"]
    assert [f["detail"]["requirement"] for f in unchecked] == ["r1", "r2", "r3"]
    assert "venue_unknown" not in codes(report) and report["venue"]["known"]
    assert report["venue"]["sources"][0]["captured_at"]

    # A requirement must cite one of the paper's venue sources, at a real line.
    stray = dict(rows[0], source_ref=text_evidence(pp.baseline, 1))
    denied(
        pp,
        [create("paper", paper_body(venue_source_refs=[venue], requirements=[stray]))],
        "validation_error",
    )
    wrong_quote = dict(rows[0], source_ref=dict(rows[0]["source_ref"], quote_sha256="0" * 64))
    denied(
        pp,
        [create("paper", paper_body(venue_source_refs=[venue], requirements=[wrong_quote]))],
        "validation_error",
    )
    # Status rules: met needs evidence and a check date; unknown applicability stays unknown.
    for bad in (
        dict(rows[0], status="met"),
        dict(
            rows[0],
            applicability="applicable",
            status="met",
            last_checked_at="2026-10-04T10:00:00Z",
        ),
        dict(
            rows[0], applicability="unknown", status="todo", last_checked_at="2026-10-04T10:00:00Z"
        ),
        dict(rows[0], applicability="applicable", status="todo"),
    ):
        denied(
            pp,
            [create("paper", paper_body(venue_source_refs=[venue], requirements=[bad]))],
            "validation_error",
        )

    # Recording a check removes exactly that row from the findings.
    page_count = text_evidence(pp.manuscript, 1)
    met = dict(
        rows[0],
        applicability="applicable",
        status="met",
        last_checked_at="2026-10-04T10:00:00Z",
        evidence=[page_count],
    )
    body = dict(pp.get(paper)["body"], requirements=[met, rows[1], rows[2]])
    pp.revise(paper, body)
    after = pp.svc.paper_check(pp.pid, AGENT, paper)
    left = [
        f["detail"]["requirement"]
        for f in after["findings"]
        if f["code"] == "requirement_unchecked"
    ]
    assert left == ["r2", "r3"]


def test_review_rounds_keep_history(pp: PaperProject) -> None:
    comment = text_evidence(pp.plant, 1)
    round_1 = {
        "id": "round-a",
        "label": "Internal review A (synthetic)",
        "source_refs": [{"id": pp.plant["version_id"], "rev": 1}],
        "received_at": "2026-10-01T09:00:00Z",
        "items": [
            {
                "id": "c1",
                "comment_source_ref": comment,
                "text": "The general claim is not supported by PLANT-B.",
                "response_text": "",
                "change_refs": [],
                "status": "open",
                "resolution_evidence": [],
            },
            {
                "id": "c2",
                "comment_source_ref": comment,
                "text": "Label the model fidelity in the figure.",
                "response_text": "",
                "change_refs": [],
                "status": "open",
                "resolution_evidence": [],
            },
        ],
    }
    paper = pp.paper(review_rounds=[round_1])
    report = pp.svc.paper_check(pp.pid, AGENT, paper)
    assert [f["detail"]["item"] for f in report["findings"] if f["code"] == "review_item_open"] == [
        "c1",
        "c2",
    ]
    base = pp.get(paper)["body"]

    def with_items(*items: dict[str, Any]) -> dict[str, Any]:
        return dict(base, review_rounds=[dict(round_1, items=list(items))])

    c1, c2 = round_1["items"]
    with pytest.raises(PrometheosError) as err:  # comments cannot be removed
        pp.revise(paper, with_items(c1))
    assert err.value.code == "policy_denied"
    with pytest.raises(PrometheosError) as err:  # nor reworded
        pp.revise(paper, with_items(dict(c1, text="Softer wording."), c2))
    assert err.value.code == "policy_denied"
    with pytest.raises(PrometheosError) as err:  # nor the round dropped
        pp.revise(paper, dict(base, review_rounds=[]))
    assert err.value.code == "policy_denied"

    # "Drafted" is not resolution; resolution needs linked change/evidence ...
    drafted = dict(c1, status="drafted", response_text="We narrowed the claim.")
    pp.revise(paper, with_items(drafted, c2))
    assert any(
        f["detail"].get("status") == "drafted"
        for f in pp.svc.paper_check(pp.pid, AGENT, paper)["findings"]
    )
    base = pp.get(paper)["body"]
    bare = dict(drafted, status="resolved")
    with pytest.raises(PrometheosError) as err:
        pp.revise(paper, with_items(bare, c2))
    assert err.value.code == "approval_required"  # ... or an owner-approved rationale
    linked = dict(bare, change_refs=[{"id": pp.manuscript["version_id"], "rev": 1}])
    pp.revise(paper, with_items(linked, c2))
    base = pp.get(paper)["body"]
    rationale = dict(c2, status="resolved", response_text="Out of scope for this venue.")
    current = pp.get(paper)
    pp.protected(
        [
            {
                "op": "revise",
                "id": paper,
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": with_items(linked, rationale),
            }
        ]
    )
    final = pp.svc.paper_check(pp.pid, AGENT, paper)
    assert "review_item_open" not in codes(final)


# ----- anchors and manuscript versions ------------------------------------------------------


def test_anchor_binding_rules(pp: PaperProject) -> None:
    paper = pp.paper()
    lines = len((pp.env.research / "manuscript.md").read_text("utf-8").splitlines())
    denied(
        pp,
        [create("anchor", anchor_body(paper, pp.manuscript["version_id"], lines + 1))],
        "validation_error",
    )
    denied(
        pp, [create("anchor", anchor_body(paper, pp.baseline["version_id"], 1))], "validation_error"
    )  # fmt: skip  (not a version of the manuscript source)
    flagged = anchor_body(paper, pp.manuscript["version_id"], 1, status="needs_review", reason="x")
    denied(pp, [create("anchor", flagged)], "policy_denied")
    pdf_on_text = anchor_body(
        paper, pp.manuscript["version_id"], 1, locator={"type": "pdf", "page": 1, "bbox": None}
    )
    denied(pp, [create("anchor", pdf_on_text)], "validation_error")
    no_manuscript = new_id("paper")
    pp.env.apply([create("paper", paper_body(), no_manuscript)])
    denied(
        pp,
        [create("anchor", anchor_body(no_manuscript, pp.manuscript["version_id"], 1))],
        "validation_error",
    )
    anchor = pp.anchor(paper, 1)
    body = dict(pp.get(anchor)["body"], paper_id=no_manuscript)
    with pytest.raises(PrometheosError) as err:
        pp.revise(anchor, body)
    assert err.value.code == "policy_denied"


def test_new_manuscript_version_remaps_exactly_or_flags(pp: PaperProject) -> None:
    env = pp.env
    path = env.research / "manuscript.md"
    original = path.read_text("utf-8").splitlines()
    figure_line = next(i for i, t in enumerate(original, 1) if t.startswith("Figure placeholder"))
    paper = pp.paper()
    moved = pp.anchor(paper, pp.overbroad_line)
    edited = pp.anchor(paper, figure_line)
    title = pp.anchor(paper, 1)

    # Two new lines at the top; the figure sentence is reworded; the title is duplicated.
    lines = ["# Synthetic manuscript fragment (draft two)", "", *original]
    lines[figure_line + 1] = "Figure placeholder: compare SIMPLIFIED-A with PLANT-B by regime."
    lines.append(original[0])
    path.write_text("\n".join(lines) + "\n", "utf-8")
    second = env.import_files(["manuscript.md"])
    new_version = second["plan"]["items"][0]["version_id"]

    remapped = pp.get(moved)["body"]
    assert remapped["manuscript_version_id"] == new_version
    assert remapped["locator"]["start_line"] == pp.overbroad_line + 2
    assert remapped["status"] == "current"
    flagged = pp.get(edited)["body"]
    assert flagged["status"] == "needs_review"
    assert flagged["manuscript_version_id"] == pp.manuscript["version_id"]  # binding kept
    assert "no longer occur exactly" in flagged["reason"]
    duplicated = pp.get(title)["body"]
    assert duplicated["status"] == "needs_review" and "more than once" in duplicated["reason"]
    ops = {r["operation"] for r in pp.svc.history(pp.pid, OWNER, moved)["revisions"]}
    assert "anchor_exact_remap" in ops

    report = pp.svc.paper_check(pp.pid, AGENT, paper)
    stale = {f["record_ids"][0] for f in report["findings"] if f["code"] == "anchor_stale_version"}
    assert stale == {edited, title}
    review = {
        f["record_ids"][0]: f for f in report["findings"] if f["code"] == "anchor_needs_review"
    }
    # Deterministic candidates: exact first-line matches in the latest version only.
    assert [c["start_line"] for c in review[title]["detail"]["remap_candidates"]] == [
        1 + 2,
        len(lines),
    ]
    assert review[edited]["detail"]["remap_candidates"] == []
    assert report["manuscript"]["latest"]["version_id"] == new_version


def test_agent_scope_hides_restricted_inputs(pp: PaperProject) -> None:
    secret_claim = new_id("claim")
    pp.env.apply(
        [
            create(
                "claim",
                {
                    "statement": "Unpublished collaborator result (restricted).",
                    "scope": SCOPE_SIMPLIFIED,
                    "evidence": [],
                    "assessment_refs": [],
                    "state": "proposed",
                    "superseded_by": None,
                },
                secret_claim,
                "restricted",
            )
        ],
        caller=OWNER,
    )
    paper = pp.paper()
    anchor = new_id("anchor")
    pp.env.apply(
        [
            create(
                "anchor",
                anchor_body(
                    paper,
                    pp.manuscript["version_id"],
                    1,
                    claim_refs=[{"id": secret_claim, "rev": 1}],
                ),
                anchor,
                "restricted",
            )
        ],
        caller=OWNER,
    )
    agent_view = pp.svc.paper_check(pp.pid, AGENT, paper)
    assert secret_claim not in json.dumps(agent_view) and anchor not in json.dumps(agent_view)
    assert agent_view["hidden_references"] >= 1
    owner_view = pp.svc.paper_check(pp.pid, OWNER, paper)
    assert any(secret_claim in f["record_ids"] for f in owner_view["findings"])
    # Sensitivity is inherited: a normal anchor cannot cite the restricted claim.
    with pytest.raises(PrometheosError) as err:
        pp.env.apply(
            [
                create(
                    "anchor",
                    anchor_body(
                        paper,
                        pp.manuscript["version_id"],
                        1,
                        claim_refs=[{"id": secret_claim, "rev": 1}],
                    ),
                )
            ],
            caller=OWNER,
        )
    assert err.value.code == "policy_denied"


def test_paper_check_over_mcp_matches_service(pp: PaperProject) -> None:
    paper = pp.paper()

    async def main() -> Any:
        async with Client(build_server(pp.svc, pp.pid)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            result = await client.call_tool("paper_check", {"paper_id": paper})
            missing = await client.call_tool("paper_check", {"paper_id": new_id("paper")})
            return tools["paper_check"], result, missing

    tool, result, missing = anyio.run(main)
    assert tool.annotations.read_only_hint is True and tool.annotations.idempotent_hint is True
    assert result.structured_content == json.loads(
        json.dumps(pp.svc.paper_check(pp.pid, AGENT, paper))
    )
    assert missing.is_error and "not_found" in missing.content[0].text


def test_paper_and_anchor_kinds_need_no_other_route(pp: PaperProject) -> None:
    """Jobs, unlike papers, are only created through their own commands."""
    job = create(
        "job",
        {
            "handler": "digest",
            "parameters": {},
            "schedule": {"type": "interval", "minutes": 60, "start_utc": "2026-10-04T00:00:00Z"},
            "enabled": True,
            "budget": {
                "max_inputs": 10,
                "max_output_chars": 1000,
                "max_provider_calls": 0,
                "max_concurrent_runs": 1,
                "spend_cap_estimate_usd": None,
            },
            "last_slot": None,
        },
    )
    denied(pp, [job], "policy_denied")
    # Ordinary entries may still reference papers through used_in relations.
    entry = new_id("entry")
    paper = pp.paper()
    pp.env.apply([create_entry(entry)])
    raw = request(
        pp.pid,
        [
            {
                "op": "relation_add",
                "source_ref": {"id": entry, "rev": 1},
                "target_ref": {"id": paper, "rev": 1},
                "relation": "used_in",
            }
        ],
        pp.env.seq(),
    )
    assert pp.svc.apply(pp.pid, raw)["ok"]
