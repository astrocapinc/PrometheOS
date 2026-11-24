"""MCP agent replies carry no absolute local path (decision 0012 item 47; decision 0022 item 2).

CP5's claim covered the hub's HTTP replies only. The MCP exposures that decision listed are
closed here and tested: unredacted error envelopes, Overleaf Git ``details.stderr``,
``transcription_status.error`` text, ``project_info.store`` and a local-path remote in
``overleaf_status`` and both previews. Every operation family sweeps clean on a world whose
folders all sit under a recognisable marker.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp10_world import World, running_routes
from mcp import Client
from test_cp10_parity_transports import STEPS, run_script

from prometheos import agentops
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server, call_with
from prometheos.mcp_schemas import Out

MARKER = "McpPathMarkerQ7Z"
DRIVE = re.compile(r"(?<![\w/])[A-Za-z]:[\\/]")


def tool(env_svc: Any, project_id: str, name: str, args: dict[str, Any]) -> tuple[bool, str]:
    async def go() -> Any:
        async with Client(build_server(env_svc, project_id)) as client:
            return await client.call_tool(name, args)

    result = anyio.run(go)
    text = json.dumps(result.structured_content) if not result.is_error else result.content[0].text
    return bool(result.is_error), text


def clean(label: str, text: str) -> None:
    assert MARKER.casefold() not in text.casefold(), f"{label} carries a local path: {text[:300]}"
    assert not DRIVE.search(text.replace("\\\\", "\\")), f"{label} carries a drive path"


@pytest.fixture
def marked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("PROMETHEOS_TEX_ENGINE_DIR", "PROMETHEOS_MODELS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROMETHEOS_ENABLE_SYNTHETIC_ENGINE", "1")
    monkeypatch.setenv("PROMETHEOS_CAPTURE_BACKEND", "synthetic")
    base = tmp_path / MARKER
    base.mkdir()
    return base


def test_project_info_names_the_store_by_label(marked: Path) -> None:
    world = World(marked)
    assert MARKER in str(world.env.store_dir)
    failed, text = tool(world.svc, world.pid, "project_info", {})
    assert not failed
    clean("project_info", text)
    assert json.loads(text)["store"] == "<project store>"


def test_every_operation_family_sweeps_clean_over_mcp(marked: Path) -> None:
    """The scripted agent session of the parity test, through MCP, on a marked world: no
    result and no error carries the marker or a drive path."""
    world = World(marked)
    with running_routes(world) as routes:
        seen = run_script(world, routes.mcp, "mcp")
    assert len(seen) >= len(STEPS) - 1
    for label, outcome in seen.items():
        clean(label, json.dumps(outcome))
    # The sweep reached real data and real errors, not only empty answers.
    assert sum(1 for o in seen.values() if o["ok"]) > 40
    assert sum(1 for o in seen.values() if not o["ok"]) > 10


def test_error_envelopes_and_details_are_scrubbed(marked: Path) -> None:
    world = World(marked)
    root = str(world.root)
    # A refused operand whose message and details name the folder.
    failed, text = tool(
        world.svc,
        world.pid,
        "skills_manage",
        {
            "request": {
                "op": "create",
                "name": "new-skill",
                "description": "x",
                "license": "MIT",
                "root_alias": "research",
                "folder": "exports",
            }
        },
    )
    assert not failed
    failed, text = tool(
        world.svc,
        world.pid,
        "skills_manage",
        {
            "request": {
                "op": "create",
                "name": "new-skill",
                "description": "x",
                "license": "MIT",
                "root_alias": "research",
                "folder": "exports",
            }
        },
    )
    assert failed
    clean("create twice", text)
    assert root not in text and "<research root research>" in text


def test_a_service_error_that_quotes_a_path_is_rewritten(marked: Path) -> None:
    world = World(marked)
    secret_path = str(world.root / "Private Data" / "run 1.csv")
    original = type(world.svc).skill_catalog

    def raises(self: Any) -> Any:
        raise PrometheosError(
            "io_error",
            f"cannot read '{secret_path}' and then {world.root}\\other.txt",
            {"stderr": f"fatal: could not read {secret_path}", "path": secret_path},
        )

    type(world.svc).skill_catalog = raises  # type: ignore[method-assign]
    try:
        failed, text = tool(world.svc, world.pid, "skills_read", {"request": {"op": "catalog"}})
    finally:
        type(world.svc).skill_catalog = original  # type: ignore[method-assign]
    assert failed
    clean("service error", text)
    envelope = json.loads(text[text.index("{") :])["error"]
    assert envelope["code"] == "io_error" and "<research root research>" in envelope["message"]


def test_an_unexpected_exception_is_a_plain_internal_error(marked: Path) -> None:
    """Exception text often holds a path (a file name, a folder): none of it is sent."""
    world = World(marked)
    original = type(world.svc).skill_catalog

    def crashes(self: Any) -> Any:
        raise RuntimeError("No such file " + str(world.root / "missing" / "x.txt"))

    type(world.svc).skill_catalog = crashes  # type: ignore[method-assign]
    try:
        failed, text = tool(world.svc, world.pid, "skills_read", {"request": {"op": "catalog"}})
    finally:
        type(world.svc).skill_catalog = original  # type: ignore[method-assign]
    assert failed
    clean("unexpected exception", text)
    envelope = json.loads(text[text.index("{") :])["error"]
    assert envelope["code"] == "io_error"
    # None of the exception's own text is sent, not even the part that is no path.
    assert envelope["message"] == "internal error in the MCP server"
    assert "No such file" not in text and "missing" not in text


def test_a_file_exception_is_a_typed_path_safe_io_error(marked: Path) -> None:
    """The explicit OSError mapping preserves its safe reason and conceals its filename."""
    world = World(marked)
    original = type(world.svc).skill_catalog

    def crashes(self: Any) -> Any:
        raise FileNotFoundError(2, "No such file", str(world.root / "missing" / "x.txt"))

    type(world.svc).skill_catalog = crashes  # type: ignore[method-assign]
    try:
        failed, text = tool(world.svc, world.pid, "skills_read", {"request": {"op": "catalog"}})
    finally:
        type(world.svc).skill_catalog = original  # type: ignore[method-assign]
    assert failed
    clean("typed file exception", text)
    envelope = json.loads(text[text.index("{") :])["error"]
    assert envelope["code"] == "io_error"
    assert envelope["message"] == "file system error: No such file"
    assert envelope["details"] == {}
    assert "missing" not in text and "x.txt" not in text


def test_error_fields_of_a_result_are_scrubbed_but_content_is_not(marked: Path) -> None:
    """``transcription_status.error`` style text is diagnostic; researcher content is not
    rewritten (the same rule as the hub: a note that is a path comes back as it is)."""
    world = World(marked)
    from prometheos.app.pathscrub import build_redactor

    redactor = build_redactor(world.svc, world.pid)
    path = str(world.root / "paper" / "main.tex")

    class Row(Out):
        error: str
        title: str
        store: str

    row = call_with(
        lambda: {"error": f"engine failed on '{path}'", "title": path, "store": path},
        Row,
        redactor,
    )
    assert MARKER not in row.error and "<research root research>" in row.error
    assert row.store.startswith("<research root research>")  # a path field keeps its shape
    assert row.title == path  # content passes byte for byte


def test_local_path_remotes_of_overleaf_are_labelled(marked: Path) -> None:
    from test_cp9_sync import Linked

    linked = Linked(marked)
    assert MARKER in str(linked.remote.bare)
    linked.remote.edit("main.tex", "settling time", "settling time (revised)", "Collaborator")
    for name, args in [
        ("overleaf_status", {"paper_id": linked.paper_id}),
        ("overleaf_pull_preview", {"paper_id": linked.paper_id}),
        ("overleaf_push_preview", {"paper_id": linked.paper_id}),
        ("paper_workspace_files", {"paper_id": linked.paper_id}),
    ]:
        failed, text = tool(linked.svc, linked.pid, name, args)
        clean(name, text)
        assert not failed, (name, text[:200])


def test_git_stderr_in_an_error_is_scrubbed(marked: Path) -> None:
    from test_cp9_sync import Linked

    linked = Linked(marked)
    # Break the remote so the fetch fails with Git's own message, which names the path.
    (linked.remote.bare / "HEAD").unlink()
    failed, text = tool(
        linked.svc, linked.pid, "overleaf_pull_preview", {"paper_id": linked.paper_id}
    )
    clean("pull preview with a missing remote", text)
    assert failed and "stderr" in text


def test_the_family_tools_cover_the_operation_table() -> None:
    agentops.load()
    assert all(a.sub for a in agentops.OPS.values() if a.tool)
    assert set(agentops.FAMILIES) >= {a.tool for a in agentops.OPS.values() if a.tool}
