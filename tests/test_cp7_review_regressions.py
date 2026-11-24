"""Regressions for defects found by the early CP7 review (reviewer-subagent-cp7-pre).

F1 corrections reach resolved anchors; F2 LaTeX manuscripts take line anchors and text
previews; F3 handoff never clobbers or half-writes the chosen folder; F4 purge removes job
slot/notification remnants; F5 a digest never counts records above its own visibility;
S1 a lost scheduler lease is reported, not claimed as success; S2 a reviewer comment cannot
be "resolved" by citing itself; S3 related papers pass on their sensitivity."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry
from cp7_support import (
    OVERBROAD_LINE,
    SCOPE_OVERBROAD,
    SCOPE_SIMPLIFIED,
    PaperProject,
    anchor_body,
    create,
    paper_body,
    text_evidence,
)

from prometheos.app import jobs
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError


@pytest.fixture
def pp(tmp_path: Path) -> PaperProject:
    return PaperProject(Env(tmp_path))


def supersede(pp: PaperProject, target: str) -> str:
    replacement = pp.claim(
        "Narrowed: SIMPLIFIED-A only.", SCOPE_SIMPLIFIED, text_evidence(pp.baseline, 1)
    )
    correction = new_id("correction")
    pp.env.apply(
        [
            create(
                "correction",
                {
                    "target_refs": [{"id": target, "rev": 1}],
                    "replacement_refs": [{"id": replacement, "rev": 1}],
                    "reason": "Overbroad.",
                    "evidence": [],
                    "effects": [
                        {
                            "target_ref": {"id": target, "rev": 1},
                            "effect": "supersede",
                            "replacement_ref": {"id": replacement, "rev": 1},
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
    pp.protected([{"op": "correction_apply", "id": correction, "base_rev": 1}])
    return correction


def test_f1_correction_reopens_a_resolved_anchor(pp: PaperProject) -> None:
    claim = pp.claim(
        OVERBROAD_LINE, SCOPE_OVERBROAD, text_evidence(pp.manuscript, pp.overbroad_line)
    )
    paper = pp.paper()
    anchor = pp.anchor(paper, pp.overbroad_line, claim_refs=[{"id": claim, "rev": 1}])
    pp.protected([pp.transition_op(anchor, "resolved", "Sentence rewritten.")])
    correction = supersede(pp, claim)
    body = pp.get(anchor)["body"]
    assert body["status"] == "needs_review" and correction in body["reason"]
    report = pp.svc.paper_check(pp.pid, AGENT, paper)
    assert any(f["record_ids"] == [anchor, claim] for f in report["findings"]
               if f["code"] == "claim_corrected_in_use")  # fmt: skip


def test_f1_resolved_anchor_still_reports_corrected_claims(pp: PaperProject) -> None:
    claim = pp.claim(
        OVERBROAD_LINE, SCOPE_OVERBROAD, text_evidence(pp.manuscript, pp.overbroad_line)
    )
    supersede(pp, claim)
    paper = pp.paper()
    anchor = pp.anchor(paper, pp.overbroad_line)
    pp.protected([pp.transition_op(anchor, "resolved", "Not used any more.")])
    # Later an agent points the resolved anchor at the superseded claim.
    pp.revise(anchor, dict(pp.get(anchor)["body"], claim_refs=[{"id": claim, "rev": 2}]))
    report = pp.svc.paper_check(pp.pid, AGENT, paper)
    assert any(f["code"] == "claim_corrected_in_use" and anchor in f["record_ids"]
               for f in report["findings"])  # fmt: skip


def test_f2_latex_manuscript_takes_line_anchors_and_text_preview(pp: PaperProject) -> None:
    tex = "\\section{Results}\nThe candidate improves energy recovery.\n\\end{document}\n"
    (pp.env.research / "paper.tex").write_bytes(tex.encode())
    item = pp.env.import_files(["paper.tex"])["plan"]["items"][0]
    version = pp.get(item["version_id"])["body"]
    assert version["mime"] != "text/plain"  # e.g. application/x-tex on Windows
    paper = new_id("paper")
    pp.env.apply([create("paper", paper_body(manuscript_source_id=item["source_id"]), paper)])
    anchor = new_id("anchor")
    pp.env.apply([create("anchor", anchor_body(paper, item["version_id"], 2), anchor)])
    data, media_type = pp.svc.version_content(pp.pid, OWNER, item["version_id"])
    assert data == tex.encode() and media_type == "text/plain; charset=utf-8"
    # A new version with two lines inserted above: the anchor follows the exact text.
    (pp.env.research / "paper.tex").write_bytes(("% draft two\n%\n" + tex).encode())
    pp.env.import_files(["paper.tex"])
    moved = pp.get(anchor)["body"]
    assert moved["status"] == "current" and moved["locator"]["start_line"] == 4


def test_f3_handoff_keeps_unrelated_files_and_refuses_before_writing(
    pp: PaperProject, tmp_path: Path
) -> None:
    paper = pp.paper()
    raw = json.dumps(
        {
            "schema_version": 1,
            "project_id": pp.pid,
            "request_id": new_request_id(),
            "expected_seq": pp.env.seq(),
            "operations": [
                {
                    "op": "relation_add",
                    "source_ref": {"id": pp.plant["version_id"], "rev": 1},
                    "target_ref": {"id": paper, "rev": 1},
                    "relation": "used_in",
                }
            ],
        }
    ).encode()
    pp.svc.apply(pp.pid, raw, OWNER)
    into = tmp_path / "clone"
    into.mkdir()
    (into / ".manuscript.md.prometheos-tmp").write_text("someone else's file", "utf-8")
    (into / "prometheos-artifacts").write_text("a file, not a folder", "utf-8")
    plan = pp.svc.paper_handoff_plan(pp.pid, AGENT, paper, into)
    with pytest.raises(PrometheosError) as err:
        pp.svc.paper_handoff_apply(pp.pid, AGENT, paper, into, plan["digest"], [])
    assert err.value.code == "policy_denied"
    assert not (into / "manuscript.md").exists()  # nothing half-written
    (into / "prometheos-artifacts").unlink()
    plan = pp.svc.paper_handoff_plan(pp.pid, AGENT, paper, into)
    pp.svc.paper_handoff_apply(pp.pid, AGENT, paper, into, plan["digest"], [])
    assert (into / ".manuscript.md.prometheos-tmp").read_text("utf-8") == "someone else's file"
    leftovers = [p.name for p in into.rglob(".prometheos-handoff-*")]
    assert leftovers == []


def _tick_after(env: Env, minutes: int) -> dict[str, Any]:
    return env.svc.jobs_tick(env.project_id, now=datetime.now(UTC) + timedelta(minutes=minutes))


def _interval_job(env: Env, handler: str, params: dict[str, Any]) -> str:
    start = datetime.now(UTC).replace(second=0, microsecond=0)
    body = {
        "handler": handler,
        "parameters": params,
        "schedule": {"type": "interval", "minutes": 15, "start_utc": jobs.fmt(start)},
        "enabled": True,
        "budget": {
            "max_inputs": 20,
            "max_output_chars": 2000,
            "max_provider_calls": 0,
            "max_concurrent_runs": 1,
            "spend_cap_estimate_usd": None,
        },
        "last_slot": None,
    }
    return str(env.svc.job_add(env.project_id, OWNER, body, new_request_id())["job_id"])


def test_f4_purge_removes_job_remnants(pp: PaperProject) -> None:
    env = pp.env
    digest = _interval_job(env, "digest", {})
    paper = pp.paper()
    watcher = _interval_job(env, "paper_check", {"paper_id": paper})
    _tick_after(env, 16)
    inbox = env.svc.inbox(pp.pid, OWNER, include_dismissed=True)["items"]
    assert any(paper in json.dumps(i) for i in inbox)
    plan = env.svc.purge_plan(pp.pid, [paper], OWNER)
    assert watcher in plan["closure"]
    grant = env.svc.grant(pp.pid, operation="purge", digest=plan["plan_sha256"],
                          label="SYNTHETIC-TEST purge", synthetic=True)  # fmt: skip
    env.svc.purge_commit(pp.pid, plan["plan_id"], grant.secret)
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        notes = " ".join(
            " ".join(map(str, r)) for r in conn.execute("SELECT * FROM job_notifications")
        )
        slots = " ".join(" ".join(map(str, r)) for r in conn.execute("SELECT * FROM job_slots"))
        watcher_slots = conn.execute(
            "SELECT COUNT(*) FROM job_slots WHERE job_id = ?", (watcher,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert paper not in notes and paper not in slots and watcher_slots == 0
    assert digest  # the digest job itself survives; only its mentions were removed


def test_f5_digest_counts_only_what_its_readers_may_see(pp: PaperProject) -> None:
    env = pp.env
    _interval_job(env, "digest", {})
    env.apply([create_entry(title="Public note")])
    env.apply([create_entry(title="Private note", sensitivity="private")], caller=OWNER)
    _tick_after(env, 16)
    (note,) = env.svc.inbox(pp.pid, AGENT)["items"]
    assert note["summary"] == "entry: 1"
    assert note["title"].startswith("Project digest: 1 record(s)")


def test_s1_lost_lease_is_not_reported_as_success(
    pp: PaperProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = pp.env

    def slow(ctx: jobs.JobContext) -> jobs.Outcome:
        conn = ctx.store.connect()
        with conn:  # another scheduler recovered and re-claimed this slot meanwhile
            conn.execute(
                "UPDATE job_slots SET lease_owner = 'other-tick' WHERE job_id = ? AND due_utc = ?",
                (ctx.job["id"], ctx.due_utc),
            )
        conn.close()
        return jobs.Outcome({"usage": jobs.USAGE}, "x")

    monkeypatch.setitem(jobs.HANDLERS, "digest", slow)
    _interval_job(env, "digest", {})
    result = _tick_after(env, 16)
    assert [r["state"] for r in result["ran"]] == ["lease_lost"]


def test_s2_comment_cannot_resolve_itself(pp: PaperProject) -> None:
    comment = text_evidence(pp.plant, 1)
    item = {
        "id": "c1",
        "comment_source_ref": comment,
        "text": "Please narrow the claim.",
        "response_text": "Done.",
        "change_refs": [],
        "status": "open",
        "resolution_evidence": [],
    }
    review_round = {"id": "r", "label": "Round", "source_refs": [], "received_at": None,
                    "items": [item]}  # fmt: skip
    paper = pp.paper(review_rounds=[review_round])
    base = pp.get(paper)["body"]
    cases = (
        # Citing the comment's own location is not resolution evidence: owner decision.
        (dict(item, status="resolved", resolution_evidence=[comment]), "approval_required"),
        # The paper itself is not a change record at all.
        (dict(item, status="resolved", change_refs=[{"id": paper, "rev": 1}]), "validation_error"),
    )
    for resolved, code in cases:
        with pytest.raises(PrometheosError) as err:
            pp.revise(paper, dict(base, review_rounds=[dict(review_round, items=[resolved])]))
        assert err.value.code == code


def test_s3_related_paper_passes_on_its_sensitivity(pp: PaperProject) -> None:
    private = new_id("paper")
    pp.env.apply([create("paper", paper_body(), private, "private")], caller=OWNER)
    related = [{"paper_id": private, "relationship": "potential_overlap",
                "explanation": "Describes the unpublished companion paper."}]  # fmt: skip
    with pytest.raises(PrometheosError) as err:
        pp.env.apply([create("paper", paper_body(related_papers=related))], caller=OWNER)
    assert err.value.code == "policy_denied"
    pp.env.apply(
        [create("paper", paper_body(related_papers=related), None, "private")], caller=OWNER
    )
