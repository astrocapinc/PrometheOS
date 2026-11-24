"""Regressions for review cp9-a (pre-check, changes requested; decision 0012 "Review round 1").

CP9-A-1: a NORMAL paper whose workspace holds one file registered RESTRICTED: agents get
nothing from that file through read, structure, listing, write, CLI or MCP; the owner can.
CP9-A-2: hidden paths (``.git/config`` holding a credential URL) are never read.
CP9-A-3: the listing says nothing about a file above the caller's scope.
CP9-A-4: the hub CSP names the worker source explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env
from cp9_support import LatexEnv
from mcp import Client
from test_cp9_transports import Cli

from prometheos.domain.models import PrometheosError
from prometheos.hub.security import CSP
from prometheos.mcp_adapter import build_server

CANARY = "RESTRICTEDCANARY91"
TOKEN = "olp_REVIEWTOKEN0000111122223333"


@pytest.fixture
def ws(tmp_path: Path) -> LatexEnv:
    latex = LatexEnv(Env(tmp_path))
    intro = latex.path("sections/intro.tex")
    intro.write_text(f"\\subsection{{Hidden {CANARY}}}\\label{{sec:{CANARY}}}\n{CANARY}\n", "utf-8")
    plan = latex.svc.import_plan(
        latex.pid,
        OWNER,
        root_alias="research",
        paths=["paper/sections/intro.tex"],
        retention="copy",
        sensitivity="restricted",
    )
    latex.svc.import_commit(latex.pid, plan["plan_id"], OWNER)
    assert latex.paper()["sensitivity"] == "normal"
    return latex


def _refused(call: Any) -> dict[str, Any]:
    with pytest.raises(PrometheosError) as refused:
        call()
    response = refused.value.to_response().model_dump()
    assert refused.value.code == "policy_denied"
    assert refused.value.details == {}
    assert CANARY not in json.dumps(response) and "intro" not in json.dumps(response)
    return response


def test_agent_gets_nothing_from_a_restricted_file_in_a_normal_paper(ws: LatexEnv) -> None:
    _refused(lambda: ws.svc.workspace_read(ws.pid, AGENT, ws.paper_id, "sections/intro.tex"))
    # Case variants and the on-disk name resolve to the same registered file.
    _refused(lambda: ws.svc.workspace_read(ws.pid, AGENT, ws.paper_id, "Sections/INTRO.tex"))
    _refused(lambda: ws.svc.workspace_structure(ws.pid, AGENT, ws.paper_id))
    _refused(
        lambda: ws.svc.workspace_write(
            ws.pid, AGENT, ws.paper_id, "sections/intro.tex", "overwrite", None
        )
    )
    _refused(lambda: ws.svc.workspace_snapshot(ws.pid, AGENT, ws.paper_id))
    listing = ws.svc.workspace_files(ws.pid, AGENT, ws.paper_id)
    assert "sections/intro.tex" not in {f["path"] for f in listing["files"]}
    assert CANARY not in json.dumps(listing) and "hidden" not in {
        f["state"] for f in listing["files"]
    }
    assert CANARY in ws.path("sections/intro.tex").read_text("utf-8")  # untouched
    # Other files of the paper stay readable for the agent.
    assert ws.svc.workspace_read(ws.pid, AGENT, ws.paper_id, "main.tex")["text"]


def test_owner_reads_and_structures_the_restricted_file(ws: LatexEnv) -> None:
    read = ws.svc.workspace_read(ws.pid, OWNER, ws.paper_id, "sections/intro.tex")
    assert CANARY in read["text"]
    structure = ws.svc.workspace_structure(ws.pid, OWNER, ws.paper_id)
    assert f"sec:{CANARY}" in {item["key"] for item in structure["labels"]}
    listing = ws.svc.workspace_files(ws.pid, OWNER, ws.paper_id)
    intro = next(f for f in listing["files"] if f["path"] == "sections/intro.tex")
    assert intro["source_id"] is not None and intro["state"] == "versioned"


def test_agent_transports_get_nothing_from_the_restricted_file(ws: LatexEnv) -> None:
    cli = Cli(ws.env)
    for args in (
        ("paper", "workspace", "read", ws.paper_id, "sections/intro.tex"),
        ("paper", "workspace", "structure", ws.paper_id),
    ):
        reply = cli.err(*args, "--project", ws.pid)
        assert reply["error"]["code"] == "policy_denied" and CANARY not in json.dumps(reply)
    listing = cli.ok("paper", "workspace", "files", ws.paper_id, "--project", ws.pid)
    assert CANARY not in json.dumps(listing) and "intro" not in json.dumps(listing)

    async def main() -> str:
        async with Client(build_server(ws.svc, ws.pid)) as client:
            texts = []
            for name, args in (
                ("paper_workspace_read", {"paper_id": ws.paper_id, "path": "sections/intro.tex"}),
                ("paper_workspace_structure", {"paper_id": ws.paper_id}),
                ("paper_workspace_files", {"paper_id": ws.paper_id}),
            ):
                reply = await client.call_tool(name, args)
                texts.append(str(reply.content[0].text))  # type: ignore[union-attr]
            return "\n".join(texts)

    text = anyio.run(main)
    assert CANARY not in text and "sections/intro.tex" not in text
    assert text.count("policy_denied") == 2


def test_hidden_paths_are_never_read(tmp_path: Path) -> None:
    ws = LatexEnv(Env(tmp_path))
    git_dir = ws.folder / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        f'[remote "origin"]\n\turl = https://git:{TOKEN}@git.overleaf.com/abc\n', "utf-8"
    )
    (ws.folder / ".hidden.tex").write_text(TOKEN, "utf-8")
    # Round 2 (R2-5): the owner may read non-VCS dot files; VCS metadata stays refused.
    assert ws.svc.workspace_read(ws.pid, OWNER, ws.paper_id, ".hidden.tex")["text"] == TOKEN
    for caller in (AGENT, OWNER):
        for path in (".git/config", ".GIT/config", "sections/../.git/config"):
            with pytest.raises(PrometheosError) as refused:
                ws.svc.workspace_read(ws.pid, caller, ws.paper_id, path)
            assert refused.value.code == "policy_denied"
            assert TOKEN not in json.dumps(refused.value.to_response().model_dump())
    with pytest.raises(PrometheosError):
        ws.svc.workspace_read(ws.pid, AGENT, ws.paper_id, ".hidden.tex")
    for value in (
        ws.svc.workspace_files(ws.pid, AGENT, ws.paper_id),
        ws.svc.workspace_structure(ws.pid, AGENT, ws.paper_id),
    ):
        assert TOKEN not in json.dumps(value) and ".git" not in json.dumps(value)

    async def main() -> str:
        async with Client(build_server(ws.svc, ws.pid)) as client:
            reply = await client.call_tool(
                "paper_workspace_read", {"paper_id": ws.paper_id, "path": ".git/config"}
            )
            return str(reply.content[0].text)  # type: ignore[union-attr]

    assert TOKEN not in anyio.run(main)
    cli = Cli(ws.env)
    reply = cli.err("paper", "workspace", "read", ws.paper_id, ".git/config", "--project", ws.pid)
    assert TOKEN not in json.dumps(reply)
    for path in ws.env.store_dir.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()


def test_hub_csp_names_the_worker_source_and_keeps_the_rest() -> None:
    directives = {}
    for part in (p.strip() for p in CSP.split(";")):
        name, _, value = part.partition(" ")
        directives[name] = value
    assert directives["worker-src"] == "'self'"
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'" and directives["style-src"] == "'self'"
    assert "unsafe" not in CSP and "blob:" not in CSP
