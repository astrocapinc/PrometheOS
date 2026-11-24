"""CP10.4 research continuity (C10-CONTINUITY; S26; decision 0018).

The S26 scenario runs through the service here; ``test_cp10_continuity_transports.py`` repeats
it through MCP, HTTP and the CLI and compares outcomes. Every test uses disposable homes and
the synthetic fixtures.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, FULL, OWNER
from cp7_support import create
from cp10_continuity_support import Scenario, entry_body

from prometheos.app import continuity
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    return Scenario(tmp_path).build()


def refused(fn: Any) -> PrometheosError:
    with pytest.raises(PrometheosError) as err:
        fn()
    return err.value


def ids(section: dict[str, Any]) -> list[str]:
    return [item["id"] for item in section["items"]]


def resolve_as_owner(
    s: Scenario, target: str, resolution: str, text: str, rid: str | None = None
) -> dict[str, Any]:
    """The owner takes a queue item out: a one-step exact-request grant, like the hub."""
    rid = rid or new_request_id()
    raw = s.svc.awaiting_resolve_request(s.pid, OWNER, target, resolution, text, rid)
    digest = s.svc.review_request(s.pid, raw, OWNER)["digest"]
    grant = s.svc.grant(
        s.pid, operation="exact_request", digest=digest, label="SYNTHETIC-TEST", synthetic=True
    )
    return s.svc.apply(s.pid, raw, OWNER, grant.secret)


# ----- S26, step by step ----------------------------------------------------------------


def test_s26_correction_flags_dependents_in_the_same_commit(scenario: Scenario) -> None:
    s = scenario
    apply_op, correction_id = s.supersede_correction()

    # The agent's apply needs the owner's decision: approval_required, nothing commits.
    seq_before = s.env.seq()
    denied = refused(lambda: s.env.apply([apply_op], caller=AGENT))
    assert denied.code == "approval_required"
    assert s.env.seq() == seq_before and s.body(s.drag).get("superseded_by") is None
    assert s.flags() == []

    # The owner applies it with a grant: effect, flags and anchor moves share one commit.
    result = s.env.apply_protected([apply_op], caller=OWNER)
    commit_seq = result["project_seq"]
    assert s.body(s.drag)["superseded_by"] == {"id": s.drag_v2, "rev": 1}
    flagged = {row["record_id"]: row for row in s.flags()}
    assert set(flagged) == {s.claim_a, s.action, s.anchor, s.anchor_claim}
    assert {row["created_seq"] for row in flagged.values()} == {commit_seq}
    assert all(row["cause_id"] == correction_id for row in flagged.values())
    # The assumption reaches the claim directly; the action and the claim's anchor are reached
    # through the claim, and say so.
    assert "relies on" not in flagged[s.claim_a]["reason"]
    assert s.claim_a in flagged[s.action]["reason"]
    assert s.claim_a in flagged[s.anchor_claim]["reason"]
    # The anchors move to needs_review in the same transaction, naming why.
    for anchor in (s.anchor, s.anchor_claim):
        body = s.body(anchor)
        assert body["status"] == "needs_review" and correction_id in body["reason"]
    # The unrelated control, the replacement, the correction and the paper stay untouched.
    assert s.body(s.anchor_control)["status"] == "current"
    for untouched in (s.claim_b, s.drag_v2, correction_id, s.paper_id):
        assert untouched not in flagged
    history = s.svc.history(s.pid, OWNER, s.anchor)
    assert history["revisions"][-1]["commit_seq"] == commit_seq


def test_s26_registered_runs_and_the_awaiting_queue(scenario: Scenario) -> None:
    s = scenario
    running = s.svc.execution_list(s.pid, AGENT)["executions"]
    states = {e["id"]: e["effective_state"] for e in running}
    assert states == {s.run_live: "reported_running", s.run_done: "reported_running"}
    # Nothing completed yet: only the bare receipt waits.
    queue = s.svc.awaiting_list(s.pid, AGENT)
    assert [i["target_id"] for i in queue["items"]] == [s.bare_receipt["version_id"]]
    assert queue["items"][0]["target_kind"] == "receipt"

    # The simulation is reported completed with a receipt: it waits until something cites it.
    receipt = s.receipt("Sweep receipt")
    s.name(receipt["version_id"], "done_receipt")
    s.report(s.run_done, "reported_completed", [receipt["version_id"]])
    queue = s.svc.awaiting_list(s.pid, AGENT)["items"]
    assert {i["target_id"]: i["target_kind"] for i in queue} == {
        s.run_done: "execution",
        s.bare_receipt["version_id"]: "receipt",
    }  # the registered run's own receipt is covered by the run, not listed on its own
    waiting = next(i for i in queue if i["target_id"] == s.run_done)
    assert "no assessment or reviewed entry cites it" in waiting["reason"]
    listed = {e["id"]: e for e in s.svc.execution_list(s.pid, AGENT)["executions"]}
    assert listed[s.run_done]["awaiting_interpretation"] is True
    assert listed[s.run_live]["awaiting_interpretation"] is False

    # Dismissing the bare receipt is a reviewed entry giving the reason: the owner needs a
    # grant, the agent gets approval_required, and only then does the item leave.
    reason = "Superseded by the later sweep; this smoke run measured nothing we use."
    asked = refused(
        lambda: s.svc.awaiting_resolve(
            s.pid, AGENT, s.bare_receipt["version_id"], "dismissed", reason, new_request_id()
        )
    )
    assert asked.code == "approval_required"
    assert s.bare_receipt["version_id"] in [
        i["target_id"] for i in s.svc.awaiting_list(s.pid, AGENT)["items"]
    ]
    done = resolve_as_owner(s, s.bare_receipt["version_id"], "dismissed", reason)
    entry_id = next(r["id"] for r in done["results"] if r["id"].startswith("entry_"))
    entry = s.body(entry_id)
    assert entry["review"] == "reviewed" and entry["text"] == reason
    assert entry["category"] == "decision"
    queue = s.svc.awaiting_list(s.pid, AGENT)["items"]
    assert [i["target_id"] for i in queue] == [s.run_done]
    # The run leaves only when an assessment or a reviewed entry cites it.
    assessment = new_id("assessment")
    s.env.apply_protected(
        [
            create(
                "assessment",
                {
                    "claim_ref": {"id": s.claim_a, "rev": 1},
                    "outcome": "challenges",
                    "rationale": "The completed sweep contradicts the constant-drag result.",
                    "checks": [],
                    "author_ref": None,
                    "method": "researcher",
                    "scope_match": True,
                },
                assessment,
            ),
            {
                "op": "relation_add",
                "source_ref": {"id": assessment, "rev": 1},
                "target_ref": {"id": s.run_done, "rev": 3},
                "relation": "cites",
            },
        ],
        caller=OWNER,
    )
    assert s.svc.awaiting_list(s.pid, AGENT)["items"] == []
    interpreted = {e["id"]: e for e in s.svc.execution_list(s.pid, AGENT)["executions"]}
    assert interpreted[s.run_done]["awaiting_interpretation"] is False
    assert interpreted[s.run_done]["interpreted_by"][0]["id"] == assessment


def test_now_lists_changed_due_running_awaiting_and_approvals_with_reasons(
    scenario: Scenario,
) -> None:
    s = scenario
    apply_op, _correction = s.supersede_correction()
    # The agent's attempt leaves a pending approval request; the owner's own apply flags records.
    assert refused(lambda: s.env.apply([apply_op], caller=AGENT)).code == "approval_required"
    s.env.apply_protected([apply_op], caller=OWNER)
    s.report(s.run_done, "reported_completed", [])
    view = s.svc.now(s.pid, AGENT)

    assert view["generation"] == "deterministic"
    changed = {i["id"]: i for i in view["changed"]["items"]}
    assert {s.claim_a, s.action, s.anchor, s.anchor_claim} <= set(changed)
    assert all(i["reason"].startswith("Needs review:") for i in changed.values())
    assert s.claim_b not in changed
    due = view["due"]["items"]
    assert [i["id"] for i in due] == [s.action]
    assert due[0]["reason"].startswith("Overdue since")
    running = {i["id"]: i for i in view["running"]["items"]}
    assert set(running) == {s.run_live}
    assert "Reported running" in running[s.run_live]["reason"]
    awaiting = {i["id"]: i for i in view["awaiting_interpretation"]["items"]}
    assert set(awaiting) == {s.run_done, s.bare_receipt["version_id"]}
    assert all("no assessment or reviewed entry cites it" in i["reason"] for i in awaiting.values())
    approvals = view["approvals"]["items"]
    assert len(approvals) == 1 and approvals[0]["operation"] == "apply"
    assert "asked for your decision" in approvals[0]["reason"]
    assert approvals[0]["owner_route"]["hub"].startswith("/approvals/apreq_")

    # since_seq adds what changed after a visit: a new note shows with its sequence.
    before = s.env.seq()
    s.env.apply([create("entry", entry_body("Late note", "Added after the visit."), None)])
    later = s.svc.now(s.pid, AGENT, since_seq=before)
    notes = [i for i in later["changed"]["items"] if i["title"] == "Late note"]
    assert len(notes) == 1 and f"after {before}" in notes[0]["reason"]
    # A limit truncates and says so.
    small = s.svc.now(s.pid, AGENT, limit=1)
    assert small["changed"]["truncated"] is True and len(small["changed"]["items"]) == 1
    assert small["changed"]["count"] >= 4


def test_silence_reads_as_status_unknown_never_failed_or_completed(scenario: Scenario) -> None:
    s = scenario
    far = datetime.now(UTC) + timedelta(hours=30)

    def read(fn: Any) -> Any:
        conn = s.svc.open(s.pid).connect(readonly=True)
        try:
            return fn(conn)
        finally:
            conn.close()

    views = {
        v["id"]: v
        for v in read(lambda c: continuity.execution_views(c, AGENT, now=far))["executions"]
    }
    for run in (s.run_live, s.run_done):
        assert views[run]["state"] == "reported_running"  # what was reported stays as stored
        assert views[run]["effective_state"] == "status_unknown"
        assert views[run]["silent_since"] is not None
    silent = read(lambda c: continuity.execution_views(c, AGENT, state="status_unknown", now=far))
    assert {v["id"] for v in silent["executions"]} == {s.run_live, s.run_done}
    for never in ("reported_failed", "reported_completed", "reported_cancelled"):
        assert (
            read(lambda c, n=never: continuity.execution_views(c, AGENT, state=n, now=far))[
                "executions"
            ]
            == []
        )
    # A completed run is never turned into unknown by silence.
    s.report(s.run_done, "reported_completed")
    views = {
        v["id"]: v
        for v in read(lambda c: continuity.execution_views(c, AGENT, now=far))["executions"]
    }
    assert views[s.run_done]["effective_state"] == "reported_completed"
    # The same holds through the Now view's running section.
    now = read(lambda c: continuity.now_view(c, s.env.store_dir, AGENT, now=far))
    running = {i["id"]: i for i in now["running"]["items"]}
    assert set(running) == {s.run_live}
    assert running[s.run_live]["state"] == "status_unknown"
    assert "never assumed failed or completed" in running[s.run_live]["reason"]
    # Through the service parameter: a report three hours old is silent after one hour.
    old = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = s.register("Old report")["results"][0]["id"]
    s.svc.execution_report(
        s.pid, AGENT, stale, state="reported_running", reported_at=old, request_id=new_request_id()
    )
    by_hours = {
        h: {
            e["id"]: e["effective_state"]
            for e in s.svc.execution_list(s.pid, AGENT, silence_after_hours=h)["executions"]
        }[stale]
        for h in (1, 24)
    }
    assert by_hours == {1: "status_unknown", 24: "reported_running"}
    assert refused(lambda: s.svc.execution_list(s.pid, AGENT, silence_after_hours=0)).code == (
        "validation_error"
    )


def test_reported_states_follow_the_rules(scenario: Scenario) -> None:
    s = scenario
    s.report(s.run_done, "reported_failed")
    assert refused(lambda: s.report(s.run_done, "reported_completed")).code == "validation_error"
    # A final run still gains a receipt (the completion report usually comes first).
    receipt = s.receipt("Late receipt")
    s.report(s.run_done, "reported_failed", [receipt["version_id"]])
    refs = s.body(s.run_done)["receipt_refs"]
    assert [r["id"] for r in refs] == [receipt["version_id"]]
    # Through apply the same rules hold, and receipt references only grow.
    current = s.svc.get(s.pid, OWNER, s.run_done, None)
    dropped = dict(current["body"], receipt_refs=[])
    revise = {
        "op": "revise",
        "id": s.run_done,
        "base_rev": current["rev"],
        "sensitivity": "normal",
        "occurred_at": None,
        "body": dropped,
    }
    assert refused(lambda: s.env.apply([revise])).code == "validation_error"
    # A run reported running is not registered again, and a final run cannot be re-reported
    # through apply either.
    live = s.svc.get(s.pid, OWNER, s.run_live, None)
    again = dict(live["body"], state="registered", last_reported_at=None)
    to_registered = dict(revise, id=s.run_live, base_rev=live["rev"], body=again)
    assert refused(lambda: s.env.apply([to_registered])).code == "validation_error"
    reopened = dict(current["body"], state="reported_running")
    to_running = dict(revise, base_rev=current["rev"], body=reopened)
    assert refused(lambda: s.env.apply([to_running])).code == "validation_error"
    assert (
        refused(
            lambda: s.report(
                "execution_" + "0" * 8 + "-0000-4000-8000-" + "0" * 12, "reported_running"
            )
        ).code
        == "not_found"
    )
    bad_alias = refused(
        lambda: s.svc.execution_register(
            s.pid, AGENT, title="x", host_label="h", reporter="r", working_root_alias="nowhere"
        )
    )
    assert bad_alias.code == "policy_denied"
    secret = refused(
        lambda: s.svc.execution_register(
            s.pid,
            AGENT,
            title="x",
            host_label="h",
            reporter="r",
            external_ref="https://user:pw@host/job?token=abc",
        )
    )
    assert secret.code == "validation_error"


def test_registering_and_reporting_start_nothing(
    scenario: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = scenario
    marker = tmp_path / "started.txt"
    command = f"python -c \"open(r'{marker}', 'w').write('ran')\""
    calls: list[str] = []

    def forbid(name: str) -> Any:
        def guard(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} must not be called by registration or reports")

        return guard

    monkeypatch.setattr(subprocess, "Popen", forbid("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", forbid("subprocess.run"))
    monkeypatch.setattr(os, "system", forbid("os.system"))
    monkeypatch.setattr(os, "startfile", forbid("os.startfile"), raising=False)
    registered = s.svc.execution_register(
        s.pid,
        AGENT,
        title="Never started",
        host_label="host",
        reporter="agent",
        command_summary=command,
        request_id=new_request_id(),
    )
    run = registered["results"][0]["id"]
    s.report(run, "reported_running")
    s.report(run, "reported_cancelled")
    s.svc.execution_list(s.pid, AGENT)
    s.svc.awaiting_list(s.pid, AGENT)
    s.svc.now(s.pid, AGENT)
    assert calls == [] and not marker.exists()
    assert s.body(run)["state"] == "reported_cancelled"
    assert s.body(run)["command_summary"] == command  # recorded, never executed


def test_register_and_report_replay_by_request_id(scenario: Scenario) -> None:
    s = scenario
    rid = new_request_id()
    first = s.register("Replayed", rid=rid)
    again = s.register("Replayed", rid=rid)
    assert again["replayed"] is True and again["results"] == first["results"]
    assert refused(lambda: s.register("A different run", rid=rid)).code == "request_key_conflict"
    run = first["results"][0]["id"]
    report_id = new_request_id()
    report = lambda: s.svc.execution_report(  # noqa: E731
        s.pid, AGENT, run, state="reported_running", reported_at="2026-10-06T09:00:00Z",
        request_id=report_id,
    )  # fmt: skip
    assert report()["project_seq"] == report()["project_seq"]


# ----- the queue's derivation -----------------------------------------------------------


def test_queue_leaves_only_on_a_citation_by_an_assessment_or_reviewed_entry(
    scenario: Scenario,
) -> None:
    s = scenario
    target = s.bare_receipt["version_id"]

    def awaiting() -> list[str]:
        return [i["target_id"] for i in s.svc.awaiting_list(s.pid, OWNER)["items"]]

    assert target in awaiting()
    cite = lambda entry_id, review, rev=1: [  # noqa: E731
        create("entry", entry_body("Looked at it", "A note.", review=review), entry_id),
        {
            "op": "relation_add",
            "source_ref": {"id": entry_id, "rev": 1},
            "target_ref": {"id": target, "rev": 1},
            "relation": "cites",
        },
    ]
    # An unreviewed entry (an agent's draft) citing it does not take it out.
    draft = new_id("entry")
    s.env.apply(cite(draft, "unreviewed"), caller=AGENT)
    assert target in awaiting()
    # A reviewed entry citing it is a protected effect: the agent gets approval_required.
    sneaky = new_id("entry")
    assert refused(lambda: s.env.apply(cite(sneaky, "reviewed"), caller=AGENT)).code == (
        "approval_required"
    )
    assert target in awaiting()
    # Marking the draft reviewed (it already cites the receipt) is protected too.
    current = s.svc.get(s.pid, OWNER, draft, None)
    mark = {
        "op": "revise",
        "id": draft,
        "base_rev": current["rev"],
        "sensitivity": "normal",
        "occurred_at": None,
        "body": dict(current["body"], review="reviewed"),
    }
    assert refused(lambda: s.env.apply([mark], caller=AGENT)).code == "approval_required"
    assert target in awaiting()
    # The owner's decision does take it out; archiving the entry brings it back (nothing is
    # silent in either direction).
    s.env.apply_protected([mark], caller=OWNER)
    assert target not in awaiting()
    archived = s.svc.get(s.pid, OWNER, draft, None)
    s.env.apply_protected(
        [{"op": "archive", "id": draft, "base_rev": archived["rev"]}],
        caller=OWNER,
    )
    assert target in awaiting()
    # A citation of the receipt's source (not its version) counts as well.
    by_source = new_id("entry")
    ops = cite(by_source, "reviewed")
    ops[1]["target_ref"] = {"id": s.bare_receipt["source_id"], "rev": 1}
    s.env.apply_protected(ops, caller=OWNER)
    assert target not in awaiting()


def test_resolving_requires_a_reason_and_an_awaiting_item(scenario: Scenario) -> None:
    s = scenario
    target = s.bare_receipt["version_id"]
    for blank in ("", "   "):
        assert (
            refused(
                lambda b=blank: s.svc.awaiting_resolve_request(  # type: ignore[misc]
                    s.pid, OWNER, target, "dismissed", b, new_request_id()
                )
            ).code
            == "validation_error"
        )
    assert (
        refused(
            lambda: s.svc.awaiting_resolve_request(
                s.pid, OWNER, s.run_live, "dismissed", "no", new_request_id()
            )
        ).code
        == "validation_error"
    )  # a running execution is not awaiting interpretation
    rid = new_request_id()
    first = resolve_as_owner(s, target, "interpreted", "Energy 1.80 kJ vs 1.90 kJ: worse.", rid)
    replay = s.svc.awaiting_resolve(
        s.pid, OWNER, target, "interpreted", "Energy 1.80 kJ vs 1.90 kJ: worse.", rid
    )
    assert replay["replayed"] is True and replay["results"] == first["results"]
    entry = s.body(first["results"][0]["id"])
    assert entry["category"] == "finding" and entry["origin"] == "typed"


# ----- artifact availability and backup coverage ----------------------------------------


def coverage_by_title(s: Scenario) -> dict[str, dict[str, Any]]:
    view = s.svc.artifact_coverage(s.pid, OWNER, limit=200)
    return {i["title"]: i for i in view["items"]}


def test_coverage_never_shows_reference_only_bytes_as_backed_up(scenario: Scenario) -> None:
    s = scenario
    items = coverage_by_title(s)
    reference = next(i for t, i in items.items() if "notes" in t)
    assert reference["storage"] == "external" and reference["availability"] == "reference_only"
    assert reference["backup"]["covered"] is False
    assert "never backed up" in reference["backup"]["reason"]
    retained = [i for i in items.values() if i["storage"] != "external"]
    assert retained and all(i["availability"] == "retained" for i in retained)
    assert all(not i["backup"]["covered"] for i in retained)  # no backup recorded yet

    # A manual backup is verified and inventoried; retained bytes become covered, the
    # reference-only file stays uncovered although its origin still exists.
    made = s.svc.backup(s.pid, tmp_backup(s), FULL)
    items = coverage_by_title(s)
    view = s.svc.artifact_coverage(s.pid, OWNER, limit=200)
    assert view["verified_backups"] == 1
    assert all(i["backup"]["covered"] for i in items.values() if i["storage"] != "external")
    reference = next(i for t, i in items.items() if "notes" in t)
    assert (
        reference["backup"]["covered"] is False and reference["backup"]["status"] == "not_backed_up"
    )
    assert view["totals"]["reference_only"] == 1
    assert Path(made["backup"]).name in json.dumps(
        next(i for i in items.values() if i["storage"] != "external")["backup"]["backups"]
    )

    # Content added after the backup is not covered by it.
    s.env.import_files(["artifact.bin"], retention="copy")
    fresh = [i for i in s.svc.artifact_coverage(s.pid, OWNER, limit=200)["items"]][-1]
    assert fresh["storage"] != "external" and fresh["backup"]["covered"] is False
    assert "predate" in fresh["backup"]["reason"]

    # Removing the reference-only file: unavailable and not backed up.
    (s.env.research / "sub" / "notes.txt").unlink()
    gone = next(
        i for i in s.svc.artifact_coverage(s.pid, OWNER, limit=200)["items"]
        if i["version_id"] == s.reference["version_id"]
    )  # fmt: skip
    assert gone["availability"] == "unavailable" and gone["backup"]["covered"] is False
    assert "gone" in gone["availability_reason"]
    # The agent sees the same, and a deleted backup archive covers nothing.
    Path(made["backup"]).unlink()
    after = coverage_by_title(s)
    assert all(not i["backup"]["covered"] for i in after.values())
    assert s.svc.artifact_coverage(s.pid, AGENT)["verified_backups"] == 0


def tmp_backup(s: Scenario) -> Path:
    return s.env.tmp / f"backup-{new_request_id()}.zip"


def test_coverage_counts_only_verified_present_backups_and_pages(scenario: Scenario) -> None:
    s = scenario
    # The archive exists on disk, but its verification never succeeded: it covers nothing.
    archive = s.env.tmp / "unverified.zip"
    archive.write_bytes(b"not a verified backup")
    store = s.svc.open(s.pid)
    conn = store.connect()
    try:
        # An unverified inventory row never covers anything.
        conn.execute(
            "INSERT INTO backup_inventory (backup_sha256, manifest_sha256, location, label, "
            "purpose, project_seq, schema_version, software_version, created_at, database_sha256, "
            "media_count, media_bytes, reference_only_count, verification, verified_at, "
            "last_checked_at, present, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "a" * 64, "b" * 64, str(archive), "unverified.zip", "manual",
                10_000, 8, "0", "2026-10-01T00:00:00Z", "c" * 64, 0, 0, 0, "unverified",
                None, None, None, "2026-10-01T00:00:00Z",
            ),
        )  # fmt: skip
        conn.commit()
    finally:
        conn.close()
    assert all(not i["backup"]["covered"] for i in coverage_by_title(s).values())
    # Paging walks every version exactly once, also across versions one commit created.
    everything = [
        i["version_id"] for i in s.svc.artifact_coverage(s.pid, OWNER, limit=200)["items"]
    ]
    assert len(everything) >= 5 and len(set(everything)) == len(everything)
    seen: list[str] = []
    cursor = None
    for _ in range(40):  # bounded: a paging bug must fail, not hang
        page = s.svc.artifact_coverage(s.pid, OWNER, limit=2, cursor=cursor)
        assert len(page["items"]) <= 2
        seen += [i["version_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    else:
        raise AssertionError("paging never ended")
    assert seen == everything
    assert refused(lambda: s.svc.artifact_coverage(s.pid, OWNER, cursor="x")).code == (
        "validation_error"
    )
    only = s.svc.artifact_coverage(s.pid, OWNER, source_id=s.reference["source_id"])
    assert [i["version_id"] for i in only["items"]] == [s.reference["version_id"]]


# ----- claims, assumptions and links ----------------------------------------------------


def test_claims_surface_objective_and_assumption_links(scenario: Scenario) -> None:
    s = scenario
    claim = s.svc.get(s.pid, AGENT, s.claim_a, None)
    assert claim["body"]["scope"]["objective"] == "Replace the baseline controller?"
    assert s.svc.get(s.pid, AGENT, s.claim_b, None)["body"]["scope"]["objective"] is None
    links = s.svc.links(s.pid, AGENT, s.claim_a)
    assert [a["id"] for a in links["relies_on"]] == [s.drag]
    assert links["relies_on"][0]["state"] == "active" and links["relies_on"][0]["stale"] is False
    shared = s.svc.links(s.pid, AGENT, s.drag)
    assert [d["id"] for d in shared["relied_on_by"]] == [s.claim_a]
    context = s.svc.context(s.pid, AGENT)["text"]
    assert "objective=Replace the baseline controller?" in context
    assert "relies on assumption(s): 'Constant drag'" in context
    assert "ASSUMPTION Constant drag" in context
    # After the owner supersedes the assumption, every surface says so.
    apply_op, _ = s.supersede_correction()
    s.env.apply_protected([apply_op], caller=OWNER)
    links = s.svc.links(s.pid, AGENT, s.claim_a)
    assert links["relies_on"][0]["state"] == "superseded" and links["relies_on"][0]["stale"]
    context = s.svc.context(s.pid, AGENT)["text"]
    assert "SUPERSEDED by 'Speed-dependent drag'" in context
    assert "HISTORICAL (superseded assumption, do not rely on it): Constant drag" in context
    assert "NEEDS REVIEW" in context


def test_revising_an_assumption_flags_what_relied_on_the_earlier_revision(
    scenario: Scenario,
) -> None:
    s = scenario
    current = s.svc.get(s.pid, OWNER, s.drag, None)
    revise = {
        "op": "revise",
        "id": s.drag,
        "base_rev": current["rev"],
        "sensitivity": "normal",
        "occurred_at": None,
        "body": dict(current["body"], text="Drag is constant below 40 m/s only."),
    }
    result = s.env.apply([revise], caller=AGENT)
    flagged = {row["record_id"]: row for row in s.flags()}
    assert {s.claim_a, s.action, s.anchor, s.anchor_claim} <= set(flagged)
    assert {row["created_seq"] for row in flagged.values()} == {result["project_seq"]}
    assert all(row["cause_id"] == s.drag for row in flagged.values())
    assert s.body(s.anchor)["status"] == "needs_review"
    assert "revised" in s.body(s.anchor)["reason"]
    # A dependent recorded against the new revision is not stale: nothing new is flagged.
    extra = new_id("claim")
    from cp7_support import SCOPE_SIMPLIFIED, claim_body

    s.env.apply(
        [
            create(
                "claim", claim_body("Built on the revised assumption.", SCOPE_SIMPLIFIED, []), extra
            ),
            {
                "op": "relation_add",
                "source_ref": {"id": extra, "rev": 1},
                "target_ref": {"id": s.drag, "rev": 2},
                "relation": "depends_on",
            },
        ]
    )
    again = dict(current["body"], text="Drag is constant below 40 m/s only (clarified).")
    revise_again = dict(revise, base_rev=2, body=again)
    s.env.apply([revise_again], caller=AGENT)
    flagged_after = {row["record_id"] for row in s.flags()}
    assert extra in flagged_after  # relied on revision 2, which the new revision 3 replaced


def test_transitive_flagging_stops_at_history_and_containment(scenario: Scenario) -> None:
    s = scenario
    # A second action that depends on the flagged action is flagged transitively; the
    # meeting-style history (an assessment of the claim) and the paper are not carried further.
    follower = new_id("action")
    s.env.apply(
        [
            create(
                "action",
                {
                    "title": "Write up after the rerun",
                    "reason": "Follows the rerun.",
                    "source_refs": [],
                    "owner_ref": None,
                    "due_at": None,
                    "dependencies": [s.action],
                    "status": "proposed",
                    "blocker": None,
                    "completion_evidence": [],
                    "commitment_item_id": None,
                },
                follower,
            )
        ]
    )
    note = new_id("entry")
    mention = entry_body(
        "Note on A", "Mentions the finding.", claim_refs=[{"id": s.claim_a, "rev": 1}]
    )
    s.env.apply([create("entry", mention, note)])
    assessment = new_id("assessment")
    s.env.apply_protected(
        [
            create(
                "assessment",
                {
                    "claim_ref": {"id": s.claim_a, "rev": 1},
                    "outcome": "supports",
                    "rationale": "History.",
                    "checks": [],
                    "author_ref": None,
                    "method": "researcher",
                    "scope_match": True,
                },
                assessment,
            )
        ],
        caller=OWNER,
    )
    apply_op, _ = s.supersede_correction()
    s.env.apply_protected([apply_op], caller=OWNER)
    flagged = {row["record_id"]: row["reason"] for row in s.flags()}
    assert follower in flagged and s.action in flagged[follower]
    assert note in flagged and s.claim_a in flagged[note]
    # Direct dependents keep the pre-CP10.4 behaviour (the assessment of the claim is not a
    # dependent of the assumption, and a hop never carries a flag onto history or containment).
    assert assessment not in flagged and s.paper_id not in flagged
    assert s.claim_b not in flagged and s.anchor_control not in flagged


# ----- dogfood F5: receipt citations read as the artifacts they name ----------------------


def test_receipt_citations_resolve_to_artifact_titles_in_the_notebook(scenario: Scenario) -> None:
    s = scenario
    receipt = s.bare_receipt

    def ev(pointer: str) -> dict[str, Any]:
        return {
            "source_id": receipt["source_id"],
            "version_id": receipt["version_id"],
            "locator": {"type": "json", "pointer": pointer},
            "quote_sha256": None,
        }

    entry = new_id("entry")
    s.env.apply(
        [
            create(
                "entry",
                entry_body(
                    "Run notes",
                    "What the run produced.",
                    evidence=[ev("/outputs/0"), ev("/exit_status"), ev("")],
                ),
                entry,
            )
        ]
    )
    view = s.svc.notebook(s.pid, AGENT)
    sources = next(e for e in view["entries"] if e["id"] == entry)["sources"]
    by_pointer = {src["locator"]["pointer"]: src for src in sources}
    cited = by_pointer["/outputs/0"]
    assert cited["locator_label"] == "output result: plant-result.md"
    assert cited["artifact"]["version_id"] == s.plant["version_id"]
    assert cited["artifact"]["role"] == "result" and cited["artifact"]["direction"] == "output"
    # Pointers that name no artifact keep the raw locator and get no label.
    assert by_pointer["/exit_status"]["locator_label"] is None
    assert by_pointer[""]["artifact"] is None
    # The markdown export reads the label, not the pointer.
    from prometheos.app import views

    markdown = views.notebook_markdown(view)
    assert "output result: plant-result.md" in markdown and "/outputs/0" not in markdown
    assert "'pointer': '/exit_status'" in markdown or "/exit_status" in markdown


def test_a_receipt_citation_never_names_an_artifact_the_caller_cannot_see(
    scenario: Scenario,
) -> None:
    s = scenario
    secret = s.env.import_files(["artifact.bin"], sensitivity="restricted")["plan"]["items"][0]
    commit = s.svc.receipt_record(
        s.pid,
        raw=json.dumps(
            {
                "schema_version": 1,
                "observation": "reported",
                "reporter": "owner",
                "command": "run",
                "working_root": "research",
                "environment": {"os": "TestOS", "tools": []},
                "started_at": None,
                "ended_at": None,
                "exit_status": 0,
                "outcome": "succeeded",
                "inputs": [],
                "outputs": [
                    {
                        "source_id": secret["source_id"],
                        "version_id": secret["version_id"],
                        "sha256": secret["sha256"],
                        "role": "figure",
                    }
                ],
                "limitations": [],
            }
        ).encode(),
        title="Owner-only receipt",
        description="",
        request_id=new_request_id(),
        caller=OWNER,
    )
    source = next(r["id"] for r in commit["results"] if r["id"].startswith("source_"))
    version = next(r["id"] for r in commit["results"] if r["id"].startswith("version_"))
    entry = new_id("entry")
    ref = {
        "source_id": source,
        "version_id": version,
        "locator": {"type": "json", "pointer": "/outputs/0"},
        "quote_sha256": None,
    }
    s.env.apply(
        [
            create(
                "entry",
                entry_body("Owner note", "Cites a restricted figure.", evidence=[ref]),
                entry,
                sensitivity="restricted",
            )
        ],
        caller=OWNER,
    )
    owner_view = s.svc.notebook(s.pid, OWNER)
    owner_entry = next(e for e in owner_view["entries"] if e["id"] == entry)
    assert owner_entry["sources"][0]["locator_label"] == "output figure: artifact.bin"
    # The receipt, the entry and the artifact are all restricted: an agent sees none of it.
    agent_view = s.svc.notebook(s.pid, AGENT)
    assert entry not in [e["id"] for e in agent_view["entries"]]
    assert "artifact.bin" not in json.dumps(agent_view)


def test_restricted_executions_are_invisible_to_agent_views(scenario: Scenario) -> None:
    s = scenario
    private = s.svc.execution_register(
        s.pid,
        OWNER,
        title="Owner-only sweep",
        host_label="private host",
        reporter="owner",
        sensitivity="restricted",
        request_id=new_request_id(),
    )["results"][0]["id"]
    s.report(private, "reported_completed", caller=OWNER)
    owner_queue = [i["target_id"] for i in s.svc.awaiting_list(s.pid, OWNER)["items"]]
    assert private in owner_queue
    agent_queue = s.svc.awaiting_list(s.pid, AGENT)
    assert private not in [i["target_id"] for i in agent_queue["items"]]
    assert "Owner-only" not in json.dumps(agent_queue)
    assert private not in [e["id"] for e in s.svc.execution_list(s.pid, AGENT)["executions"]]
    assert "Owner-only" not in json.dumps(s.svc.now(s.pid, AGENT))
    assert private in [i["id"] for i in s.svc.now(s.pid, OWNER)["awaiting_interpretation"]["items"]]
    # The agent can neither report on it nor resolve it: it reads as absent.
    assert refused(lambda: s.report(private, "reported_running")).code == "not_found"
    assert (
        refused(
            lambda: s.svc.awaiting_resolve_request(
                s.pid, AGENT, private, "dismissed", "no", new_request_id()
            )
        ).code
        == "validation_error"
    )


def test_evidence_citing_a_receipt_is_a_citation_and_a_reviewed_one_is_protected(
    scenario: Scenario,
) -> None:
    s = scenario
    receipt = s.bare_receipt
    evidence = {
        "source_id": receipt["source_id"],
        "version_id": receipt["version_id"],
        "locator": {"type": "json", "pointer": "/outcome"},
        "quote_sha256": None,
    }

    def queued() -> bool:
        return receipt["version_id"] in [
            i["target_id"] for i in s.svc.awaiting_list(s.pid, OWNER)["items"]
        ]

    unreviewed, reviewed = new_id("entry"), new_id("entry")
    s.env.apply(
        [
            create(
                "entry", entry_body("Draft", "Quotes the receipt.", evidence=[evidence]), unreviewed
            )
        ],
        caller=AGENT,
    )
    assert queued()  # an unreviewed draft is no interpretation
    sneaky = entry_body("Reviewed", "Quotes the receipt.", review="reviewed", evidence=[evidence])
    assert (
        refused(lambda: s.env.apply([create("entry", sneaky, reviewed)], caller=AGENT)).code
        == "approval_required"
    )
    assert queued()
    s.env.apply_protected([create("entry", sneaky, reviewed)], caller=OWNER)
    assert not queued()  # a reviewed entry that cites it through evidence takes it out


# ----- round 2 (review precheck-cp10-4-a) ------------------------------------------------


def awaiting_ids(s: Scenario, caller: Any = OWNER) -> list[str]:
    return [i["target_id"] for i in s.svc.awaiting_list(s.pid, caller)["items"]]


def receipt_ev(receipt: dict[str, Any], pointer: str) -> dict[str, Any]:
    return {
        "source_id": receipt["source_id"],
        "version_id": receipt["version_id"],
        "locator": {"type": "json", "pointer": pointer},
        "quote_sha256": None,
    }


def reviewed_entry_citing(s: Scenario, how: str) -> tuple[dict[str, Any], str]:
    """A reviewed entry citing a result in one of the ways the reference index can see; the
    result it cites is returned too. Shapes: text, title, quantity, evidence, source_text."""
    receipt = s.bare_receipt
    target = receipt["version_id"]
    body = entry_body("Reviewed note", "A plain note.", review="reviewed")
    if how == "text":
        body["text"] = target
    elif how == "title":
        body["title"] = target
    elif how == "source_text":
        body["text"] = receipt["source_id"]
    elif how == "evidence":
        body["evidence"] = [receipt_ev(receipt, "/outcome")]
    elif how == "quantity":
        ev = receipt_ev(receipt, "/environment/tools/0/version")
        body["quantities"] = [
            {
                "decimal": "1.0",
                "unit": "",
                "source_ref": ev,
                "source_pointer": "json:/environment/tools/0/version",
            }
        ]
    return create("entry", body, new_id("entry")), target


@pytest.mark.parametrize("how", ["text", "title", "source_text", "evidence", "quantity"])
def test_every_way_a_reviewed_entry_cites_a_result_is_protected_and_clears_the_queue(
    scenario: Scenario, how: str
) -> None:
    """F1: the queue and the protected effect use one definition of "cites a result"."""
    s = scenario
    op, target = reviewed_entry_citing(s, how)
    assert target in awaiting_ids(s)
    preview = s.svc.review_request(s.pid, s.raw_request([op]), AGENT)
    assert any("execution result" in effect for effect in preview["protected_effects"]), how
    assert refused(lambda: s.env.apply([op], caller=AGENT)).code == "approval_required", how
    assert target in awaiting_ids(s), how
    s.env.apply_protected([op], caller=OWNER)
    assert target not in awaiting_ids(s), how  # the same citation does clear it for the owner


def test_an_execution_id_in_a_title_is_protected_too(scenario: Scenario) -> None:
    s = scenario
    receipt = s.receipt("Sweep receipt")
    s.report(s.run_done, "reported_completed", [receipt["version_id"]])
    body = entry_body(s.run_done, "ok", review="reviewed")
    op = create("entry", body, new_id("entry"))
    assert refused(lambda: s.env.apply([op], caller=AGENT)).code == "approval_required"
    assert s.run_done in awaiting_ids(s)
    s.env.apply_protected([op], caller=OWNER)
    assert s.run_done not in awaiting_ids(s)


def test_a_reviewed_entry_that_mentions_no_result_is_neither_protected_nor_a_citation(
    scenario: Scenario,
) -> None:
    s = scenario
    plain = create(
        "entry", entry_body("Plain", "Nothing cited.", review="reviewed"), new_id("entry")
    )
    s.env.apply([plain], caller=AGENT)  # no approval needed
    mentions_plant = create(
        "entry", entry_body("Plant", s.plant["version_id"], review="reviewed"), new_id("entry")
    )  # a non-result ID in the text
    s.env.apply([mentions_plant], caller=AGENT)
    assert s.bare_receipt["version_id"] in awaiting_ids(s)


@pytest.mark.parametrize("how", ["text", "evidence"])
def test_revising_an_owners_reviewed_entry_to_cite_another_result_is_protected(
    scenario: Scenario, how: str
) -> None:
    s = scenario
    done = resolve_as_owner(s, s.bare_receipt["version_id"], "interpreted", "Owner read X.")
    entry_id = next(r["id"] for r in done["results"] if r["id"].startswith("entry_"))
    other = s.receipt("Second bare receipt")
    assert other["version_id"] in awaiting_ids(s)
    current = s.svc.get(s.pid, AGENT, entry_id, None)
    body = dict(current["body"])
    if how == "text":
        body["text"] = other["version_id"]
    else:
        body["evidence"] = [receipt_ev(other, "/outcome")]
    revise = {
        "op": "revise",
        "id": entry_id,
        "base_rev": current["rev"],
        "sensitivity": current["sensitivity"],
        "occurred_at": None,
        "body": body,
    }
    assert refused(lambda: s.env.apply([revise], caller=AGENT)).code == "approval_required"
    assert other["version_id"] in awaiting_ids(s)
    # Revising without citing anything new stays an ordinary edit.
    edit = dict(revise, body=dict(current["body"], text="Owner read X, clarified."))
    s.env.apply([edit], caller=AGENT)
    current = s.svc.get(s.pid, AGENT, entry_id, None)
    revise = dict(revise, base_rev=current["rev"], body=dict(body, text=body["text"]))
    s.env.apply_protected([revise], caller=OWNER)
    assert other["version_id"] not in awaiting_ids(s)


def test_finding_entries_that_rely_on_an_assumption_carry_the_flag_to_actions_and_anchors(
    scenario: Scenario,
) -> None:
    """F2: an entry (not a claim) relies on the assumption; an action works from it and an
    anchor uses it."""
    s = scenario
    finding, action = new_id("entry"), new_id("action")
    s.env.apply(
        [
            create(
                "entry",
                entry_body("Finding E", "An entry finding.", category="finding", review="reviewed"),
                finding,
            )
        ],
        caller=OWNER,
    )
    anchor = s.paper.anchor(s.paper_id, 4)
    s.env.apply(
        [
            {
                "op": "relation_add",
                "source_ref": {"id": finding, "rev": 1},
                "target_ref": {"id": s.drag, "rev": 1},
                "relation": "depends_on",
            },
            {
                "op": "relation_add",
                "source_ref": {"id": finding, "rev": 1},
                "target_ref": {"id": anchor, "rev": 1},
                "relation": "used_in",
            },
            create(
                "action",
                {
                    "title": "Act on finding E",
                    "reason": "E.",
                    "source_refs": [{"id": finding, "rev": 1}],
                    "owner_ref": None,
                    "due_at": None,
                    "dependencies": [],
                    "status": "proposed",
                    "blocker": None,
                    "completion_evidence": [],
                    "commitment_item_id": None,
                },
                action,
            ),
        ],
        caller=OWNER,
    )
    apply_op, _ = s.supersede_correction()
    s.env.apply_protected([apply_op], caller=OWNER)
    flagged = {row["record_id"]: row["reason"] for row in s.flags()}
    assert {finding, action} <= set(flagged) and s.drag_v2 not in flagged
    assert finding in flagged[action]
    assert s.body(anchor)["status"] == "needs_review"
    assert s.body(s.anchor_control)["status"] == "current"  # history and containment stop


def test_an_agent_assessment_clears_the_queue_and_the_owner_is_shown_it(
    scenario: Scenario,
) -> None:
    """F3: spec/05 literally. An assessment (agent or owner) clears the item; Now lists who
    interpreted it so the owner can confirm, and a reviewed entry ends the notice."""
    s = scenario
    target = s.run_done
    receipt = s.receipt("Sweep receipt")
    s.report(target, "reported_completed", [receipt["version_id"]])
    assert target in awaiting_ids(s)
    assessment = new_id("assessment")
    s.env.apply(
        [
            create(
                "assessment",
                {
                    "claim_ref": {"id": s.claim_b, "rev": 1},
                    "outcome": "inconclusive",
                    "rationale": "Agent quick reading.",
                    "checks": [],
                    "author_ref": None,
                    "method": "reported_agent",
                    "scope_match": True,
                },
                assessment,
            ),
            {
                "op": "relation_add",
                "source_ref": {"id": assessment, "rev": 1},
                "target_ref": {"id": target, "rev": 3},
                "relation": "cites",
            },
        ],
        caller=AGENT,
    )
    assert target not in awaiting_ids(s)
    view = s.svc.now(s.pid, OWNER)
    notices = [i for i in view["changed"]["items"] if i["id"] == target]
    assert len(notices) == 1
    reason = notices[0]["reason"]
    assert "Recorded as an agent-reported assessment from test" in reason
    assert reason.startswith("Checked without your review: the assessment ")
    assert notices[0]["checked_by_agent"] is True and len(notices[0]["outcomes"]) == 1
    assert notices[0]["cause_id"] == assessment
    # The section counts agent-checked items over the whole list, before truncation, on the
    # service and through MCP alike (decision 0078; review ux-s1-s2-a R2-1).
    checked = sum(1 for i in view["changed"]["items"] if i.get("checked_by_agent"))
    assert view["changed"]["checked_by_agent"] == checked >= 1
    first = s.svc.now(s.pid, OWNER, limit=1)["changed"]
    assert len(first["items"]) == 1 and first["checked_by_agent"] == checked
    assert mcp_now(s)["changed"]["checked_by_agent"] == checked
    # The agent sees the same notice through its own Now (it is a fact about the project).
    assert [i["id"] for i in s.svc.now(s.pid, AGENT)["changed"]["items"]].count(target) == 1
    # The owner confirms with a reviewed interpretation; the notice ends.
    resolve_as_owner(s, target, "interpreted", "Confirmed: inconclusive is fair.")
    after = s.svc.now(s.pid, OWNER)
    assert target not in [i["id"] for i in after["changed"]["items"]]
    assert target not in awaiting_ids(s)


def test_a_researcher_assessment_leaves_no_agent_notice(scenario: Scenario) -> None:
    s = scenario
    assessment = new_id("assessment")
    s.env.apply_protected(
        [
            create(
                "assessment",
                {
                    "claim_ref": {"id": s.claim_b, "rev": 1},
                    "outcome": "challenges",
                    "rationale": "Owner reading.",
                    "checks": [],
                    "author_ref": None,
                    "method": "researcher",
                    "scope_match": True,
                },
                assessment,
            ),
            {
                "op": "relation_add",
                "source_ref": {"id": assessment, "rev": 1},
                "target_ref": {"id": s.bare_receipt["version_id"], "rev": 1},
                "relation": "cites",
            },
        ],
        caller=OWNER,
    )
    assert s.bare_receipt["version_id"] not in awaiting_ids(s)
    changed = s.svc.now(s.pid, OWNER)["changed"]["items"]
    assert s.bare_receipt["version_id"] not in [i["id"] for i in changed]


def test_a_report_cannot_be_dated_in_the_future(scenario: Scenario) -> None:
    """F4: a far-future reported_at would keep a run from ever reading as silent."""
    s = scenario

    def report_at(moment: datetime) -> Any:
        return lambda: s.svc.execution_report(
            s.pid,
            AGENT,
            s.run_live,
            state="reported_running",
            reported_at=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            request_id=new_request_id(),
        )

    assert refused(report_at(datetime(2999, 1, 1, tzinfo=UTC))).code == "validation_error"
    assert refused(report_at(datetime.now(UTC) + timedelta(hours=1))).code == "validation_error"
    report_at(datetime.now(UTC) + timedelta(minutes=2))()  # within the clock skew
    report_at(datetime.now(UTC) - timedelta(days=2))()  # the past is fine


def operation_run(state: str, rank: int) -> Any:
    """A valid ``operation_runs`` row in ``state`` built from the contract examples."""
    from prometheos.domain.ids import new_operational_id
    from prometheos.store import operational

    examples = Path(__file__).resolve().parents[1] / "contracts" / "examples" / "operation_run"
    base = {
        "completed": "valid-completed.json",
        "waiting_for_input": "valid-waiting-for-approval.json",
        "unknown_external": "valid-unknown-external.json",
    }.get(state, "valid-running.json")
    data = json.loads((examples / base).read_text(encoding="utf-8"))
    data.update(
        id=new_operational_id("oprun"),
        state=state,
        caller_rank=rank,
        start_request_id=None,
        start_request_sha256=None,
    )
    return operational.OperationRun.model_validate(data)


def test_now_shows_open_operation_runs_from_the_state_model_at_the_callers_rank(
    scenario: Scenario,
) -> None:
    """F6 and R8: the open states come from the shared model, the rank filter holds."""
    from prometheos.store import operational

    s = scenario
    assert set(continuity.RUN_OPEN_STATES) == set(operational.RUN_STATES) - set(
        operational.TERMINAL_RUN_STATES
    )
    conn = s.svc.open(s.pid).connect()
    try:
        runs = {state: operation_run(state, 0) for state in continuity.RUN_OPEN_STATES}
        runs["completed"] = operation_run("completed", 0)
        private = operation_run("running", 2)
        for run in (*runs.values(), private):
            operational.insert_operation_run(conn, run)
        conn.commit()
    finally:
        conn.close()
    shown = {i["id"]: i for i in s.svc.now(s.pid, AGENT)["running"]["items"]}
    for state in continuity.RUN_OPEN_STATES:
        assert shown[runs[state].id]["kind"] == "operation_run", state
        assert shown[runs[state].id]["state"] == state
    assert runs["completed"].id not in shown  # terminal states never read as running
    assert private.id not in shown  # a run started at a higher rank is not the agent's to see
    owner = {i["id"] for i in s.svc.now(s.pid, OWNER)["running"]["items"]}
    assert private.id in owner


def test_media_versions_are_covered_only_by_a_backup_that_holds_their_bytes(
    scenario: Scenario,
) -> None:
    """R5: a version stored as media is covered by a backup listing its digest, not by any."""
    s = scenario
    first = s.svc.backup(s.pid, tmp_backup(s), FULL)  # taken before the large file exists
    big = s.env.research / "large.bin"
    big.write_bytes(os.urandom(8 * 1024 * 1024 + 1))  # incompressible: the backup ratio guard
    imported = s.env.import_files(["large.bin"])["plan"]["items"][0]

    def big_item() -> dict[str, Any]:
        view = s.svc.artifact_coverage(s.pid, OWNER, source_id=imported["source_id"])
        return view["items"][0]

    item = big_item()
    assert item["storage"] == "media" and item["availability"] == "retained"
    assert item["backup"]["covered"] is False  # the first backup is verified but lacks it
    second = s.svc.backup(s.pid, tmp_backup(s), FULL)
    item = big_item()
    assert item["backup"]["covered"] is True
    labels = [b["label"] for b in item["backup"]["backups"]]
    assert labels == [Path(second["backup"]).name] and Path(first["backup"]).name not in labels


def test_a_dependent_recorded_after_a_revision_is_not_flagged_by_it(scenario: Scenario) -> None:
    """R11: dependents are bound to a revision; one bound to the new revision is current."""
    from cp7_support import SCOPE_SIMPLIFIED, claim_body

    s = scenario
    current = s.svc.get(s.pid, OWNER, s.drag, None)
    s.env.apply(
        [
            {
                "op": "revise",
                "id": s.drag,
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": dict(current["body"], text="Drag is constant below 40 m/s."),
            }
        ]
    )
    later = new_id("claim")
    s.env.apply(
        [
            create("claim", claim_body("Built on the revision.", SCOPE_SIMPLIFIED, []), later),
            {
                "op": "relation_add",
                "source_ref": {"id": later, "rev": 1},
                "target_ref": {"id": s.drag, "rev": 2},
                "relation": "depends_on",
            },
        ]
    )
    assert later not in {row["record_id"] for row in s.flags()}
    links = s.svc.links(s.pid, AGENT, later)
    assert links["relies_on"][0]["stale"] is False  # bound to the current revision
    assert s.svc.links(s.pid, AGENT, s.claim_a)["relies_on"][0]["stale"] is True


def test_coverage_totals_cover_every_version_and_paging_can_start_from_the_newest(
    scenario: Scenario,
) -> None:
    """F7: totals are the project's, the order can be newest first, the cursor follows it."""
    s = scenario
    everything = s.svc.artifact_coverage(s.pid, OWNER, limit=200)
    oldest_first = [i["version_id"] for i in everything["items"]]
    page = s.svc.artifact_coverage(s.pid, OWNER, limit=2, newest_first=True)
    assert page["totals"] == everything["totals"]
    assert page["totals"]["versions"] == len(oldest_first) > 2
    seen: list[str] = []
    cursor = None
    for _ in range(40):  # bounded: a paging bug must fail, not hang
        part = s.svc.artifact_coverage(s.pid, OWNER, limit=2, newest_first=True, cursor=cursor)
        seen += [i["version_id"] for i in part["items"]]
        cursor = part["next_cursor"]
        if cursor is None:
            break
    else:
        raise AssertionError("paging never ended")
    assert seen == list(reversed(oldest_first))


