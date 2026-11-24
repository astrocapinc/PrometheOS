"""CP9 transports: hub routes over real HTTP, the CLI and MCP reach the same service
operations with the same validation; owner-only actions (engine install, push) are not
available to an agent's channels."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import CP10_REQUEST_TOOLS, Env
from cp9_support import LatexEnv
from hub_support import Hub, running_hub
from mcp import Client
from test_cp1_cli import Cli as BaseCli

from prometheos.mcp_adapter import build_server


class Cli(BaseCli):
    def __init__(self, env: Env) -> None:
        super().__init__(env.tmp)

    def ok(self, *args: str) -> Any:
        code, payload, stderr = self(*args)
        assert code == 0, (payload, stderr)
        return payload

    def err(self, *args: str) -> Any:
        code, payload, _ = self(*args)
        assert code != 0 and payload["ok"] is False, payload
        return payload


@pytest.fixture
def ws(tmp_path: Path) -> LatexEnv:
    return LatexEnv(Env(tmp_path))


@pytest.fixture
def hub(ws: LatexEnv) -> Iterator[Hub]:
    with running_hub(ws.svc, ws.pid) as running:
        running.pair()
        yield running


def test_hub_workspace_routes_and_external_change_refusal(ws: LatexEnv, hub: Hub) -> None:
    base = f"/api/v1/papers/{ws.paper_id}"
    files = hub.request("GET", f"{base}/workspace/files")
    assert files.status == 200
    assert "main.tex" in {f["path"] for f in files.json()["files"]}
    loaded = hub.request("GET", f"{base}/workspace/file?path=main.tex").json()
    ws.path("main.tex").write_text(loaded["text"] + "% external\n", "utf-8")
    body = {
        "path": "main.tex",
        "text": loaded["text"] + "% draft\n",
        "base_sha256": loaded["sha256"],
    }
    refused = hub.request("PUT", f"{base}/workspace/file", body)
    assert refused.status == 409
    assert refused.json()["error"]["code"] == "revision_conflict"
    assert "% draft" not in ws.path("main.tex").read_text("utf-8")
    # Mutations need the CSRF token and the paired session like every other write.
    assert hub.request("PUT", f"{base}/workspace/file", body, csrf=False).status == 403
    assert hub.request("PUT", f"{base}/workspace/file", body, cookie=False).status == 401
    structure = hub.request("GET", f"{base}/workspace/structure").json()
    assert {item["key"] for item in structure["bib_keys"]} == {"doe2024", "roe2023"}
    status = hub.request("GET", f"{base}/compile").json()
    assert status["last_compile"] is None and status["pdf"] is None
    engine = hub.request("GET", "/api/v1/tex/engine").json()
    assert engine["plan"]["license"] == "MIT"
    # Not a Git clone: a clear validation error, not a server error.
    overleaf = hub.request("GET", f"{base}/overleaf")
    assert overleaf.status == 400
    assert "Git clone" in overleaf.json()["error"]["message"]


def test_hub_push_apply_refuses_a_stale_plan_digest(ws: LatexEnv, hub: Hub) -> None:
    reply = hub.request(
        "POST", f"/api/v1/papers/{ws.paper_id}/overleaf/push/apply", {"digest": "0" * 64}
    )
    assert reply.status == 400  # not a clone; nothing was pushed
    assert reply.json()["error"]["code"] == "validation_error"


def test_cli_reaches_the_workspace_with_the_same_rules(ws: LatexEnv, tmp_path: Path) -> None:
    cli = Cli(ws.env)
    listing = cli.ok("paper", "workspace", "files", ws.paper_id, "--project", ws.pid)
    assert "refs.bib" in {f["path"] for f in listing["files"]}
    read = cli.ok("paper", "workspace", "read", ws.paper_id, "main.tex", "--project", ws.pid)
    draft = tmp_path / "draft.tex"
    draft.write_text(read["text"] + "% cli edit\n", "utf-8")
    ws.path("main.tex").write_text(read["text"] + "% external\n", "utf-8")
    stale = cli.err(
        "paper", "workspace", "write", ws.paper_id, "main.tex",
        "--from-file", str(draft), "--base", read["sha256"], "--project", ws.pid,
    )  # fmt: skip
    assert stale["error"]["code"] == "revision_conflict"
    fresh = hashlib.sha256(ws.path("main.tex").read_bytes()).hexdigest()
    written = cli.ok(
        "paper", "workspace", "write", ws.paper_id, "main.tex",
        "--from-file", str(draft), "--base", fresh, "--project", ws.pid,
    )  # fmt: skip
    assert written["sha256"] == hashlib.sha256(draft.read_bytes()).hexdigest()
    snap = cli.ok("paper", "workspace", "snapshot", ws.paper_id, "--project", ws.pid)
    assert snap["recorded"] >= 1
    structure = cli.ok("paper", "workspace", "structure", ws.paper_id, "--project", ws.pid)
    assert [o["title"] for o in structure["outline"]] == ["Introduction", "Results"]


def test_cli_owner_actions_need_an_interactive_owner(ws: LatexEnv) -> None:
    cli = Cli(ws.env)
    plan = cli.ok("tex", "engine", "install")["plan"]
    # CP10.2 (decision 0016): without a terminal the install becomes an approval request.
    refused = cli.err(
        "tex", "engine", "install", "--digest", plan["plan_digest"], "--project", ws.pid
    )
    assert refused["error"]["code"] == "approval_required"
    assert ws.env.svc.tex_engine_status()["state"] != "installed" or plan["already_installed"]
    push = cli.err(
        "paper", "overleaf", "push", ws.paper_id, "--digest", "0" * 64, "--project", ws.pid
    )
    # A digest that is not the current plan cannot even be requested.
    assert push["error"]["code"] in ("validation_error", "policy_denied", "revision_conflict")


def _mcp(env: Env, steps: Any) -> Any:
    async def main() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await steps(client)

    return anyio.run(main)


def _payload(result: Any) -> dict[str, Any]:
    text = result.content[0].text
    return json.loads(text[text.index("{") :])  # type: ignore[no-any-return]


def test_mcp_workspace_tools_match_the_cli(ws: LatexEnv) -> None:
    loaded = ws.read("main.tex")
    ws.path("main.tex").write_text(loaded["text"] + "% external\n", "utf-8")

    async def steps(client: Client) -> Any:
        read = await client.call_tool(
            "paper_workspace_read", {"paper_id": ws.paper_id, "path": "sections/intro.tex"}
        )
        stale = await client.call_tool(
            "paper_workspace_write",
            {
                "paper_id": ws.paper_id,
                "path": "main.tex",
                "text": "overwrite attempt",
                "base_sha256": loaded["sha256"],
            },
        )
        structure = await client.call_tool("paper_workspace_structure", {"paper_id": ws.paper_id})
        status = await client.call_tool("tex_engine_status", {})
        tools = {t.name for t in (await client.list_tools()).tools}
        return read, stale, structure, status, tools

    read, stale, structure, status, tools = _mcp(ws.env, steps)
    assert _payload(read)["text"].startswith("This synthetic manuscript")
    assert stale.is_error and _payload(stale)["error"]["code"] == "revision_conflict"
    assert "overwrite attempt" not in ws.path("main.tex").read_text("utf-8")
    assert {b["key"] for b in _payload(structure)["bib_keys"]} == {"doe2024", "roe2023"}
    assert _payload(status)["pinned_version"] == "0.17.0"
    # Owner actions are not tools; CP10.2 offers them only as approval requests.
    assert {"overleaf_push", "tex_engine_install"} <= CP10_REQUEST_TOOLS
    assert not {"overleaf_push_grant", "decision_grant", "provider_approve"} & set(tools)
