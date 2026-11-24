"""MATLAB parsing is an optional extra (decision 0053): with ``adoption-parsers`` installed the
``code.matlab`` analyzer runs; without it, adoption runs the analyzers that need no parser and
says so the same way on every surface (the scan's analysis summary over the service, MCP and the
CLI; the analyzer list; the hub card in ``e2e/20-adoption.spec.ts``). The parser is hidden by
making its import fail, so the real detection runs. Synthetic data only; no model, no network."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER
from test_cp10_mcp_paths import tool
from test_cp17_review_r2 import SWEEPS, scan_tree

from prometheos.adoption import matlab

FILES: dict[str, str | bytes] = {
    **SWEEPS,
    "code/run_sweep.m": "% the sweep\nresults = readtable('results/sweep_v3.csv');\n"
    "plot_it(results);\n",
    "code/plot_it.m": "function plot_it(r)\n% plot one sweep\nend\n",
    "notes/log.md": "# Log\n\nresults/sweep_v3.csv is the final sweep.\n",
}
MATLAB_FILES = 2
REASON = "the MATLAB parser is not installed (the adoption-parsers extra)"
INSTALL = "install prometheos[adoption-parsers] and scan again"
UNAVAILABLE = [
    {"analyzer": "code.matlab", "reason": REASON, "install": INSTALL, "files": MATLAB_FILES}
]


@pytest.fixture
def no_parser(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An installation without the extra: importing either package fails."""
    matlab.parser_problem.cache_clear()
    for name in ("tree_sitter", "tree_sitter_matlab"):
        monkeypatch.setitem(sys.modules, name, None)
    yield
    matlab.parser_problem.cache_clear()


def _rescan(world: dict[str, Any]) -> dict[str, Any]:
    env = world["env"]
    scan = env.svc.adoption_scan(env.project_id, AGENT, root_alias="tree", mode="wait")
    assert scan["state"] == "complete", scan
    world["gen"] = scan["scan_gen"]
    return scan


def _by_analyzer(world: dict[str, Any]) -> dict[str, int]:
    env = world["env"]
    rows = env.svc.adoption_evidence(env.project_id, OWNER, world["gen"], limit=500)["evidence"]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["analyzer"]] = counts.get(row["analyzer"], 0) + 1
    return counts


def _summary(world: dict[str, Any]) -> dict[str, Any]:
    env = world["env"]
    scans = env.svc.adoption_scans(env.project_id, OWNER)["scans"]
    found: dict[str, Any] = next(s for s in scans if s["scan_gen"] == world["gen"])["analysis"]
    return found


# ----- with the extra (the test environment has it; this fails, never skips, without it) -------


def test_with_the_extra_matlab_code_is_parsed_and_nothing_is_reported_missing(
    tmp_path: Path,
) -> None:
    assert matlab.parser_problem() is None, "the test environment must have the parser"
    structure = matlab.parse_matlab(b"x = 1;\ny = f(x);\n")
    assert structure.parsed and structure.language == "matlab"
    world = scan_tree(tmp_path, FILES)
    assert _summary(world)["analyzers_unavailable"] == []
    assert _by_analyzer(world).get("code.matlab", 0) > 0
    env = world["env"]
    assert env.svc.adoption_analyzers(env.project_id, AGENT)["unavailable"] == []


# ----- without the extra ----------------------------------------------------------------------


def test_without_the_extra_the_parser_is_reported_and_no_code_is_parsed(no_parser: None) -> None:
    assert matlab.parser_problem() == REASON
    structure = matlab.parse_matlab(b"% a script\nx = 1;\n")
    assert structure.language == "matlab" and not structure.parsed, "detected, not parsed"
    # A script without comment, function or classdef lines needs the parser to be recognised.
    assert matlab.language_of("x = 1;\ny = x + 2;\n") == "unknown"
    # The readers that need no parser still work.
    assert matlab.read_mdl('Model {\n  Name "m"\n}\n').model_name == "m"