def test_the_replacement_assumption_is_never_flagged_even_if_it_builds_on_the_old_one(
    scenario: Scenario,
) -> None:
    """R2: the replacement may itself depend on what it replaces; it is not a dependent."""
    s = scenario
    s.env.apply(
        [
            {
                "op": "relation_add",
                "source_ref": {"id": s.drag_v2, "rev": 1},
                "target_ref": {"id": s.drag, "rev": 1},
                "relation": "depends_on",
            }
        ]
    )
    apply_op, _ = s.supersede_correction()
    s.env.apply_protected([apply_op], caller=OWNER)
    assert s.drag_v2 not in {row["record_id"] for row in s.flags()}


def test_coverage_never_lists_a_version_above_the_callers_rank(scenario: Scenario) -> None:
    """R12."""
    s = scenario
    secret = s.env.import_files(["artifact.bin"], sensitivity="restricted")["plan"]["items"][0]
    owner = {i["version_id"] for i in s.svc.artifact_coverage(s.pid, OWNER, limit=200)["items"]}
    agent_view = s.svc.artifact_coverage(s.pid, AGENT, limit=200)
    agent = {i["version_id"] for i in agent_view["items"]}
    assert secret["version_id"] in owner and secret["version_id"] not in agent
    assert agent_view["totals"]["versions"] == len(agent)  # totals do not count what it cannot see


