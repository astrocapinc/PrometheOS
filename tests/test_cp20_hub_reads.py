"""CP20.4, the hub slice (A06.1; C20-STATUS, C20-ADOPT; gap audit #16): what the research map read
adds for the hub, and the owner's budget control.

- A claim's Details carry its verifiables: each evidence version's locator (a whole file shows
  path, digest and size), the recorded runs that wrote it with their producer files and receipt,
  and the latest assessment's checks with expected, measured and tolerance. Hidden records stay
  out, as everywhere in the map.
- Each ``answers`` and ``addresses`` link says who placed it (decision 0083 F2): a read-only join
  on the audit row its transaction wrote, ``agent`` for the MCP and agent-token services.
- The hub's budget control previews and applies the same ``project_update`` as
  ``prometheos verify budget``; only the paired owner reaches it, and the apply needs the digest
  the preview showed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env
from hub_support import running_hub
from test_cp20_claim_status import assess, claim, imported, line_ev, receipt
from test_cp20_structure import entry, link, paper, research_map

from prometheos.app.reads import Caller
from prometheos.app.service import Service
from prometheos.domain.ids import new_request_id


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "script.m").write_text("energy = 196.9;\n", "utf-8")
    (environment.research / "result.csv").write_text(
        "name = value\nenergy_MJ = 196.9\nenergy_MJ = 63.6\n", "utf-8"
    )
    (environment.research / "result.mat").write_bytes(b"MATLAB 5.0 MAT-file synthetic" * 20)
    (environment.research / "private.m").write_text("secret = 1;\n", "utf-8")
    return environment


def whole(item: dict[str, str]) -> dict[str, Any]:
    return {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "file"},
        "quote_sha256": None,
    }


def claim_item(found: dict[str, Any], cid: str) -> dict[str, Any]:
    for d in found["deliverables"]:
        for q in d["questions"]:
            for c in q["claims"]:
                if c["claim_id"] == cid:
                    return dict(c)
        for c in d["other_claims"]:
            if c["claim_id"] == cid:
                return dict(c)
    raise AssertionError(f"{cid} not on the map")


def test_details_carry_the_file_the_run_that_wrote_it_and_the_checks(env: Env) -> None:
    files = imported(env, ["script.m", "result.csv", "result.mat"])
    restricted = env.import_files(["private.m"], sensitivity="restricted")["plan"]["items"][0]
    secret = {k: restricted[k] for k in ("source_id", "version_id", "sha256")}
    receipt(env, [files["script.m"]], [files["result.csv"]])
    # A second run, recorded by the owner, read a restricted script: its receipt is restricted.
    owner_receipt(env, [secret], [files["result.csv"]])
    cid = claim(env, [line_ev(files["result.csv"], 2), whole(files["result.mat"])])
    assess(env, cid, checks=[{
        "name": "R1 re-derivation", "result": "pass", "evidence": [],
        "expected": "~200 MJ", "measured": "196.9 MJ", "tolerance": "order of magnitude",
    }])  # fmt: skip
    pid = paper(env, claim_refs=[{"id": cid, "rev": 1}])

    item = claim_item(research_map(env, OWNER, paper_id=pid), cid)
    by_path = {e["path"]: e for e in item["details"]["evidence"]}
    mat = by_path["result.mat"]
    assert mat["locator"] == {"type": "file"}
    assert mat["sha256"] == files["result.mat"]["sha256"] and mat["byte_length"] == 580
    assert mat["runs"] == []
    csv = by_path["result.csv"]
    assert csv["locator"] == {"type": "text", "start_line": 2, "end_line": 2}
    runs = {r["receipt_title"]: r for r in csv["runs"]}
    assert set(runs) == {"run 1", "owner run"}  # the owner sees both runs
    run = runs["run 1"]
    assert (run["outcome"], run["command"], run["reporter"]) == (
        "succeeded",
        "run('script.m')",
        "test agent",
    )
    assert run["receipt_version_id"].startswith("version_")
    (script,) = run["producers"]
    assert (script["path"], script["sha256"]) == ("script.m", files["script.m"]["sha256"])
    assert [p["path"] for p in runs["owner run"]["producers"]] == ["private.m"]
    checks = item["details"]["assessment"]
    assert checks["basis"] == "agent_reported" and checks["outcome"] == "supports"
    assert checks["checks"] == [
        {
            "name": "R1 re-derivation",
            "result": "pass",
            "expected": "~200 MJ",
            "measured": "196.9 MJ",
            "tolerance": "order of magnitude",
        }
    ]

    # An agent never learns of the restricted run or its producer.
    seen = claim_item(research_map(env, AGENT, paper_id=pid), cid)
    (agent_run,) = {e["path"]: e for e in seen["details"]["evidence"]}["result.csv"]["runs"]
    assert agent_run["receipt_title"] == "run 1"
    assert "private.m" not in str(seen) and "owner run" not in str(seen)


def owner_receipt(env: Env, inputs: list[dict[str, str]], outputs: list[dict[str, str]]) -> None:
    body = {
        "schema_version": 1,
        "observation": "reported",
        "reporter": "the owner",
        "command": "run('private.m')",
        "working_root": "research",
        "environment": {"os": "TestOS", "tools": []},
        "started_at": None,
        "ended_at": None,
        "exit_status": 0,
        "outcome": "succeeded",
        "inputs": [i | {"role": "script"} for i in inputs],
        "outputs": [o | {"role": "result"} for o in outputs],
        "limitations": [],
    }
    env.svc.receipt_record(
        env.project_id,
        raw=json.dumps(body).encode("utf-8"),
        title="owner run",
        description="",
        request_id=new_request_id(),
        caller=OWNER,
    )


def test_a_claim_with_no_assessment_has_none_and_the_read_changes_nothing(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    pid = paper(env, claim_refs=[{"id": cid, "rev": 1}])
    before = env.seq()
    item = claim_item(research_map(env, OWNER, paper_id=pid), cid)
    assert item["details"]["assessment"] is None
    assert item["linked_by"] is None  # not under a question
    assert env.seq() == before


def test_links_say_who_placed_them(env: Env) -> None:
    files = imported(env, ["result.csv"])
    by_agent = claim(env, [line_ev(files["result.csv"], 2)])
    by_owner = claim(env, [line_ev(files["result.csv"], 3)])
    question = entry(env, "question", "Does the climb dominate the energy?")
    other = entry(env, "question", "Is the reduced model enough?")
    pid = paper(env)
    # The agent's MCP server and agent-token HTTP service write as "mcp" and "http-agent".
    mcp = Service(home=env.home, actor="mcp")
    http_agent = Service(home=env.home, actor="http-agent")
    agent_env, token_env = _as(env, mcp), _as(env, http_agent)
    link(agent_env, by_agent, question, "answers")
    link(token_env, question, pid, "addresses")
    link(env, by_owner, question, "answers", caller=OWNER)
    link(env, other, pid, "addresses", caller=Caller(actor="cli-owner", scope="owner"))

    (d,) = research_map(env, OWNER, paper_id=pid)["deliverables"]
    questions = {q["entry_id"]: q for q in d["questions"]}
    assert questions[question]["linked_by"] == "agent"
    assert questions[other]["linked_by"] == "owner_or_cli"
    placed = {c["claim_id"]: c["linked_by"] for c in questions[question]["claims"]}
    assert placed == {by_agent: "agent", by_owner: "owner_or_cli"}


def test_mutation_the_agent_actors_decide_what_reads_as_placed_by_the_agent(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the agent services' actors the same links read as the owner's: the test above
    would fail."""
    from prometheos.app import research_map as rm

    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    question = entry(env, "question", "Does the climb dominate the energy?")
    pid = paper(env)
    agent_env = _as(env, Service(home=env.home, actor="mcp"))
    link(agent_env, cid, question, "answers")
    link(agent_env, question, pid, "addresses")
    (d,) = research_map(env, OWNER, paper_id=pid)["deliverables"]
    assert d["questions"][0]["claims"][0]["linked_by"] == "agent"
    monkeypatch.setattr(rm, "AGENT_ACTORS", frozenset())
    (d,) = research_map(env, OWNER, paper_id=pid)["deliverables"]
    assert d["questions"][0]["linked_by"] == "owner_or_cli"
    assert d["questions"][0]["claims"][0]["linked_by"] == "owner_or_cli"