def test_a_scan_without_the_extra_says_so_on_every_surface(
    tmp_path: Path, no_parser: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from prometheos import cli

    world = scan_tree(tmp_path, FILES)
    env = world["env"]
    # Service: the summary names the analyzer, why, how to add it and the files it would read.
    assert _summary(world)["analyzers_unavailable"] == UNAVAILABLE
    counts = _by_analyzer(world)
    assert "code.matlab" not in counts, "no evidence from an analyzer that did not run"
    assert counts.get("name.conventions", 0) > 0 and counts.get("text.statements", 0) > 0
    listed = env.svc.adoption_analyzers(env.project_id, AGENT)
    assert listed["unavailable"] == [{**UNAVAILABLE[0], "files": 0}]
    assert "code.matlab" in {a["id"] for a in listed["analyzers"]}, "still declared"
    # MCP: the same documents.
    failed, text = tool(env.svc, env.project_id, "adoption_read", {"request": {"op": "scans"}})
    assert not failed
    scans = json.loads(text)["result"]["scans"]
    assert (
        next(s for s in scans if s["scan_gen"] == world["gen"])["analysis"]["analyzers_unavailable"]
        == UNAVAILABLE
    )
    failed, text = tool(env.svc, env.project_id, "adoption_read", {"request": {"op": "analyzers"}})
    assert not failed and json.loads(text)["result"]["unavailable"] == listed["unavailable"]
    # CLI: the same JSON, and a note on stderr for a person reading the terminal.
    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    runner = CliRunner()
    done = runner.invoke(cli.app, ["adopt", "scans", "--project", env.project_id, "--json"])
    assert done.exit_code == 0, done.output
    shown = json.loads(done.stdout)["scans"]
    assert (
        next(s for s in shown if s["scan_gen"] == world["gen"])["analysis"]["analyzers_unavailable"]
        == UNAVAILABLE
    )
    done = runner.invoke(cli.app, ["adopt", "analyzers", "--project", env.project_id])
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["unavailable"] == listed["unavailable"]
    assert f"note: code.matlab did not run: {REASON}; {INSTALL}" in done.stderr
    done = runner.invoke(cli.app, ["adopt", "analyzers", "--project", env.project_id, "--json"])
    assert done.stderr == "", "JSON mode keeps stderr quiet; the document carries the list"
    # A scan from the CLI notes it too, and so does the list of scans, once per analyzer
    # (review cp17-0053-a F2, F4).
    note = f"note: code.matlab did not run: {REASON}; {INSTALL}"
    done = runner.invoke(cli.app, ["adopt", "scan", "--root", "tree", "--project", env.project_id])
    assert done.exit_code == 0, done.output
    scanned = json.loads(done.stdout)
    assert scanned["state"] == "complete"
    assert scanned["analysis"]["analyzers_unavailable"] == UNAVAILABLE
    assert note in done.stderr
    done = runner.invoke(cli.app, ["adopt", "scans", "--project", env.project_id])
    assert done.exit_code == 0, done.output
    assert done.stderr.count(note) == 1, done.stderr


def test_the_changes_view_and_the_vault_note_say_what_was_not_analysed(
    tmp_path: Path, no_parser: None
) -> None:
    """Review cp17-0053-a F3: drift (service, MCP) and the exported Markdown note."""
    from cp1_support import OWNER_ACT

    from prometheos.adoption import transfer

    world = scan_tree(tmp_path, FILES)
    env = world["env"]
    (world["root"] / "notes" / "log.md").write_text("# Log\n\nMore runs.\n", "utf-8")
    _rescan(world)
    drift = env.svc.adoption_drift(env.project_id, OWNER, world["gen"])
    assert drift["analysis"]["analyzers_unavailable"] == UNAVAILABLE
    failed, text = tool(
        env.svc, env.project_id, "adoption_read",
        {"request": {"op": "drift", "scan_gen": world["gen"]}},
    )  # fmt: skip
    assert not failed
    assert json.loads(text)["result"]["analysis"]["analyzers_unavailable"] == UNAVAILABLE
    baseline = env.svc.adoption_baseline_accept(env.project_id, world["gen"], authority=OWNER_ACT)
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        block = transfer.markdown_block(conn, baseline["baseline_id"])
    finally:
        conn.close()
    assert (
        f"Not analysed by code.matlab: {REASON}; {MATLAB_FILES} file(s) were inventoried but "
        f"not parsed ({INSTALL})."
    ) in block


def test_a_folder_without_matlab_files_loses_nothing_and_says_nothing(
    tmp_path: Path, no_parser: None
) -> None:
    world = scan_tree(tmp_path, dict(SWEEPS))
    assert _summary(world)["analyzers_unavailable"] == []
    env = world["env"]
    # the installation still lacks the parser, which the analyzer list says
    assert env.svc.adoption_analyzers(env.project_id, AGENT)["unavailable"] != []


def test_installing_the_extra_later_analyses_the_code_and_old_outputs_are_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the parser: code.matlab outputs are cached.
    world = scan_tree(tmp_path, FILES)
    parsed = _by_analyzer(world).get("code.matlab", 0)
    assert parsed > 0
    # Without it: the cache is not replayed for an analyzer that cannot run here.
    matlab.parser_problem.cache_clear()
    for name in ("tree_sitter", "tree_sitter_matlab"):
        monkeypatch.setitem(sys.modules, name, None)
    try:
        _rescan(world)
        assert _summary(world)["analyzers_unavailable"] == UNAVAILABLE
        assert "code.matlab" not in _by_analyzer(world)
        # the newest scan is the degraded one: the CLI notes it (review cp17-0053-a-r3 R3-1)
        from typer.testing import CliRunner

        from prometheos import cli

        monkeypatch.setenv("PROMETHEOS_HOME", str(world["env"].home))
        done = CliRunner().invoke(cli.app, ["adopt", "scans", "--project", world["env"].project_id])
        assert done.exit_code == 0 and "note: code.matlab did not run" in done.stderr, done.output
    finally:
        monkeypatch.undo()
        matlab.parser_problem.cache_clear()
    # With the parser again: the code is analysed, the summary is clear.
    _rescan(world)
    assert _summary(world)["analyzers_unavailable"] == []
    assert _by_analyzer(world).get("code.matlab", 0) == parsed
    # The CLI's list of scans speaks for the newest scan of each folder: the older degraded scan
    # keeps its record, but no note says the parser is missing now (review cp17-0053-a-r2 R2-1).
    from typer.testing import CliRunner

    from prometheos import cli

    env = world["env"]
    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    done = CliRunner().invoke(cli.app, ["adopt", "scans", "--project", env.project_id])
    assert done.exit_code == 0, done.output
    listed = json.loads(done.stdout)["scans"]
    assert [s["analysis"]["analyzers_unavailable"] != [] for s in listed] == [False, True, False]
    assert "did not run" not in done.stderr