# ----- round 3 (review precheck-cp10-4-a-r2) ---------------------------------------------


def researcher_assessment(s: Scenario, cites: str | None = None) -> str:
    """The owner's researcher-method assessment (a protected create); optionally citing a result
    in the same granted commit."""
    assessment = new_id("assessment")
    ops: list[dict[str, Any]] = [
        create(
            "assessment",
            {
                "claim_ref": {"id": s.claim_b, "rev": 1},
                "outcome": "challenges",
                "rationale": "Owner reading.",
                "checks": [],
                "author_ref": None,
                "method": "researcher",
                "scope_match": True,
            },
            assessment,
        )
    ]
    if cites is not None:
        ops.append(
            {
                "op": "relation_add",
                "source_ref": {"id": assessment, "rev": 1},
                "target_ref": {"id": cites, "rev": 1},
                "relation": "cites",
            }
        )
    s.env.apply_protected(ops, caller=OWNER)
    return assessment


def cites_op(source: str, target: str) -> dict[str, Any]:
    return {
        "op": "relation_add",
        "source_ref": {"id": source, "rev": 1},
        "target_ref": {"id": target, "rev": 1},
        "relation": "cites",
    }


def test_an_agent_cannot_cite_a_result_from_the_owners_researcher_assessment(
    scenario: Scenario,
) -> None:
    """R2-F1 (a): the researcher's assessment cannot be made to interpret a result by a link an
    agent adds; that is the owner's decision, and the item stays in the queue meanwhile."""
    s = scenario
    already = s.receipt("Already interpreted")
    assessment = researcher_assessment(s, already["version_id"])
    target = s.bare_receipt["version_id"]
    assert target in awaiting_ids(s)
    assert refused(lambda: s.env.apply([cites_op(assessment, target)], caller=AGENT)).code == (
        "approval_required"
    )
    assert target in awaiting_ids(s)
    # The owner can make the same link: the item leaves, and no agent notice is raised for it.
    s.env.apply_protected([cites_op(assessment, target)], caller=OWNER)
    assert target not in awaiting_ids(s)
    changed = [i["id"] for i in s.svc.now(s.pid, OWNER)["changed"]["items"]]
    assert target not in changed and already["version_id"] not in changed


