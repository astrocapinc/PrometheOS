"""Draft a change-impact record (schemas/impact.schema.json) between two tree manifests.

``changed_paths`` is computed from the manifests, never typed by hand. The
implementer chooses affected checks (rerun) and carried checks (with reasons);
an independent reviewer then writes ``development/impact/reviews/<name>.review.json``
binding the record's SHA-256, after which the record can be accepted.

Usage:
  uv run python tools/make_impact.py NAME --from MANIFEST --to MANIFEST
      [--affected C00-BOOT ...] [--rerun RECEIPT ...]
      [--carry CHECK=RECEIPT=REASON ...] --author ID --reviewer ID [--status accepted]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tree_manifest

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft an impact record.")
    parser.add_argument("name")
    parser.add_argument("--from", dest="from_manifest", required=True)
    parser.add_argument("--to", dest="to_manifest", required=True)
    parser.add_argument("--affected", nargs="*", default=[])
    parser.add_argument("--rerun", nargs="*", default=[])
    parser.add_argument("--carry", nargs="*", default=[])
    parser.add_argument("--author", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--status", default="pending", choices=["pending", "accepted"])
    args = parser.parse_args(argv)
    old_path, new_path = ROOT / args.from_manifest, ROOT / args.to_manifest
    old = json.loads(old_path.read_text(encoding="utf-8"))
    new = json.loads(new_path.read_text(encoding="utf-8"))
    carried = []
    for spec in args.carry:
        check, receipt, reason = spec.split("=", 2)
        carried.append({"id": check, "source_receipt": receipt, "reason": reason})
    record = {
        "schema_version": 1,
        "from_manifest": args.from_manifest,
        "from_tree_sha256": hashlib.sha256(old_path.read_bytes()).hexdigest(),
        "to_manifest": args.to_manifest,
        "to_tree_sha256": hashlib.sha256(new_path.read_bytes()).hexdigest(),
        "changed_paths": tree_manifest.diff_paths(old, new),
        "affected_check_ids": sorted(args.affected),
        "rerun_receipts": args.rerun,
        "carried_checks": carried,
        "author_id": args.author,
        "reviewer_id": args.reviewer,
        "review_status": args.status,
        "review_evidence_paths": [f"development/impact/reviews/{args.name}.review.json"],
    }
    out = ROOT / "development" / "impact" / f"{args.name}.json"
    if out.exists():
        print(f"{out} exists; impact records are immutable once reviewed", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"wrote {out.relative_to(ROOT).as_posix()} sha256={digest}")
    print(f"{len(record['changed_paths'])} changed paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
