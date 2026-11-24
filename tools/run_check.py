"""Run one check command and retain its evidence.

Writes ``<run-dir>/<name>.stdout.txt``, ``<name>.stderr.txt`` and a command
record ``<name>.cmd.json`` holding the exact argv, display command, exit code,
timestamps and output digests. The receipt verifier cross-checks receipts
against these records, so a receipt cannot claim an exit code or output that
was not recorded. Exit status is propagated.

Usage: uv run python tools/run_check.py --run-dir DIR --name NAME -- CMD [ARGS...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tree_manifest

ROOT = Path(__file__).resolve().parents[1]


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a check command and retain evidence.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (tests)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("missing command")
    if not args.name.replace("-", "").replace("_", "").isalnum():
        parser.error("name must be alphanumeric with - or _")
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    root = args.root.resolve()
    tree = tree_manifest.tree_sha256(root)
    started = now()
    try:
        proc = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            timeout=args.timeout,
            check=False,
        )
        exit_code: int | None = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout = exc.stdout or b""
        stderr = (exc.stderr or b"") + f"\n[run_check] timeout after {args.timeout}s\n".encode()
    ended = now()
    if tree_manifest.tree_sha256(root) != tree:
        stderr += b"\n[run_check] the tree changed while the command ran\n"
        exit_code = exit_code or 1
    out_path = run_dir / f"{args.name}.stdout.txt"
    err_path = run_dir / f"{args.name}.stderr.txt"
    out_path.write_bytes(stdout)
    err_path.write_bytes(stderr)
    record = {
        "schema_version": 1,
        "kind": "prometheos-command-record",
        "name": args.name,
        "command": shlex.join(command),
        "argv": command,
        "cwd": ".",
        "tree_sha256": tree,
        "started_at": started,
        "ended_at": ended,
        "exit_code": exit_code,
        "stdout": {"file": out_path.name, "sha256": hashlib.sha256(stdout).hexdigest()},
        "stderr": {"file": err_path.name, "sha256": hashlib.sha256(stderr).hexdigest()},
    }
    (run_dir / f"{args.name}.cmd.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    sys.stdout.write(stdout.decode("utf-8", "replace"))
    sys.stderr.write(stderr.decode("utf-8", "replace"))
    print(f"[run_check] {args.name}: exit {exit_code}", file=sys.stderr)
    return 1 if exit_code is None else (exit_code if exit_code < 256 else 1)


if __name__ == "__main__":
    sys.exit(main())