def test_the_agent_notice_is_judged_by_who_added_the_link_not_by_the_method(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-F1 (b): with the protection of (a) out of the picture, a citation an agent added to
    the researcher's own assessment still shows the owner a notice naming who added it."""
    from prometheos.app import apply as apply_mod

    s = scenario
    assessment = researcher_assessment(s)
    target = s.bare_receipt["version_id"]
    monkeypatch.setattr(apply_mod, "is_result", lambda txn, record_id: False)
    s.env.apply([cites_op(assessment, target)], caller=AGENT)
    assert target not in awaiting_ids(s)
    notices = [i for i in s.svc.now(s.pid, OWNER)["changed"]["items"] if i["id"] == target]
    assert len(notices) == 1
    assert "Recorded as a researcher-method assessment from test" in notices[0]["reason"]
    assert notices[0]["cause_id"] == assessment
    # An owner-granted link to the same assessment would not have raised it (see the test above).


def test_a_hidden_assessment_never_shows_in_the_agents_notice(scenario: Scenario) -> None:
    """R2-F2: the agent sees that the item was interpreted, never the restricted assessment."""
    s = scenario
    target = s.bare_receipt["version_id"]
    assessment = new_id("assessment")
    s.env.apply(
        [
            create(
                "assessment",
                {
                    "claim_ref": {"id": s.claim_b, "rev": 1},
                    "outcome": "challenges",
                    "rationale": "ZQXSECRET reading",
                    "checks": [],
                    "author_ref": None,
                    "method": "reported_agent",
                    "scope_match": True,
                },
                assessment,
                sensitivity="restricted",
            ),
            cites_op(assessment, target),
        ],
        caller=OWNER,  # no grant: an unprotected commit, so the notice stands
    )
    agent_view = json.dumps(s.svc.now(s.pid, AGENT))
    assert target in agent_view  # the fact is visible
    assert assessment not in agent_view and "ZQXSECRET" not in agent_view
    # The operation itself withholds the ID; the read path's redaction is a second layer, so the
    # function is checked on its own as well.
    conn = s.svc.open(s.pid).connect(readonly=True)
    try:
        raw = continuity.agent_interpreted(conn, AGENT)
    finally:
        conn.close()
    assert [c["id"] for item in raw for c in item["interpreted_by"]] == [None]
    # Its outcome is its content: hidden from the agent too (review ux-s1-s2-a F2).
    assert [c["outcome"] for item in raw for c in item["interpreted_by"]] == [None]
    assert "challenges" not in agent_view
    agent_notice = [i for i in s.svc.now(s.pid, AGENT)["changed"]["items"] if i["id"] == target]
    assert "an assessment you cannot read" in agent_notice[0]["reason"]
    owner_notice = [i for i in s.svc.now(s.pid, OWNER)["changed"]["items"] if i["id"] == target]
    assert owner_notice[0]["cause_id"] == assessment
    assert owner_notice[0]["outcomes"] == ["challenges"]
    assert [i["id"] for i in s.svc.now(s.pid, AGENT)["changed"]["items"]].count(target) == 1


def mcp_now(s: Scenario) -> dict[str, Any]:
    """Now as an agent reads it through MCP."""
    import anyio
    from mcp import Client

    async def main() -> dict[str, Any]:
        async with Client(build_server(s.svc, s.pid)) as client:
            return dict((await client.call_tool("now_get", {})).structured_content or {})

    return anyio.run(main)