def _as(env: Env, svc: Service) -> Env:
    """The same project through a service with another audit actor."""
    other = object.__new__(Env)
    other.__dict__.update(env.__dict__)
    other.svc = svc
    return other


# ----- the hub's budget control -------------------------------------------------------------


def test_the_paired_owner_previews_and_applies_the_budget_change(env: Env) -> None:
    budget = {
        "max_minutes_each": 20,
        "max_minutes_per_day": 90,
        "output_root_alias": "research",
        "output_path": "checks",
    }
    roots_before = env.svc.get(env.project_id, OWNER, env.project_id, None)["body"]["allowed_roots"]
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        shown = hub.request("POST", "/api/v1/verification/budget/preview", {"budget": budget})
        assert shown.status == 200, shown.body
        preview = shown.json()
        assert preview["would_apply"] is True
        assert any(e.startswith("verification budget change") for e in preview["protected_effects"])
        assert preview["operations"] == ["project_update"]
        before = env.seq()
        # No digest, or another digest, applies nothing.
        missing = hub.request("POST", "/api/v1/verification/budget", {"budget": budget})
        assert missing.status == 400 and missing.json()["error"]["code"] == "validation_error"
        wrong = hub.request(
            "POST", "/api/v1/verification/budget", {"budget": budget, "digest": "0" * 64}
        )
        assert wrong.json()["error"]["code"] == "revision_conflict"
        assert env.seq() == before
        done = hub.request(
            "POST",
            "/api/v1/verification/budget",
            {"budget": budget, "digest": preview["digest"]},
        )
        assert done.status == 200, done.body
        assert env.seq() == before + 1
        # The same preview cannot be applied twice: the project moved on.
        again = hub.request(
            "POST",
            "/api/v1/verification/budget",
            {"budget": budget, "digest": preview["digest"]},
        )
        assert again.json()["error"]["code"] == "revision_conflict"
        # An agent token never reaches either route.
        token = hub.gate.new_agent_token()
        for path in ("/api/v1/verification/budget/preview", "/api/v1/verification/budget"):
            denied = hub.request(
                "POST",
                path,
                {"budget": budget, "digest": preview["digest"]},
                origin=None,
                cookie=None,
                csrf=None,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert denied.status in (401, 403), (path, denied.status)
    stored = env.svc.get(env.project_id, OWNER, env.project_id, None)["body"]
    assert stored["verification_budget"] == budget
    assert stored["allowed_roots"] == roots_before  # the settings' paths were never resent
    plan = env.svc.verification_plan(env.project_id, AGENT)["budget"]
    assert (plan["max_minutes_each"], plan["max_minutes_per_day"]) == (20, 90)


def test_a_budget_that_cannot_apply_is_refused_before_any_preview(env: Env) -> None:
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        over = {"max_minutes_each": 90, "max_minutes_per_day": 60}
        refused = hub.request("POST", "/api/v1/verification/budget/preview", {"budget": over})
        assert refused.status == 400
        unknown = {
            "max_minutes_each": 10,
            "max_minutes_per_day": 60,
            "output_root_alias": "elsewhere",
            "output_path": "checks",
        }
        shown = hub.request("POST", "/api/v1/verification/budget/preview", {"budget": unknown})
        # A folder outside the approved roots does not apply (review cp20-3-a F4).
        assert shown.status == 200 and shown.json()["would_apply"] is False, shown.body
