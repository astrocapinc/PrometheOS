"""The research map reaches the agent's other reads (A06.1; decision 0107; gap audit items 1, 3
and 4): ``context_get`` leads with the claims not to quote and the checks the agent may start,
shows each claim's derived status, and stops calling a settled challenge unresolved; ``now_get``
carries the map's counts.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from cp1_support import AGENT, Env
from cp7_support import create
from mcp import Client
from test_cp20_claim_status import assess, claim, imported, line_ev, receipt

from prometheos.mcp_adapter import build_server


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "script.m").write_text("energy = 196.9;\n", "utf-8")
    (environment.research / "result.csv").write_text(
        "name = value\nenergy_MJ = 196.9\nenergy_MJ = 63.6\n", "utf-8"
    )
    return environment


def stale_claim(env: Env) -> str:
    files = imported(env, ["script.m", "result.csv"])
    receipt(env, [files["script.m"]], [files["result.csv"]])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid)
    (env.research / "script.m").write_text("energy = 63.6;\n", "utf-8")
    imported(env, ["script.m"])
    return cid


def test_context_leads_with_what_not_to_quote_and_the_checks_an_agent_may_start(
    env: Env,
) -> None:
    cid = stale_claim(env)
    unverified = claim(env, [])  # no evidence: the detector plans a read-only check
    env.svc.verification_propose(env.project_id, AGENT)
    context = env.svc.context(env.project_id, AGENT)
    text = context["text"]
    section = next(s for s in context["manifest"]["sections"] if s["name"] == "research_map")
    assert section["critical"] is True and cid in section["included"]
    assert "STALE, do not quote until it is checked again" in text
    assert "CHECK you may start within the researcher's budget" in text
    assert text.index("## research_map") < text.index("## findings")
    assert "(derived status: stale" in text and "(derived status: unverified" in text
    assert unverified in {s for sec in context["manifest"]["sections"] for s in sec["included"]}


def test_a_settled_challenge_is_not_reported_as_unresolved(env: Env) -> None:
    files = imported(env, ["result.csv"])
    cid = claim(env, [line_ev(files["result.csv"], 2)])
    assess(env, cid, "challenges")
    assert "UNRESOLVED" in env.svc.context(env.project_id, AGENT)["text"]
    owner = {
        "claim_ref": {"id": cid, "rev": 1},
        "outcome": "supports",
        "rationale": "I checked.",
        "checks": [],
        "author_ref": None,
        "method": "researcher",
        "scope_match": True,
    }
    env.apply_protected([create("assessment", owner)])  # the owner's own support settles it
    assert "UNRESOLVED" not in env.svc.context(env.project_id, AGENT)["text"]


def test_now_carries_the_map_counts_through_the_service_and_mcp(env: Env) -> None:
    stale_claim(env)
    counts = env.svc.now(env.project_id, AGENT)["research_map"]
    assert counts["stale"] == 1 and counts["not_usable"] == 1
    assert {"cards_waiting", "checks_open", "checks_eligible", "agent_reported"} <= set(counts)

    async def main() -> dict[str, int]:
        async with Client(build_server(env.svc, env.project_id)) as client:
            reply = await client.call_tool("now_get", {})
            assert not reply.is_error, reply
            return dict((reply.structured_content or {})["research_map"])

    assert anyio.run(main) == counts
