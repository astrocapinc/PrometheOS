"""Decision 0079 (P22, P28): an agent applies a large request from a file.

The KERS P3 ledger was a 62 KB request that MCP could only take inline, so the agent fell back
to the CLI. MCP ``apply`` now also takes ``request_file`` {root_alias, path}: a JSON file inside
an approved research root, resolved like every agent file operand and applied exactly as the
inline request would be. A missing file names itself and is not retryable, on MCP and the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import OWNER, Env, create_entry, request
from mcp import Client
from typer.testing import CliRunner

from prometheos.cli import app
from prometheos.domain.models import MAX_REQUEST_BYTES
from prometheos.mcp_adapter import build_server


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def call(env: Env, args: dict[str, Any]) -> tuple[bool, Any]:
    async def main() -> tuple[bool, Any]:
        async with Client(build_server(env.svc, env.project_id)) as client:
            reply = await client.call_tool("apply", args)
            text = "".join(getattr(c, "text", "") for c in reply.content)
            return bool(reply.is_error), (reply.structured_content if not reply.is_error else text)

    return anyio.run(main)


def entries(env: Env) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM records WHERE kind = 'entry'")]
    finally:
        conn.close()
    return sorted(env.svc.get(env.project_id, OWNER, i, None)["body"]["title"] for i in ids)


def test_a_request_file_applies_like_the_inline_request_and_replays(env: Env) -> None:
    ops = [create_entry(title=f"Ledger row {i}", text="x" * 2000) for i in range(30)]
    raw = request(env.project_id, ops, env.seq())
    (env.research / "requests").mkdir()
    (env.research / "requests" / "p3-ledger.json").write_bytes(raw)
    assert len(raw) > 60_000  # the KERS P3 ledger's size class
    failed, first = call(
        env, {"request_file": {"root_alias": "research", "path": "requests/p3-ledger.json"}}
    )
    assert not failed, first
    assert entries(env) == sorted(f"Ledger row {i}" for i in range(30))
    # The same file again is the same request: an identical retry replays, creating nothing.
    failed, again = call(
        env, {"request_file": {"root_alias": "research", "path": "requests/p3-ledger.json"}}
    )
    assert not failed and again["project_seq"] == first["project_seq"]
    assert len(entries(env)) == 30


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "give exactly one of request or request_file"),
        (
            {
                "request": {"schema_version": 1},
                "request_file": {"root_alias": "research", "path": "a.json"},
            },
            "give exactly one of request or request_file",
        ),
        (
            {"request_file": {"root_alias": "research", "path": "missing.json"}},
            "request file not found",
        ),
        ({"request_file": {"root_alias": "research", "path": "../outside.json"}}, "policy_denied"),
        (
            {"request_file": {"root_alias": "elsewhere", "path": "a.json"}},
            "research root alias is not approved",
        ),
    ],
)
def test_a_request_file_must_be_one_file_inside_an_approved_root(
    env: Env, args: dict[str, Any], message: str
) -> None:
    (env.tmp / "outside.json").write_text("{}", encoding="utf-8")
    failed, text = call(env, args)
    assert failed and message in text
    if "not found" in text:
        envelope = json.loads(text[text.index("{") :])
        assert envelope["error"]["retryable"] is False
        assert envelope["error"]["details"] == {"root_alias": "research", "path": "missing.json"}
    assert entries(env) == []


def test_an_oversized_request_file_is_refused(env: Env) -> None:
    big = env.research / "big.json"
    with big.open("wb") as handle:
        handle.truncate(MAX_REQUEST_BYTES + 1)
    failed, text = call(env, {"request_file": {"root_alias": "research", "path": "big.json"}})
    assert failed and "request file exceeds the request size limit" in text


def test_the_cli_names_a_missing_request_file_and_does_not_invite_a_retry(
    env: Env, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-request.json"
    done = CliRunner().invoke(
        app,
        ["apply", "--file", str(missing), "--project", env.project_id, "--json"],
        env={"PROMETHEOS_HOME": str(env.home)},
    )
    assert done.exit_code == 2  # validation_error
    envelope = json.loads(done.stdout[done.stdout.index("{") :])
    assert envelope["error"]["message"] == "request file not found"
    assert envelope["error"]["retryable"] is False
    assert envelope["error"]["details"]["file"] == str(missing)


@pytest.mark.parametrize(
    "path",
    [
        "C:/Windows/win.ini",
        "/etc/passwd",
        "//server/share/a.json",
        "requests/../../outside.json",
        ".git/config",
        "a.json:stream",
        "PROGRA~1/a.json",
    ],
)
def test_no_request_file_outside_the_root_and_no_absolute_path_in_the_reply(
    env: Env, path: str
) -> None:
    """Review ux-p22-a F5: the hostile inputs the reviewer probed, refused before any read, and
    the reply never carries an absolute local path (decision 0022)."""
    failed, text = call(env, {"request_file": {"root_alias": "research", "path": path}})
    # Refused by the root check itself, not by a later failure (review ux-p22-a-r2 R2-1).
    assert failed and "policy_denied" in text
    assert str(env.tmp) not in text and str(env.research) not in text
    assert entries(env) == []


def test_a_folder_or_a_broken_file_is_refused_with_the_inline_envelope(env: Env) -> None:
    (env.research / "requests").mkdir()
    failed, text = call(env, {"request_file": {"root_alias": "research", "path": "requests"}})
    assert failed and "request file is not a regular file" in text
    (env.research / "broken.json").write_bytes(b'{"schema_version": 1,')
    failed, from_file = call(
        env, {"request_file": {"root_alias": "research", "path": "broken.json"}}
    )
    assert failed and "validation_error" in from_file
    assert entries(env) == []
