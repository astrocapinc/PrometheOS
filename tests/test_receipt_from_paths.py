"""A receipt recorded from file paths (decision 0112): each path is pinned to its newest recorded
version while the file still has that digest; a file not recorded, or changed since, is refused
with the list to import first, and the receipt makes generated numbers traced to their run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env
from cp7_support import create, paper_body

from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError

RUN = {
    "reporter": "Claude Code (test)",
    "command": "matlab -batch make_report_data",
    "working_root": "research",
    "environment": {"os": "TestOS", "tools": [{"name": "MATLAB", "version": "R2024b"}]},
    "started_at": None,
    "ended_at": None,
    "exit_status": 0,
    "outcome": "succeeded",
    "limitations": ["synthetic test"],
}
PAPER = (
    "\\documentclass{article}\n\\input{generated/macros}\n\\begin{document}\n"
    "Benefit \\Ben~\\%.\n\\end{document}\n"
)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    (e.research / "generated").mkdir()
    (e.research / "make_report_data.m").write_text("% generator\n", "utf-8")
    (e.research / "campaign.mat").write_bytes(b"MATLAB 5.0 MAT-file synthetic" * 20)
    (e.research / "generated" / "macros.tex").write_text("\\newcommand{\\Ben}{67}\n", "utf-8")
    (e.research / "paper.tex").write_text(PAPER, "utf-8")
    return e


def _at(path: str, role: str) -> dict[str, str]:
    return {"root_alias": "research", "path": path, "role": role}


INPUTS = [_at("make_report_data.m", "script"), _at("campaign.mat", "data")]
OUTPUTS = [_at("generated/macros.tex", "macros")]


def _record(env: Env, caller: Any = AGENT, rid: str | None = None) -> dict[str, Any]:
    return env.svc.receipt_from_paths(
        env.project_id,
        run=RUN,
        inputs=INPUTS,
        outputs=OUTPUTS,
        title="make_report_data",
        description="",
        request_id=rid or new_request_id(),
        caller=caller,
    )


def test_a_run_by_paths_pins_versions_and_traces_the_paper(env: Env) -> None:
    env.import_files(["make_report_data.m", "campaign.mat", "generated/macros.tex", "paper.tex"])
    rid = new_request_id()
    got = _record(env, rid=rid)
    assert got["commit"]["ok"] and not got["commit"]["replayed"]
    assert [a["path"] for a in got["inputs"]] == ["make_report_data.m", "campaign.mat"]
    assert got["outputs"][0]["role"] == "macros"
    # A retry with the same request ID replays; nothing new is written.
    assert _record(env, rid=rid)["commit"]["replayed"]
    # The paper's macro now traces to this run.
    manuscript = env.svc.import_plan(
        env.project_id, root_alias="research", paths=["paper.tex"], retention="copy",
        sensitivity="normal",
    )["items"][0]  # fmt: skip
    pid = new_id("paper")
    env.apply([create("paper", paper_body(manuscript_source_id=manuscript["source_id"]), pid)])
    numbers = env.svc.paper_numbers(env.project_id, OWNER, pid)
    assert numbers["counts"]["traced_run"] == 1
    assert numbers["macro_files"][0]["runs"][0]["receipt_title"] == "make_report_data"


def test_unrecorded_and_changed_files_are_listed_and_nothing_is_written(env: Env) -> None:
    from test_cp18_events import store_digest

    env.import_files(["make_report_data.m", "generated/macros.tex"])
    (env.research / "make_report_data.m").write_text("% generator, edited\n", "utf-8")
    before = store_digest(env.store_dir)
    with pytest.raises(PrometheosError) as refused:
        _record(env)
    assert refused.value.code == "validation_error"
    assert refused.value.details["needs_import"] == [
        {"root_alias": "research", "path": "make_report_data.m", "reason": "changed_on_disk"},
        {"root_alias": "research", "path": "campaign.mat", "reason": "not_recorded"},
    ]
    assert store_digest(env.store_dir) == before


def test_a_restricted_file_is_refused_to_an_agent_without_naming_it(env: Env) -> None:
    env.import_files(["make_report_data.m", "generated/macros.tex"])
    env.import_files(["campaign.mat"], sensitivity="restricted")
    with pytest.raises(PrometheosError) as refused:
        _record(env)
    assert refused.value.code == "policy_denied" and "campaign" not in str(refused.value.details)


def test_an_unapproved_root_is_refused(env: Env) -> None:
    with pytest.raises(PrometheosError) as refused:
        env.svc.receipt_from_paths(
            env.project_id,
            run=RUN,
            inputs=[{"root_alias": "elsewhere", "path": "x.m", "role": "script"}],
            outputs=[],
            title="x",
            description="",
            request_id=new_request_id(),
        )
    assert refused.value.code == "policy_denied"


# ----- review frame-rerun-a round 1 regressions ------------------------------------------


def test_f3_a_file_listed_twice_is_refused(env: Env) -> None:
    env.import_files(["make_report_data.m", "campaign.mat", "generated/macros.tex"])
    twice = [_at("campaign.mat", "data"), _at("CAMPAIGN.mat", "data")]
    with pytest.raises(PrometheosError) as refused:
        env.svc.receipt_from_paths(
            env.project_id, run=RUN, inputs=twice, outputs=[], title="x", description="",
            request_id=new_request_id(),
        )  # fmt: skip
    assert refused.value.code == "validation_error"
    # The same file as an input and an output stays allowed (a script that rewrites itself).
    got = env.svc.receipt_from_paths(
        env.project_id, run=RUN, inputs=[_at("campaign.mat", "data")],
        outputs=[_at("campaign.mat", "data")], title="x", description="",
        request_id=new_request_id(),
    )  # fmt: skip
    assert got["commit"]["ok"]


def test_f7_retry_after_import_newest_version_and_hidden_newest(env: Env) -> None:
    env.import_files(["make_report_data.m", "generated/macros.tex"])
    rid = new_request_id()
    with pytest.raises(PrometheosError):
        _record(env, rid=rid)  # campaign.mat is not recorded yet
    env.import_files(["campaign.mat"])
    first = _record(env, rid=rid)  # the skill's retry: same request ID after the import
    assert not first["commit"]["replayed"] and _record(env, rid=rid)["commit"]["replayed"]
    # A newer version is pinned once it is imported.
    (env.research / "campaign.mat").write_bytes(b"MATLAB 5.0 MAT-file rerun" * 20)
    env.import_files(["campaign.mat"])
    newer = _record(env)["inputs"][1]["version_id"]
    assert newer != first["inputs"][1]["version_id"]
    # A restricted newest version of a visible source is refused to the agent, recorded by
    # the owner.
    (env.research / "campaign.mat").write_bytes(b"MATLAB 5.0 MAT-file secret" * 20)
    env.import_files(["campaign.mat"], sensitivity="restricted")
    with pytest.raises(PrometheosError) as refused:
        _record(env)
    assert refused.value.code == "policy_denied" and refused.value.details == {}
    assert _record(env, caller=OWNER)["commit"]["ok"]


@pytest.mark.parametrize(
    "spec",
    ['{"inputs": []}', '{"run": {}, "inputs": "x"}', "[1, 2]", '{"run": {}, "inputs": [{}]}'],
)
def test_f1_the_cli_validates_like_mcp(
    env: Env, spec: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from prometheos import cli

    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    path = tmp_path / "spec.json"
    path.write_text(spec, "utf-8")
    result = CliRunner().invoke(
        cli.app,
        ["source", "receipt-paths", "--file", str(path), "--title", "x", "--request-id",
         new_request_id(), "--project", env.project_id, "--json"],
    )  # fmt: skip
    assert result.exit_code != 0
    assert '"validation_error"' in result.output and "internal error" not in result.output
