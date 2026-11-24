"""Dogfood P15 (KERS papers session, 2026-10-19): the MCP tool list failed as a whole for every
client below protocol 2026-07-28, Claude Code included ("Handler returned an invalid result").
The family tools' output schemas were a root ``oneOf`` without ``"type": "object"``, which
those versions require. The suite missed it because the bundled Client negotiates the newest
version, so this test speaks the wire protocol at each version negotiated through
``initialize`` (the 2026 era has its own handshake, which the bundled Client covers)."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import Env
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from prometheos import mcp_adapter
from prometheos.interpreter import CHILD_FLAGS


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _tools_list(env: Env, version: str) -> dict[str, Any]:
    """initialize, initialized and tools/list over stdio at ``version``; the tools/list reply.
    Input stays open until the reply arrives: on end of input the server may stop before it
    answers (review kers-p9-a-r2 R2-1). A reader thread and a deadline bound the wait."""
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": version, "capabilities": {},
                    "clientInfo": {"name": "p15", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]  # fmt: skip
    # CHILD_FLAGS keep PYTHONPATH, so the server is this checkout under any test launcher.
    command = [sys.executable, *CHILD_FLAGS, "-m", "prometheos", "mcp", "serve", "--stdio",
               "--project", env.project_id]  # fmt: skip
    replies: queue.Queue[dict[str, Any] | None] = queue.Queue()
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ, PROMETHEOS_HOME=str(env.home)),
    ) as server:
        assert server.stdin is not None and server.stdout is not None
        out = server.stdout

        def read() -> None:
            for raw in out:
                if raw.strip():
                    replies.put(json.loads(raw))
            replies.put(None)  # the server closed its output

        threading.Thread(target=read, daemon=True).start()
        server.stdin.write("".join(json.dumps(m) + "\n" for m in lines).encode("utf-8"))
        server.stdin.flush()
        by_id: dict[Any, dict[str, Any]] = {}
        deadline = time.monotonic() + 120
        try:
            while 2 not in by_id:
                reply = replies.get(timeout=max(0.1, deadline - time.monotonic()))
                if reply is None:
                    break
                by_id[reply.get("id")] = reply
        except queue.Empty:
            pass
        finally:
            server.stdin.close()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
        stderr = server.stderr.read().decode("utf-8", "replace") if server.stderr else ""
    assert 1 in by_id and 2 in by_id, stderr[-2000:]
    assert by_id[1]["result"]["protocolVersion"] == version
    return by_id[2]


@pytest.mark.parametrize("version", HANDSHAKE_PROTOCOL_VERSIONS)
def test_every_initialize_era_version_gets_the_tool_list(env: Env, version: str) -> None:
    reply = _tools_list(env, version)
    assert "error" not in reply, reply.get("error")
    assert len(reply["result"]["tools"]) > 100


def test_every_output_schema_is_an_object_at_its_root(env: Env) -> None:
    for server in (
        mcp_adapter.build_server(env.svc, env.project_id),
        mcp_adapter.build_maintenance_server(env.svc, env.project_id),
    ):
        for tool in anyio.run(server.list_tools):
            if tool.output_schema is not None:
                assert tool.output_schema.get("type") == "object", tool.name
