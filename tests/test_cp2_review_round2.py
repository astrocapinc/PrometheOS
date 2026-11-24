"""Regressions for the CP2 r2 review (round 2) and interactive owner paths."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, FULL, OWNER, Env, create_entry, request
from typer.testing import CliRunner

from prometheos import cli
from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def run_cli(env: Env, *args: str) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, "-m", "prometheos", *args, "--json"],
        env={**os.environ, "PROMETHEOS_HOME": str(env.home), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])


def test_decision_review_is_no_oracle_without_a_terminal(env: Env, tmp_path: Path) -> None:
    item = env.import_files(["meeting.txt"], sensitivity="restricted")["plan"]["items"][0]
    guesses = [
        create_entry(
            evidence=[
                {
                    "source_id": item["source_id"],
                    "version_id": item["version_id"],
                    "locator": {"type": "text", "start_line": 1, "end_line": 1},
                    "quote_sha256": "0" * 64,
                }
            ],
            sensitivity="restricted",
        ),
        create_entry(),  # no protected effects at all
    ]
    outputs = []
    for op in guesses:
        req = tmp_path / f"guess-{len(outputs)}.json"
        req.write_bytes(request(env.project_id, [op], env.seq()))
        code, out = run_cli(env, "decision", "review", "--file", str(req))
        outputs.append((code, out["error"]["code"], out["error"]["message"]))
    assert all(o == outputs[0] for o in outputs) and outputs[0][0] == 4


def _binding(
    item: dict[str, Any], line: int, text: str, needle: str, decimal: str, unit: str
) -> dict[str, Any]:
    start = text.index(needle)
    return {
        "decimal": decimal,
        "unit": unit,
        "source_ref": {
            "source_id": item["source_id"],
            "version_id": item["version_id"],
            "locator": {"type": "text", "start_line": line, "end_line": line},
            "quote_sha256": None,
        },
        "source_pointer": f"text:{line}:{start}:{start + len(needle)}",
    }


def test_round2_boundaries(env: Env) -> None:
    lines = [
        "total 1,240 kJ",
        "decimal 2,40 kJ",
        "fraction 3/4 mm",
        "tol ±0.5 mm",
        "range 1.5-2.0 kJ",
        "plain (2.40 kJ)",
    ]
    (env.research / "r2.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    item = env.import_files(["r2.txt"])["plan"]["items"][0]
    bad = [
        (1, "240 kJ", "240", "kJ"),
        (2, "40 kJ", "40", "kJ"),
        (2, "2", "2", ""),
        (3, "4 mm", "4", "mm"),
        (4, "0.5 mm", "0.5", "mm"),
        (5, "2.0 kJ", "2.0", "kJ"),
    ]
    for line, needle, decimal, unit in bad:
        with pytest.raises(PrometheosError):
            env.apply(
                [
                    create_entry(
                        quantities=[_binding(item, line, lines[line - 1], needle, decimal, unit)]
                    )
                ]
            )
    good = _binding(item, 6, lines[5], "2.40 kJ", "2.40", "kJ")
    env.apply([create_entry(quantities=[good])])
    for text in (
        "Recovered " + chr(0x2212) + "2.40 kJ.",
        "About .5 kJ.",
        "Rate 2.40 kJ/s.",
        "2,40 kJ",
    ):
        with pytest.raises(PrometheosError):
            env.apply([create_entry(origin="generated", text=text, quantities=[good])])
    env.apply([create_entry(origin="generated", text="Recovered (2.40 kJ).", quantities=[good])])


def test_as_of_manifest_reports_purged_records(env: Env) -> None:
    item = env.import_files(["meeting.txt"], sensitivity="restricted")["plan"]["items"][0]
    early = env.seq()
    plan = env.svc.purge_plan(env.project_id, [item["source_id"]], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    ctx = env.svc.context(env.project_id, OWNER, at_seq=early)
    assert ctx["manifest"]["purged_records_omitted"] == 2
    assert env.svc.notebook(env.project_id, OWNER, at_seq=early)["purged_records_omitted"] == 2
    with pytest.raises(PrometheosError):
        env.svc.purge_plan_get(env.project_id, plan["plan_id"], AGENT)


CODES = ("c0ffee", "beef01", "d00d02")


def _owner_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the owner at an interactive terminal; each prompt shows a fresh code."""
    codes = iter(CODES)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: next(codes))


