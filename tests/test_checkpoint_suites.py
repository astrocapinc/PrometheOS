"""The checkpoint suites file (tools/checkpoint_suites.json): command names are unique per run.

``tools/run_checkpoint.py`` runs every command of every check of one checkpoint into one run
folder, naming its records ``<name>.stdout.txt``, ``<name>.stderr.txt`` and ``<name>.cmd.json``.
Two commands with the same name in one checkpoint would overwrite each other's evidence, so a name
is used once per checkpoint (two checks that need the same gate each name their own).
"""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

from conftest import REPO


def suites() -> dict[str, dict[str, dict[str, list[str]]]]:
    data = json.loads((REPO / "tools" / "checkpoint_suites.json").read_text(encoding="utf-8"))
    return {
        checkpoint: {check: spec["commands"] for check, spec in checks.items()}
        for checkpoint, checks in data["suites"].items()
    }


def test_no_command_name_is_used_twice_within_a_checkpoint() -> None:
    for checkpoint, checks in suites().items():
        users: dict[str, list[str]] = {}
        for check, commands in checks.items():
            for name in commands:
                users.setdefault(name, []).append(check)
        duplicated = {name: found for name, found in users.items() if len(found) > 1}
        assert not duplicated, (
            f"{checkpoint}: command names used by more than one check: {duplicated}"
        )


def test_command_names_are_valid_record_names() -> None:
    # tools/run_check.py: alphanumeric with - or _ (the name is a file name).
    for checkpoint, checks in suites().items():
        for commands in checks.values():
            for name in commands:
                assert name.replace("-", "").replace("_", "").isalnum(), (checkpoint, name)


def test_every_command_as_recorded_matches_the_verifiers_declaration(tmp_path: Path) -> None:
    """Review cp10-browser R3-F1: a command with ``{run_dir}`` is recorded with the run folder
    expanded (tools/run_checkpoint.py) and the verifier expands the declared one the same way, so
    no check of any checkpoint can fail the receipt with "commands differ" (14 did)."""
    import run_checkpoint
    import verify_receipt

    data = (REPO / "tools" / "checkpoint_suites.json").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    placeholders = 0
    for checkpoint, checks in json.loads(data)["suites"].items():
        rel = f"development/receipts/{checkpoint}/{checkpoint.lower()}-run"
        run_dir = tmp_path / rel
        run_dir.mkdir(parents=True)
        (run_dir / "suite.json").write_bytes(data)
        manifest = {"files": [{"path": "tools/checkpoint_suites.json", "sha256": digest}]}
        (run_dir / "tree-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        errors: list[str] = []
        declared = verify_receipt._declared_suite(
            tmp_path,
            rel,
            checkpoint,
            f"{rel}/tree-manifest.json",
            {f"{rel}/suite.json": {"sha256": digest}},
            errors,
        )
        assert declared is not None and errors == [], (checkpoint, errors)
        for check, spec in checks.items():
            commands = spec["commands"].values()
            recorded = [shlex.join(run_checkpoint.expand_command(c, rel)) for c in commands]
            assert sorted(recorded) == sorted(declared[check]), (checkpoint, check)
            assert not any("{run_dir}" in command for command in recorded)
            placeholders += sum(any("{run_dir}" in part for part in c) for c in commands)
    assert placeholders >= 15  # the 14 hub e2e commands and the C10-WALK
