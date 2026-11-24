"""Assemble a checkpoint receipt from a run directory's retained evidence.

The run directory ``development/receipts/<CP>/<run-id>/`` must contain:
- ``tree-manifest.json`` written at run start (``tools/tree_manifest.py --out``),
- ``env.cmd.json``/``env.stdout.txt`` from ``tools/env_report.py`` run via run_check,
- command records produced by ``tools/run_check.py``,
- ``receipt-plan.json`` mapping check IDs to command records and summaries,
- optionally ``review.json`` (written by the independent reviewer) and ``review.md``.

Statuses are derived: an automated check passes only if every cited command
record exited 0. Review status comes from the reviewer-written ``review.json``.
The receipt is written to ``development/receipts/<CP>/<run-id>.json`` and then
validated; it is never overwritten.

Usage: uv run python tools/build_receipt.py development/receipts/CP0/<run-id>
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_receipt

ROOT = Path(__file__).resolve().parents[1]


def git_revision(root: Path) -> tuple[str | None, bool]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=root, capture_output=True, text=True
        )
        if head.returncode != 0:
            return None, True
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
        )
        return head.stdout.strip(), bool(dirty.stdout.strip())
    except OSError:
        return None, True


def build(run_dir_rel: str, root: Path = ROOT) -> Path:
    run_dir = root / run_dir_rel
    parts = Path(run_dir_rel).parts
    if len(parts) != 4 or parts[:2] != ("development", "receipts"):
        raise SystemExit("run dir must be development/receipts/<CP>/<run-id>")
    cp_id, run_id = parts[2], parts[3]
    rel = f"development/receipts/{cp_id}/{run_id}"
    plan = verify_receipt.load_json(run_dir / "receipt-plan.json")
    evidence: dict[str, str] = {}

    def add(name: str, description: str) -> str:
        path = run_dir / name
        if not path.is_file():
            raise SystemExit(f"missing evidence file {name}")
        relpath = f"{rel}/{name}"
        evidence[relpath] = description
        return relpath

    def command_entry(name: str) -> dict[str, Any]:
        record = verify_receipt.load_json(run_dir / f"{name}.cmd.json")
        return {
            "command": record["command"],
            "exit_code": record["exit_code"],
            "evidence_paths": [
                add(f"{name}.cmd.json", f"command record {name}"),
                add(record["stdout"]["file"], f"stdout of {name}"),
                add(record["stderr"]["file"], f"stderr of {name}"),
            ],
        }

    def check_entry(spec: dict[str, Any], external: bool) -> dict[str, Any]:
        commands = [command_entry(n) for n in spec.get("commands", [])]
        extra = [add(n, d) for n, d in spec.get("extra_evidence", {}).items()]
        if spec["method"] == "automated" and not external:
            ok = bool(commands) and all(c["exit_code"] == 0 for c in commands)
            derived = "passed" if ok else "failed"
            declared = spec.get("status", derived)
            if declared == "passed" and derived != "passed":
                raise SystemExit(f"{spec['id']}: plan claims pass but commands failed")
            status = declared
        else:
            status = spec["status"]
        return {
            "id": spec["id"],
            "status": status,
            "method": spec["method"],
            "commands": commands,
            "evidence_paths": extra,
            "observer": spec.get("observer"),
            "summary": spec["summary"],
            "limitations": spec.get("limitations", []),
        }

    env_paths = [
        add(f"env.{suffix}", "environment report evidence")
        for suffix in ("cmd.json", "stdout.txt", "stderr.txt")
    ]
    env_evidence = {p: {"sha256": verify_receipt.sha256_file(root / p)} for p in env_paths}
    env_errors: list[str] = []
    env = verify_receipt.environment_report(
        root,
        rel,
        env_evidence,
        env_errors,
        verify_receipt.sha256_file(run_dir / "tree-manifest.json"),
        f"{rel}.json",
        datetime.now(UTC).isoformat(),
    )
    if env_errors:
        raise SystemExit("invalid environment report command: " + "; ".join(env_errors))
    assert env is not None
    manifest_rel = add("tree-manifest.json", "candidate tree manifest")
    if (run_dir / "suite.json").is_file():
        add("suite.json", "checkpoint suite declared by the candidate tree")
    checks = [check_entry(c, external=False) for c in plan["checks"]]
    externals = [check_entry(c, external=True) for c in plan.get("external_checks", [])]
    for name, description in plan.get("supporting_evidence", {}).items():
        add(name, description)
    review: dict[str, Any] = {
        "status": "pending",
        "reviewer_id": None,
        "evidence_paths": [],
        "summary": "No independent review recorded.",
    }
    if (run_dir / "review.json").is_file():
        verdict = verify_receipt.load_json(run_dir / "review.json")
        paths = [add("review.json", "independent reviewer verdict")]
        if (run_dir / "review.md").is_file():
            paths.append(add("review.md", "independent review report"))
        review = {
            "status": verdict["status"],
            "reviewer_id": verdict["reviewer_id"],
            "evidence_paths": paths,
            "summary": verdict["summary"],
        }
    required = verify_receipt.Package(root).checkpoints[cp_id]["required_checks"]
    by_id = {c["id"]: c["status"] for c in checks}
    if all(by_id.get(cid) == "passed" for cid in required):
        technical = "passed"
    elif any(s == "failed" for s in by_id.values()):
        technical = "failed"
    else:
        technical = "in_progress"
    revision, dirty = git_revision(root)
    # The spec binding comes from the tree the run executed on, not from whatever is sealed
    # now; a resealed working tree would otherwise produce an invalid receipt (review a01 R2-3).
    tree = verify_receipt.load_json(run_dir / "tree-manifest.json")
    run_spec = {row["path"]: row["sha256"] for row in tree["files"]}.get("package-manifest.json")
    if run_spec != verify_receipt.sha256_file(root / "package-manifest.json"):
        raise SystemExit("the specification was resealed since this run; rerun the checkpoint")
    receipt = {
        "schema_version": 1,
        "spec_version": verify_receipt.Package(root).spec_version,
        "spec_manifest_sha256": run_spec,
        "run_id": run_id,
        "checkpoint_id": cp_id,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "candidate": {
            "git_revision": revision,
            "tree_sha256": hashlib.sha256(
                (run_dir / "tree-manifest.json").read_bytes()
            ).hexdigest(),
            "tree_manifest_path": manifest_rel,
            "dirty": dirty,
        },
        "implementer_id": plan["implementer_id"],
        "environment": {
            "os": env["os"],
            "architecture": env["architecture"],
            "python": env["python"],
            "tool_versions": env["tool_versions"],
        },
        "technical_status": technical,
        "checks": checks,
        "external_checks": externals,
        "evidence": [
            {"path": p, "sha256": verify_receipt.sha256_file(root / p), "description": d}
            for p, d in sorted(evidence.items())
        ],
        "review": review,
        "limitations": plan.get("limitations", []),
        "next_task": plan.get("next_task"),
    }
    out = root / f"{rel}.json"
    if out.exists():
        raise SystemExit(f"receipt {out} already exists; receipts are immutable")
    out.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    out = build(args[0].replace("\\", "/").rstrip("/"))
    rel = out.relative_to(ROOT).as_posix()
    _, errors = verify_receipt.validate_receipt(ROOT, rel)
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    print(f"wrote {rel} ({'valid' if not errors else 'INVALID'})")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
