"""Write the bidirectional parity inventory (C10-PARITY evidence; decision 0015).

Usage:
    uv run python tools/parity_inventory.py                 # JSON on stdout
    uv run python tools/parity_inventory.py --out FILE.json # write a file (never overwrites)

The inventory enumerates the real CLI command tree, MCP server and hub router, maps every
entry point to the operation registry (``prometheos.registry``) or to the reviewed
exemption list, and lists every recorded gap by owning CP10 task. Exit status 1 when a
forward problem or an unmapped entry point exists (gaps alone do not fail it).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from prometheos.registry import inventory

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="write the JSON here (must not exist)")
    args = parser.parse_args(argv)
    report = inventory.build(ROOT)
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.out:
        if args.out.exists():
            print(f"refusing to overwrite {args.out}", file=sys.stderr)
            return 2
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.write(text)
    unmapped = sum(len(v) for v in report["reverse_unmapped"].values())
    print(
        f"operations={report['operation_count']} forward_problems="
        f"{len(report['forward_problems'])} unmapped={unmapped} gaps={len(report['gaps'])} "
        f"by_owner={report['gaps_by_owner']}",
        file=sys.stderr,
    )
    return 1 if report["forward_problems"] or unmapped else 0


if __name__ == "__main__":
    sys.exit(main())
