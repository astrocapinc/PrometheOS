"""CP4 through the real CLI (subprocess) and MCP: plan-then-apply with digests, project
records, and a recipe run recorded by an agent over MCP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import OWNER, OWNER_ACT, Env, create_entry
from cp4_support import write_package
from mcp import Client

from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def cli(env: Env, *args: str) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, "-m", "prometheos", *args, "--json"],
        env={**os.environ, "PROMETHEOS_HOME": str(env.home), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])


def test_cli_plan_apply_and_project_record(env: Env, tmp_path: Path) -> None:
    source = write_package(tmp_path / "src")
    code, plan = cli(env, "skill", "plan", str(source))
    assert code == 0 and plan["status_after"] == "available"
    digest = plan["plan_sha256"]
    code, out = cli(env, "skill", "import", str(source), "--plan-digest", "0" * 64)
    assert code == 3 and out["error"]["code"] == "revision_conflict"
    assert cli(env, "skill", "import", str(source), "--plan-digest", digest)[0] == 0
    target = tmp_path / "proj"
    target.mkdir()
    code, planned = cli(
        env, "skill", "install", "synthetic-evidence", "--host", "opencode", "--target", str(target)
    )
    assert code == 0 and not (target / ".opencode").exists()  # plan only
    code, done = cli(
        env, "skill", "install", "synthetic-evidence", "--host", "opencode", "--target",
        str(target), "--plan-digest", planned["plan"]["plan_sha256"],
        "--project", env.project_id,
    )  # fmt: skip
    assert code == 4 and done["error"]["code"] == "policy_denied"
    assert not (target / ".opencode").exists()  # outside the project roots: no effect
    target = env.research / "projection"
    target.mkdir()
    code, planned = cli(
        env, "skill", "install", "synthetic-evidence", "--host", "opencode", "--target", str(target)
    )
    assert code == 0 and not (target / ".opencode").exists()
    code, required = cli(
        env, "skill", "install", "synthetic-evidence", "--host", "opencode", "--target",
        str(target), "--plan-digest", planned["plan"]["plan_sha256"],
        "--project", env.project_id,
    )  # fmt: skip
    assert code == 4 and required["error"]["code"] == "approval_required"
    approval_id = required["error"]["details"]["approval_request_id"]
    code, pending = cli(env, "approval", "resume", approval_id, "--project", env.project_id)
    assert code == 4 and pending["error"]["code"] == "approval_required"
    assert not (target / ".opencode").exists()
    shown = env.svc.approval_owner_view(env.project_id, approval_id)
    assert shown["operation"] == "skill_install" and shown["state"] == "pending"
    assert shown["plan"]["projection"]["plan_sha256"] == planned["plan"]["plan_sha256"]
    assert shown["plan"]["params"]["root_alias"] == "research"
    assert shown["plan"]["params"]["folder"] == "projection"
    assert shown["plan"]["params"]["record_in_project"] is True
    assert shown["plan"]["files"] and "findings" in shown["plan"]
    with pytest.raises(PrometheosError) as wrong:
        env.svc.approval_decide(
            env.project_id, approval_id, grant=True, plan_sha256="0" * 64, owner=OWNER_ACT
        )
    assert wrong.value.code == "revision_conflict"
    assert env.svc.approval_owner_view(env.project_id, approval_id)["state"] == "pending"
    env.svc.approval_decide(
        env.project_id, approval_id, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )
    code, done = cli(env, "approval", "resume", approval_id, "--project", env.project_id)
    assert code == 0 and done["state"] == "used" and done["replayed"] is False
    installed = target / ".opencode" / "skills" / "synthetic-evidence" / "SKILL.md"
    assert installed.is_file()
    installed_bytes, sequence = installed.read_bytes(), env.seq()
    code, replay = cli(env, "approval", "resume", approval_id, "--project", env.project_id)
    assert code == 0 and replay["replayed"] is True and replay["result"] == done["result"]
    assert installed.read_bytes() == installed_bytes and env.seq() == sequence
    record = done["result"]["record"]["results"][0]["id"]
    assert env.svc.get(env.project_id, OWNER, record, None)["body"]["status"] == "installed"
    code, trusted = cli(env, "skill", "trust", "synthetic-evidence")
    assert (
        code == 4 and trusted["error"]["code"] == "approval_required"
    )  # no terminal: an approval request (CP10.2)


def test_agent_records_a_run_over_mcp(env: Env, tmp_path: Path) -> None:
    from cp4_support import import_package, install

    import_package(env.svc, write_package(tmp_path / "src"))
    target = tmp_path / "proj"
    target.mkdir()
    package_id = install(env.svc, "synthetic-evidence", "claude-code", target, env.project_id)[
        "record"
    ]["results"][0]["id"]
    note = new_id("entry")
    env.apply([create_entry(note)])
    rid = new_request_id()

    async def main() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            rendered = await client.call_tool(
                "workflow_render",
                {"package": "synthetic-evidence", "recipe_id": "evidence-pass",
                 "inputs": {"question": "Holds?"}},
            )  # fmt: skip
            started = await client.call_tool(
                "workflow_start",
                {"package_id": package_id, "recipe_id": "evidence-pass",
                 "inputs": {"question": "Holds?"}, "request_id": rid},
            )  # fmt: skip
            run_id = started.structured_content["results"][0]["id"]
            refused = await client.call_tool(
                "workflow_step",
                {"run_id": run_id, "base_rev": 1, "step_id": "record", "status": "done",
                 "request_id": new_request_id()},
            )  # fmt: skip
            stepped = await client.call_tool(
                "workflow_step",
                {"run_id": run_id, "base_rev": 1, "step_id": "gather", "status": "done",
                 "request_id": new_request_id(),
                 "check_results": [{"id": "gather.1", "result": "pass", "evidence": []}]},
            )  # fmt: skip
            status = await client.call_tool("workflow_status", {"run_id": run_id})
            return rendered, refused, stepped, status

    rendered, refused, stepped, status = anyio.run(main)
    assert "does not run them" in rendered.structured_content["text"]
    assert refused.is_error and '"policy_denied"' in refused.content[0].text
    assert stepped.structured_content["results"][0]["rev"] == 2
    assert status.structured_content["ready_steps"] == ["optional-plot", "record"]
