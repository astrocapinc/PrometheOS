"""Run a checkpoint's suite into ``development/receipts/<CP>/<run-id>/``.

Writes the candidate tree manifest first, then the environment report, then
every suite command through tools/run_check.py, and finally a
``receipt-plan.json`` draft. The implementer reviews summaries/limitations in
the plan, an independent reviewer writes ``review.json``, and
tools/build_receipt.py assembles the immutable receipt.

The suite's one argv placeholder, ``{run_dir}``, becomes the run folder as a
repository-relative POSIX path (``development/receipts/<CP>/<run-id>``): commands
run in the repository root, and tools/verify_receipt.py expands the declared
command with the receipt's own run folder before comparing, so the recorded
command is the same on every machine and names exactly this run. A browser
command's boundary evidence (``<folder>/browser-boundary.json`` under a
``{run_dir}/<folder>`` argument, decision 0028) is listed in the plan as the
check's extra evidence, so the receipt binds it by digest.

Reuse within one integration run (decision 0037): with ``--reuse-from`` naming earlier
run folders of the same integration run, a command whose expanded argv equals an
earlier record's, on the same tree digest and with exit code 0, is not run again. Its
record and outputs are copied under this command's name, byte for byte apart from the
record's ``name`` and output file names, and the record gains ``reused_from`` (the
source record's repository-relative path). A command naming this run's folder
(``{run_dir}``) never matches, so browser, walk and other run-specific evidence always
runs fresh. A failed or differently bound record is never reused.

Usage: uv run python tools/run_checkpoint.py CP0 RUN_ID [--implementer ID]
       [--reuse-from development/receipts/CPn/<run-id> ...]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tree_manifest

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "{run_dir}"
ENV_REPORT = ["uv", "run", "--offline", "--no-sync", "python", "tools/env_report.py"]
BOUNDARY_EVIDENCE = "browser-boundary.json"


def expand_command(command: list[str], run_dir: str) -> list[str]:
    """Expand the only suite placeholder in argv, without a shell or interpolation of code.

    ``run_dir`` is the repository-relative POSIX run folder, never an absolute path.
    """
    return [argument.replace(PLACEHOLDER, run_dir) for argument in command]


def reusable_records(folders: list[str], tree: str) -> dict[tuple[str, ...], Path]:
    """Passing, tree-bound records of earlier run folders, by argv (first folder wins)."""
    found: dict[tuple[str, ...], Path] = {}
    for folder in folders:
        for path in sorted((ROOT / folder).glob("*.cmd.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                record.get("kind") == "prometheos-command-record"
                and record.get("exit_code") == 0
                and record.get("tree_sha256") == tree
                and record.get("name") != "env"
                and "reused_from" not in record
            ):
                found.setdefault(tuple(record["argv"]), path)
    return found


def reuse(source: Path, run_dir: Path, name: str) -> None:
    """Copy ``source`` (a passing record of this tree) into ``run_dir`` as command ``name``."""
    record = json.loads(source.read_text(encoding="utf-8"))
    for stream in ("stdout", "stderr"):
        data = (source.parent / record[stream]["file"]).read_bytes()
        target = f"{name}.{stream}.txt"
        (run_dir / target).write_bytes(data)
        record[stream]["file"] = target
    record["name"] = name
    record["reused_from"] = source.relative_to(ROOT).as_posix()
    (run_dir / f"{name}.cmd.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"[run_checkpoint] {name}: reused {record['reused_from']} (exit 0, same tree)")


def boundary_evidence(command: list[str], run_dir: Path) -> dict[str, str]:
    """Browser boundary evidence a command left in a folder of this run that its argv names."""
    found: dict[str, str] = {}
    for argument in command:
        _, placeholder, rest = argument.partition(PLACEHOLDER + "/")
        if not placeholder or not rest:
            continue
        name = f"{rest.rstrip('/')}/{BOUNDARY_EVIDENCE}"
        if (run_dir / name).is_file():
            found[name] = "browser boundary evidence (decision 0028)"
    return found


def code_under_test(run_dir: Path) -> str | None:
    """A reason to stop, or None: the suite must test the candidate's own ``src`` through an
    environment without PYTHONPATH (decision 0051; the session-10 launcher's records did not
    show which checkout's code ran)."""
    try:
        report = json.loads((run_dir / "env.stdout.txt").read_text(encoding="utf-8"))
        tested = report["code_under_test"]
    except (OSError, ValueError, KeyError, TypeError):
        return "the environment report does not state the code under test"
    if not isinstance(tested, dict):  # review s12-launcher-a F4
        return "the environment report's code under test is not an object"
    if tested.get("package_in_candidate") is not True:
        return f"prometheos was imported from {tested.get('prometheos_package')}, not src/"
    if tested.get("pythonpath"):
        return f"PYTHONPATH is set ({tested['pythonpath']}); the venv alone selects the code"
    if tested.get("environment") != ".venv":
        return f"the interpreter's environment is {tested.get('environment')}, not the candidate's"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a checkpoint suite.")
    parser.add_argument("checkpoint")
    parser.add_argument("run_id")
    parser.add_argument("--implementer", default="claude-opus-5-5-primary")
    parser.add_argument("--only", action="append", help="limit to these check IDs")
    parser.add_argument(
        "--reuse-from",
        nargs="*",
        default=[],
        help="earlier run folders of the same integration run whose identical passing "
        "records may be reused (decision 0037)",
    )
    args = parser.parse_args(argv)
    suites = json.loads((ROOT / "tools/checkpoint_suites.json").read_text(encoding="utf-8"))
    suite = suites["suites"][args.checkpoint]
    rel = f"development/receipts/{args.checkpoint}/{args.run_id}"
    run_dir = ROOT / rel
    if run_dir.exists():
        print(f"run directory {rel} already exists", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True)
    digest = tree_manifest.write_manifest(run_dir / "tree-manifest.json", ROOT)
    (run_dir / "suite.json").write_bytes((ROOT / "tools/checkpoint_suites.json").read_bytes())
    print(f"candidate tree {digest}")
    runner = [sys.executable, str(ROOT / "tools/run_check.py"), "--run-dir", str(run_dir)]
    environment = subprocess.run([*runner, "--name", "env", "--", *ENV_REPORT], check=False)
    if environment.returncode != 0:
        print("FAIL: environment report failed; no suite or receipt plan was run", file=sys.stderr)
        return 1
    tested = code_under_test(run_dir)
    if tested is not None:
        print(f"FAIL: {tested}; no suite or receipt plan was run", file=sys.stderr)
        return 1
    reusable = reusable_records(args.reuse_from, digest)
    checks = []
    failed = 0
    for check_id, spec in suite.items():
        if args.only and check_id not in args.only:
            continue
        extra: dict[str, str] = {}
        for name, command in spec["commands"].items():
            expanded = expand_command(command, rel)
            source = None if PLACEHOLDER in " ".join(command) else reusable.get(tuple(expanded))
            if source is not None:
                reuse(source, run_dir, name)
                continue
            code = subprocess.run(
                [*runner, "--name", name, "--", *expanded], check=False
            ).returncode
            failed += code != 0
            if code == 0 and PLACEHOLDER not in " ".join(command):
                # Later commands of this run with the same argv reuse this record too.
                reusable.setdefault(tuple(expanded), run_dir / f"{name}.cmd.json")
            extra |= {
                path: f"{description} of {name}"
                for path, description in boundary_evidence(command, run_dir).items()
            }
        check = {
            "id": check_id,
            "method": "automated",
            "commands": list(spec["commands"]),
            "summary": spec["summary"],
            "limitations": spec.get("limitations", []),
        }
        if extra:
            check["extra_evidence"] = extra
        checks.append(check)
    if tree_manifest.tree_sha256(ROOT) != digest:
        print("FAIL: the tree changed while the suite ran; evidence is not bound", file=sys.stderr)
        failed += 1
    plan = {
        "implementer_id": args.implementer,
        "checks": checks,
        "external_checks": [],
        "limitations": [],
        "next_task": None,
    }
    (run_dir / "receipt-plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(f"{'FAILURES: ' + str(failed) if failed else 'all commands exited 0'}; plan at {rel}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