def test_interactive_restore_with_rebind_audits_owner_action(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = Path(env.svc.backup(env.project_id, tmp_path / "b.zip", FULL)["backup"])
    moved = tmp_path / "moved"
    moved.mkdir()
    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    _owner_terminal(monkeypatch)
    target = tmp_path / "restored"
    result = CliRunner().invoke(
        cli.app,
        [
            "backup",
            "restore",
            str(backup),
            "--into",
            str(target),
            "--rebind",
            f"research={moved}",
            "--json",
        ],
        input="c0ffee\n",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["rebind_commit"]["project_seq"] == payload["project_seq"] + 1
    conn = sqlite3.connect(target / "record.sqlite")
    ops = [r[0] for r in conn.execute("SELECT operation FROM audit")]
    conn.close()
    assert "owner_local_action:restore --rebind" in ops
    wrong = CliRunner().invoke(
        cli.app,
        ["backup", "restore", str(backup), "--into", str(tmp_path / "r2"), "--json"],
        input="nope\n",
    )
    assert wrong.exit_code == 4 and not (tmp_path / "r2").exists()


def test_interactive_decision_review_applies_exact_request(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = new_id("entry")
    env.apply([create_entry(note)])
    req = tmp_path / "archive.json"
    req.write_bytes(
        request(env.project_id, [{"op": "archive", "id": note, "base_rev": 1}], env.seq())
    )
    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    _owner_terminal(monkeypatch)
    result = CliRunner().invoke(
        cli.app,
        ["decision", "review", "--file", str(req), "--apply", "--json"],
        input="c0ffee\nbeef01\n",
    )
    assert result.exit_code == 0, result.output
    # The protected effects and digest are shown before the code that approves them.
    shown = result.output.index("Protected effects:")
    assert shown < result.output.index("To apply exactly this request, type beef01")
    assert "Request digest: " in result.output[shown:]
    assert env.svc.get(env.project_id, OWNER, note, None)["lifecycle"] == "archived"


def _capability_count(env: Env) -> int:
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        return int(conn.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0])
    finally:
        conn.close()


def test_interactive_decision_review_wrong_second_code_grants_nothing(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = new_id("entry")
    env.apply([create_entry(note)])
    req = tmp_path / "archive.json"
    req.write_bytes(
        request(env.project_id, [{"op": "archive", "id": note, "base_rev": 1}], env.seq())
    )
    before = _capability_count(env)
    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    _owner_terminal(monkeypatch)
    for extra in ([], ["--apply"]):
        result = CliRunner().invoke(
            cli.app,
            ["decision", "review", "--file", str(req), *extra, "--json"],
            input="c0ffee\nc0ffee\n",  # replays the first code
        )
        assert result.exit_code == 4, result.output
        assert "Protected effects:" in result.output
        assert "Capability secret" not in result.output
        _owner_terminal(monkeypatch)
    assert _capability_count(env) == before
    assert env.svc.get(env.project_id, OWNER, note, None)["lifecycle"] != "archived"


def test_round3_start_side_boundaries(env: Env) -> None:
    lines = [
        "ratio 3:4 mm",
        "clock 10:30",
        "swiss 1'240 kJ",
        "thin 1" + chr(0x2009) + "240 kJ",
        "spaced 1 240 kJ",
        "bound <0.5 mm",
        "about ~2.0 kJ",
        "assign x=2.40 kJ;",
        "list 2.40 kJ, then more",
    ]
    (env.research / "r3.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    item = env.import_files(["r3.txt"])["plan"]["items"][0]
    bad = [
        (1, "4 mm", "4", "mm"),
        (2, "30", "30", ""),
        (3, "240 kJ", "240", "kJ"),
        (4, "240 kJ", "240", "kJ"),
        (5, "240 kJ", "240", "kJ"),
        (6, "0.5 mm", "0.5", "mm"),
        (7, "2.0 kJ", "2.0", "kJ"),
    ]
    for line, needle, decimal, unit in bad:
        binding = _binding(item, line, lines[line - 1], needle, decimal, unit)
        with pytest.raises(PrometheosError):
            env.apply([create_entry(quantities=[binding])])
    for line in (8, 9):
        good = _binding(item, line, lines[line - 1], "2.40 kJ", "2.40", "kJ")
        env.apply([create_entry(quantities=[good])])


def test_next_steps_as_of_reports_purged_records(env: Env) -> None:
    item = env.import_files(["meeting.txt"], sensitivity="restricted")["plan"]["items"][0]
    early = env.seq()
    assert env.svc.next_steps(env.project_id, OWNER)["purged_records_omitted"] == 0
    plan = env.svc.purge_plan(env.project_id, [item["source_id"]], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    steps = env.svc.next_steps(env.project_id, OWNER, at_seq=early)
    assert steps["purged_records_omitted"] == 2


def test_interactive_decision_review_wrong_code_computes_no_preview(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app.service import Service

    note = new_id("entry")
    env.apply([create_entry(note)])
    req = tmp_path / "archive.json"
    req.write_bytes(
        request(env.project_id, [{"op": "archive", "id": note, "base_rev": 1}], env.seq())
    )
    calls: list[str] = []
    original = Service.review_request

    def spy(self: Service, *args: Any, **kwargs: Any) -> Any:
        calls.append("review")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Service, "review_request", spy)
    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    _owner_terminal(monkeypatch)
    result = CliRunner().invoke(
        cli.app, ["decision", "review", "--file", str(req), "--apply", "--json"], input="nope\n"
    )
    assert result.exit_code == 4, result.output
    assert calls == [] and "Protected effects" not in result.output
    assert env.svc.get(env.project_id, OWNER, note, None)["lifecycle"] != "archived"
