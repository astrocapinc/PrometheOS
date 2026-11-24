"""Round-two independent-review regressions, using actual SDK replies and fixture bytes."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, Env
from cp4_support import import_package, write_package
from mcp import Client
from pydantic import ValidationError
from test_cp7_jobs import interval, job
from test_cp10_e09_script import e09
from test_cp10_walk import walk

from prometheos import agentops
from prometheos.domain.ids import new_request_id
from prometheos.mcp_adapter import build_server


@pytest.mark.parametrize(
    ("method", "tool", "input_data", "malformed"),
    [
        (
            "job_list",
            "jobs_read",
            {"op": "list"},
            {"jobs": [{"unexpected": "nested-canary"}], "project_seq": 1},
        ),
        (
            "skill_install",
            "skills_manage",
            {
                "op": "install",
                "ref": "synthetic-evidence@1.0.0",
                "host": "claude-code",
                "root_alias": "research",
            },
            {"plan": {"unrelated": True}},
        ),
    ],
)
def test_actual_mcp_rejects_malformed_known_nested_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    tool: str,
    input_data: dict[str, Any],
    malformed: dict[str, Any],
) -> None:
    env = Env(tmp_path)
    monkeypatch.setattr(env.svc, method, lambda *args, **kwargs: malformed)

    async def invoke() -> None:
        async with Client(build_server(env.svc, env.project_id)) as client:
            result = await client.call_tool(tool, {"request": input_data})
            assert result.is_error, result.structured_content

    anyio.run(invoke)


@pytest.mark.parametrize("case", ["job", "projection", "operand", "inspection", "project"])
@pytest.mark.parametrize("mutation", ["missing", "extra", "wrong_scalar"])
def test_nested_contract_negatives_reject_real_mcp_replies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    mutation: str,
) -> None:
    env = Env(tmp_path)
    ctx = agentops.Ctx(env.svc, env.project_id, AGENT)
    if case == "job":
        env.svc.job_add(
            env.project_id,
            AGENT,
            job("digest", interval(60, datetime.now(UTC) + timedelta(hours=1))),
            new_request_id(),
        )
        payload = env.svc.job_list(env.project_id, AGENT)
        method, tool, params = "job_list", "jobs_read", {"op": "list"}
        nested, key, wrong = payload["jobs"][0]["job"]["body"]["budget"], "max_inputs", "twenty"
    elif case in ("projection", "operand", "inspection"):
        source = write_package(env.research / "package")
        if case in ("projection", "operand"):
            import_package(env.svc, source)
            params = {
                "op": "install",
                "ref": "synthetic-evidence@1.0.0",
                "host": "claude-code",
                "root_alias": "research",
            }
            payload = agentops.dispatch("skills_manage", ctx, params)["result"]
            method, tool = "skill_install", "skills_manage"
            nested, key, wrong = payload["plan"]["operations"][0], "action", 42
            if case == "operand":
                nested, key, wrong = payload["target"], "root_alias", 42
        else:
            params = {"op": "inspect", "root_alias": "research", "path": "package"}
            payload = agentops.dispatch("skills_read", ctx, params)["result"]
            method, tool = "skill_inspect", "skills_read"
            nested, key, wrong = payload["files"][0], "executable", "no"
    else:
        payload = {"projects": env.svc.list_projects()}
        method, tool, params = "list_projects", "administration_read", {"op": "projects"}
        nested, key, wrong = payload["projects"][0], "title", 42
    # The real positive result satisfies its exact advertised family member before mutation.
    model = agentops.family_output(tool)
    model.model_validate_json(json.dumps({"op": params["op"], "result": payload}))
    if mutation == "missing":
        nested.pop(key)
    elif mutation == "extra":
        nested["unexpected_nested_canary"] = True
    else:
        nested[key] = wrong
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps({"op": params["op"], "result": payload}))
    if case == "operand":
        # The application layer reconstructs safe operands, so inject at its return boundary.
        monkeypatch.setattr(
            agentops, "dispatch", lambda *args, **kwargs: {"op": params["op"], "result": payload}
        )
    else:
        monkeypatch.setattr(
            env.svc,
            method,
            lambda *args, **kwargs: payload["projects"] if case == "project" else payload,
        )

    async def invoke() -> None:
        async with Client(build_server(env.svc, env.project_id)) as client:
            result = await client.call_tool(tool, {"request": params})
            assert result.is_error, result.structured_content

    anyio.run(invoke)


def test_real_completed_job_slots_satisfy_the_closed_mcp_result_contract(tmp_path: Path) -> None:
    env = Env(tmp_path)
    start = datetime.now(UTC).replace(second=0, microsecond=0)
    made = env.svc.job_add(
        env.project_id, AGENT, job("digest", interval(15, start)), new_request_id()
    )
    tick = env.svc.jobs_tick(env.project_id, now=start + timedelta(hours=2, minutes=1))
    assert tick["ran"][0]["state"] == "succeeded"
    agentops.family_output("jobs_manage").model_validate_json(
        json.dumps({"op": "tick", "result": tick})
    )

    async def invoke() -> None:
        async with Client(build_server(env.svc, env.project_id)) as client:
            listed = await client.call_tool("jobs_read", {"request": {"op": "list"}})
            slots = await client.call_tool(
                "jobs_read", {"request": {"op": "slots", "job_id": made["job_id"]}}
            )
            assert not listed.is_error and not slots.is_error
            rows = slots.structured_content["result"]["slots"]
            assert any(r["state"] == "succeeded" and "usage" in r["result"] for r in rows)
            assert any(r["state"] == "skipped" for r in rows)

    anyio.run(invoke)


def test_failed_e09_resume_is_not_a_successful_restart_fact(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    prepared = e09.prepare(tmp_path / "work", evidence)
    parts = [
        {
            "type": "tool_use",
            "id": "resume-bad",
            "name": "mcp__prometheos__run_resume",
            "input": {"run_id": "oprun_00000000-0000-4000-8000-000000000000"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "resume-bad",
            "is_error": True,
            "content": [{"text": json.dumps({"error": {"code": "not_found"}})}],
        },
    ]
    (evidence / "session-3.jsonl").write_text(
        json.dumps({"message": {"content": parts}}), encoding="utf-8"
    )
    assert e09.verify(prepared, evidence, None)["chat_3_resumed_the_run"] is False


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "failed",
        "wrong_run",
        "precompleted",
        "malformed",
        "missing_discovery",
        "wrong_session",
        "wrong_digest",
        "wrong_plan",
        "stored_incomplete",
    ],
)
def test_e09_restart_requires_discovered_linked_typed_success_and_unfinished_prior_state(
    tmp_path: Path,
    case: str,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    prepared = e09.prepare(tmp_path / "work", evidence)
    s = prepared["seeded"].s
    params = {
        "root_alias": "research",
        "paths": ["artifact.bin"],
        "retention": None,
        "sensitivity": "normal",
        "description": "",
        "titles": None,
    }
    # A real application run, queued through the public testable mode; no host is launched.
    queued = s.svc.run_start(s.pid, AGENT, "source_import_plan", params, mode="none")
    run_id = queued["run_id"]
    discovery = s.svc.run_list(s.pid, AGENT, operation="source_import_plan", limit=100)
    if case != "precompleted":
        e09.first_chat_close(prepared, evidence)
    complete = s.svc.run_resume(s.pid, AGENT, run_id, mode="foreground")
    assert complete["state"] == "completed" and complete["result"]
    if case == "precompleted":
        e09.first_chat_close(prepared, evidence)
    returned = deepcopy(complete)
    if case == "malformed":
        returned.pop("actor")
    if case == "wrong_digest":
        returned["input_digest"] = "0" * 64
    if case == "wrong_plan":
        returned["result"]["plan_sha256"] = "0" * 64
    resume_id = "oprun_00000000-0000-4000-8000-000000000000" if case == "wrong_run" else run_id

    def pair(
        tool: str, use_id: str, input_data: Any, result: Any, *, error: bool = False
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "tool_use",
                "id": use_id,
                "name": "mcp__prometheos__" + tool,
                "input": input_data,
            },
            {
                "type": "tool_result",
                "tool_use_id": use_id,
                "is_error": error,
                "content": [{"text": json.dumps(result)}],
            },
        ]

    found = pair("run_list", "discover", {"operation": "source_import_plan"}, discovery)
    resume = pair("run_resume", "resume", {"run_id": resume_id}, returned, error=case == "failed")
    if case == "wrong_session":
        (evidence / "session-2.jsonl").write_text(
            json.dumps({"message": {"content": found}}), encoding="utf-8"
        )
    parts = resume if case in ("missing_discovery", "wrong_session") else found + resume
    (evidence / "session-3.jsonl").write_text(
        json.dumps({"message": {"content": parts}}), encoding="utf-8"
    )
    checks = e09.verify(prepared, evidence, None)
    if case == "stored_incomplete":
        # A plausible transcript cannot substitute for the stored completed outcome.
        rows = s.svc.run_list(s.pid, AGENT, operation="source_import_plan", limit=100)["runs"]
        for row in rows:
            if row["run_id"] == run_id:
                row.update(state="queued", result=None, resumable=True)
        assert not e09.completed_resumed_import(prepared, evidence, rows)
        assert checks["chat_3_resumed_the_run"]
    else:
        assert checks["chat_3_resumed_the_run"] is (case == "success")


def test_walk_supported_scope_matches_the_actual_plant_fixture(tmp_path: Path) -> None:
    seeded = walk.Seeded(tmp_path / "walk")
    scope = seeded.s.body(seeded.s.claim_a)["scope"]
    fixture = (walk.ROOT / "fixtures" / "plant-result.md").read_text(encoding="utf-8")
    expected = {
        "system": "PLANT-B",
        "operating_regime": "variable-demand synthetic event",
        "model_fidelity": "dynamic plant with actuator constraints",
        "comparator": "reference controller under the same constraints",
        "validation_level": "no measured validation",
    }
    for key, value in expected.items():
        assert value in fixture
        assert scope[key] == value
