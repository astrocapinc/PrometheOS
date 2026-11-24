"""Decision 0078 (UX slices 1-2, from the KERS friction audit): quieter agent surfaces.

- An agent's import answers can be asked for as a summary: the same run document without the
  lists that repeat its result (input digests, artifacts) and its bookkeeping (stages, logs),
  saying what it left out. The default stays the full document every transport shows.
- Now words an agent-checked result plainly, with the assessment's outcome, and still names the
  method and who added it (decision 0018).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import Env
from mcp import Client

from prometheos.app.continuity import _outcome_words
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import SUMMARY_OMITS, build_server, run_detail


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_import_answers_can_be_a_summary_with_the_same_result(env: Env) -> None:
    paths = ["baseline.md", "plant-result.md", "meeting.txt", "manuscript.md"]

    async def main() -> dict[str, Any]:
        async with Client(build_server(env.svc, env.project_id)) as client:

            async def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
                reply = await client.call_tool(name, args)
                assert not reply.is_error, reply
                return dict(reply.structured_content or {})

            plan = await tool(
                "source_import_plan",
                {"root_alias": "research", "paths": paths, "wait_seconds": 20, "detail": "summary"},
            )
            plan_id = plan["result"]["plan_id"]
            commit = await tool(
                "source_import_commit",
                {"plan_id": plan_id, "wait_seconds": 20, "detail": "summary"},
            )
            return {
                "plan": plan,
                "commit": commit,
                "commit_full": await tool("run_get", {"run_id": commit["run_id"]}),
                "commit_summary": await tool(
                    "run_get", {"run_id": commit["run_id"], "detail": "summary"}
                ),
            }

    got = anyio.run(main)
    full, summary = got["commit_full"], got["commit_summary"]
    assert full["state"] == summary["state"] == "completed"
    # The full document carries the repeats; the summary drops exactly those and says so.
    assert full["input_digests"] and full["artifacts"] and full["logs"]
    assert all(summary[key] == [] for key in SUMMARY_OMITS)
    assert summary["limitations"][-1].startswith("Summary view: input_digests, artifacts")
    assert {k: v for k, v in summary.items() if k not in (*SUMMARY_OMITS, "limitations")} == {
        k: v for k, v in full.items() if k not in (*SUMMARY_OMITS, "limitations")
    }
    assert len(json.dumps(summary)) < 0.65 * len(json.dumps(full))  # 4 files; more save more
    # The plan's items are the path -> source, version, sha256 map an agent needs (P21).
    items = got["plan"]["result"]["items"]
    assert sorted(i["relative_path"] for i in items) == sorted(paths)
    assert all(i["source_id"] and i["version_id"] and len(i["sha256"]) == 64 for i in items)


def test_the_default_is_the_full_run_document(env: Env) -> None:
    """Decision 0017: without ``detail`` every tool answers the document all transports show."""

    async def main() -> dict[str, Any]:
        async with Client(build_server(env.svc, env.project_id)) as client:
            reply = await client.call_tool(
                "source_import_plan",
                {"root_alias": "research", "paths": ["baseline.md"], "wait_seconds": 20},
            )
            return dict(reply.structured_content or {})

    plan = anyio.run(main)
    assert plan["state"] == "completed" and plan["logs"] and plan["stages"]
    assert not any(note.startswith("Summary view") for note in plan["limitations"])


def view(**overrides: Any) -> dict[str, Any]:
    base = {"state": "completed", "limitations": ["a"], "input_digests": [{"name": "x"}],
            "artifacts": [{"id": "y"}], "stages": [{"name": "s"}],
            "logs": [{"idx": 0}]}  # fmt: skip
    return base | overrides


def test_a_summary_keeps_what_explains_a_run_and_refuses_unknown_details() -> None:
    lists = {"input_digests": [], "artifacts": [], "stages": [], "logs": []}
    assert run_detail(view(**lists), "summary") == view(**lists)
    assert run_detail(view(), "full") == view()
    # A failed run keeps its logs and stages; so does a run with no room for the note.
    assert run_detail(view(state="failed"), "summary") == view(state="failed")
    crowded = view(limitations=[f"n{i}" for i in range(50)])
    assert run_detail(crowded, "summary") == crowded
    with pytest.raises(PrometheosError, match="detail must be full or summary"):
        run_detail(view(), "bogus")


@pytest.mark.parametrize(
    ("outcomes", "words"),
    [
        (["supports"], "the assessment supports its claim"),
        (["challenges", "challenges"], "the assessment challenges its claim"),
        (["inconclusive"], "the assessment is inconclusive"),
        (["supports", "challenges"], "the assessments disagree (supports, challenges)"),
        ([None], "an assessment you cannot read"),
        ([], "no outcome"),
    ],
)
def test_now_says_what_an_agent_check_found(outcomes: list[str | None], words: str) -> None:
    assert _outcome_words(outcomes) == words
