"""Regressions for review cp9-a round 2 (decision 0012 "Review round 2").

R2-1: every workspace path is canonicalised to real long names whether or not it exists;
      8.3 aliases and ``~<digit>`` names that do not exist are refused; the figure writer is
      guarded. R2-2: source import refuses version-control metadata. R2-3: agent-scope Git
      views omit above-scope names; agent commits of above-scope changes are refused; the
      owner's push preview flags restricted files. R2-5: the owner may read non-VCS dot
      files; the external-tool register check is bidirectional and malformed-safe.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env
from cp9_support import LatexEnv
from mcp import Client
from test_cp9_sync import Linked, git
from test_cp9_transports import Cli

from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server

TOKEN = "olp_ROUND2TOKEN55556666777788889999"


def short_alias(path: Path, fallback: str) -> str:
    """The real 8.3 alias of ``path`` when the volume has one, else a literal look-alike
    (which must be refused too: it does not exist as a long name)."""
    if sys.platform == "win32":
        import ctypes

        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)  # type: ignore[attr-defined]
        if size:
            alias = Path(buffer.value).name
            if alias.casefold() != path.name.casefold():
                return alias
    return fallback


def _restricted(ws: LatexEnv, rel: str) -> None:
    plan = ws.svc.import_plan(
        ws.pid,
        OWNER,
        root_alias="research",
        paths=[f"paper/{rel}"],
        retention="copy",
        sensitivity="restricted",
    )
    ws.svc.import_commit(ws.pid, plan["plan_id"], OWNER)


def _denied(call: Any) -> None:
    with pytest.raises(PrometheosError) as refused:
        call()
    assert refused.value.code == "policy_denied"
    assert refused.value.details == {}


@pytest.fixture
def ws(tmp_path: Path) -> LatexEnv:
    return LatexEnv(Env(tmp_path))


# ----- R2-1 --------------------------------------------------------------------------------------


def test_new_files_through_an_alias_of_a_dot_directory_are_refused(ws: LatexEnv) -> None:
    heads = ws.folder / ".git" / "refs" / "heads"
    heads.mkdir(parents=True)
    alias = short_alias(ws.folder / ".git", "GIT~1")
    before = sorted(p.name for p in (ws.folder / ".git").rglob("*"))
    for target in (f"{alias}/evil.cfg", f"{alias}/refs/heads/agent.md"):
        for caller in (AGENT, OWNER):
            _denied(lambda t=target, c=caller: ws.write(t, "x", None, c))
    assert sorted(p.name for p in (ws.folder / ".git").rglob("*")) == before


def test_alias_of_a_parent_cannot_recreate_an_absent_restricted_file(ws: LatexEnv) -> None:
    secret = ws.folder / "secret-dir"
    secret.mkdir()
    (secret / "notes.tex").write_text("restricted notes\n", "utf-8")
    _restricted(ws, "secret-dir/notes.tex")
    (secret / "notes.tex").unlink()
    alias = short_alias(secret, "SECRET~1")
    _denied(lambda: ws.write("secret-dir/notes.tex", "agent bytes", None, AGENT))
    _denied(lambda: ws.write(f"{alias}/notes.tex", "agent bytes", None, AGENT))
    assert not (secret / "notes.tex").exists()
    # The owner may recreate it.
    ws.write("secret-dir/notes.tex", "owner bytes", None, OWNER)
    assert (secret / "notes.tex").read_text("utf-8") == "owner bytes"


def test_missing_short_name_lookalikes_are_refused(ws: LatexEnv) -> None:
    _denied(lambda: ws.write("draft~1.tex", "x", None, OWNER))
    _denied(lambda: ws.write("SECTIO~1/new.tex", "x", None, AGENT))
    assert not (ws.folder / "draft~1.tex").exists()


def test_figure_insert_cannot_recreate_an_absent_restricted_figure(ws: LatexEnv) -> None:
    figure = ws.folder / "figures" / "result.png"
    figure.write_bytes(ws.path("figures/plot.png").read_bytes())
    _restricted(ws, "figures/result.png")
    figure.unlink()
    (ws.env.research / "result.png").write_bytes(ws.path("figures/plot.png").read_bytes())
    plan = ws.svc.import_plan(
        ws.pid,
        AGENT,
        root_alias="research",
        paths=["result.png"],
        retention="copy",
        sensitivity="normal",
    )
    ws.svc.import_commit(ws.pid, plan["plan_id"], AGENT)
    version_id = plan["items"][0]["version_id"]
    _denied(lambda: ws.svc.workspace_insert(ws.pid, AGENT, ws.paper_id, "figure", version_id))
    assert not figure.exists()
    owner = ws.svc.workspace_insert(ws.pid, OWNER, ws.paper_id, "figure", version_id)
    assert owner["file"] == "figures/result.png" and figure.exists()


# ----- R2-2 --------------------------------------------------------------------------------------


def _plant_git_config(ws: LatexEnv) -> None:
    git_dir = ws.folder / ".git"
    git_dir.mkdir(exist_ok=True)
    (git_dir / "config").write_text(
        f'[remote "origin"]\n\turl = https://git:{TOKEN}@git.overleaf.com/abc\n', "utf-8"
    )
    (ws.folder / "gitdir-pointer").mkdir()
    (ws.folder / "gitdir-pointer" / ".git").write_text(f"gitdir: {TOKEN}\n", "utf-8")


def test_source_import_refuses_vcs_metadata_on_every_transport(ws: LatexEnv) -> None:
    _plant_git_config(ws)
    alias = short_alias(ws.folder / ".git", "GIT~1")
    for path in (
        "paper/.git/config",
        "paper/.GIT/config",
        f"paper/{alias}/config",
        "paper/gitdir-pointer/.git",
    ):
        for caller in (AGENT, OWNER):
            _denied(
                lambda p=path, c=caller: ws.svc.import_plan(
                    ws.pid,
                    c,
                    root_alias="research",
                    paths=[p],
                    retention="copy",
                    sensitivity="normal",
                )
            )
    absolute = str(ws.folder / ".git" / "config")
    _denied(
        lambda: ws.svc.import_plan(
            ws.pid,
            OWNER,
            root_alias="research",
            paths=[absolute],
            retention="copy",
            sensitivity="normal",
        )
    )

    async def main() -> tuple[str, Any]:
        async with Client(build_server(ws.svc, ws.pid)) as client:
            reply = await client.call_tool(
                "source_import_plan", {"root_alias": "research", "paths": ["paper/.git/config"]}
            )
            search = await client.call_tool("knowledge_search", {"query": TOKEN})
            text = str(search.content[0].text)  # type: ignore[union-attr]
            return str(reply.content[0].text), json.loads(text)  # type: ignore[union-attr]

    refusal, search = anyio.run(main)
    assert "policy_denied" in refusal and TOKEN not in refusal
    assert search["hits"] == []  # the reply echoes the query; no hit holds the token
    cli = Cli(ws.env)
    reply = cli.err(
        "source", "import", "paper/.git/config", "--plan", "--root", "research",
        "--project", ws.pid,
    )  # fmt: skip
    assert reply["error"]["code"] == "policy_denied" and TOKEN not in json.dumps(reply)
    assert ws.svc.search(ws.pid, AGENT, TOKEN)["hits"] == []
    for stored in ws.env.store_dir.rglob("*"):
        if stored.is_file():
            assert TOKEN.encode() not in stored.read_bytes()


# ----- R2-3 --------------------------------------------------------------------------------------


@pytest.fixture
def linked(tmp_path: Path) -> Linked:
    return Linked(tmp_path)


def _restrict_in_clone(linked: Linked, rel: str) -> None:
    plan = linked.svc.import_plan(
        linked.pid,
        OWNER,
        root_alias="research",
        paths=[f"overleaf/{rel}"],
        retention="copy",
        sensitivity="restricted",
    )
    linked.svc.import_commit(linked.pid, plan["plan_id"], OWNER)


def test_agent_git_views_omit_restricted_names_and_cannot_commit_them(linked: Linked) -> None:
    _restrict_in_clone(linked, "refs.bib")
    bib = linked.folder / "refs.bib"
    bib.write_text(bib.read_text("utf-8") + "% restricted edit\n", "utf-8", newline="\n")
    agent = linked.svc.overleaf_status(linked.pid, AGENT, linked.paper_id)
    assert "refs.bib" not in json.dumps(agent)
    assert agent["scope_notice"] == "changes outside your scope exist: owner review required"
    owner = linked.svc.overleaf_status(linked.pid, OWNER, linked.paper_id)
    assert owner["local_changes"] == ["refs.bib"]
    assert owner["restricted_files"] == [{"path": "refs.bib", "sensitivity": "restricted"}]
    head = git(linked.folder, "rev-parse", "HEAD")
    _denied(lambda: linked.svc.overleaf_commit(linked.pid, AGENT, linked.paper_id, "agent try"))
    assert git(linked.folder, "rev-parse", "HEAD") == head
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "Owner commits the bib")
    owner_push = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    assert owner_push["plan"]["restricted_files"] == [
        {"path": "refs.bib", "sensitivity": "restricted"}
    ]
    agent_push = linked.svc.overleaf_push_preview(linked.pid, AGENT, linked.paper_id)
    assert "refs.bib" not in json.dumps(agent_push)
    assert agent_push["plan"]["scope_notice"] is not None
    # Views differ by scope, the binding digest does not (it covers kind, heads and OIDs).
    assert agent_push["digest"] == owner_push["digest"]
    # An agent-scope view with nothing above scope carries no notice.
    bib.write_text(bib.read_text("utf-8"), "utf-8", newline="\n")
    clean = linked.svc.overleaf_status(linked.pid, AGENT, linked.paper_id)
    assert clean["scope_notice"] is None


# ----- R2-5 --------------------------------------------------------------------------------------


def test_owner_reads_non_vcs_dot_files_and_agents_do_not(ws: LatexEnv) -> None:
    (ws.folder / ".latexmkrc").write_text("$pdf_mode = 5;\n", "utf-8")
    _plant_git_config(ws)
    assert ws.read(".latexmkrc", OWNER)["text"].startswith("$pdf_mode")
    _denied(lambda: ws.read(".latexmkrc", AGENT))
    for caller in (AGENT, OWNER):
        _denied(lambda c=caller: ws.read(".git/config", c))
        _denied(lambda c=caller: ws.write(".latexmkrc", "x", None, c))


def test_tool_register_check_is_bidirectional_and_malformed_safe() -> None:
    import dependency_register as register

    pinned = register.tool_entries()
    assert register.check_tools(pinned) == []
    stale = [*pinned, {**pinned[0], "name": "old-engine", "version": "0.1.0"}]
    assert register.check_tools(stale) == [
        "registered external tool not pinned in the code: old-engine 0.1.0"
    ]
    malformed = [*pinned, {"ecosystem": "binary", "version": 3}]
    # The malformed entry is the last one: its index follows the pinned entries (the desktop
    # core and installer components joined them in CP19.2; decision 0055).
    assert register.check_tools(malformed) == [
        f"malformed external tool entry at packages[{len(pinned)}]"
    ]
    assert register.check_tools(pinned[1:])[0].startswith("external tool not registered")


def test_short_alias_helper_is_exercised() -> None:
    # Documents which variant the alias tests used on this machine (8.3 names on or off).
    probe = Path(os.environ.get("TEMP", ".")).resolve()
    assert isinstance(short_alias(probe, "X~1"), str)
