"""Opt-in dogfood check of the adoption inventory on a real folder (decision 0039 section 6).

    uv run python tools/dogfood_adoption.py --source DIR [--out FILE] [--keep]

Never part of the default suite. The real folder is only read: it is copied (with its times)
into a temporary directory, a disposable PrometheOS home adopts the copy, and the original's
full digest (every name, size, file time and content) is compared before and after. The report
holds aggregate numbers only (counts by status, container and zone, unreadable reasons by
class, timings); no file name or content is printed or written, so the report can be shared.
Ranking accuracy is never measured here: that needs blind labels frozen first (E15/E16).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prometheos.adoption import inventory as inv  # noqa: E402
from prometheos.app.reads import Caller  # noqa: E402
from prometheos.app.service import Service  # noqa: E402

AGENT = Caller(actor="dogfood", scope="agent")


def full_digest(root: Path) -> tuple[str, int, int]:
    """Names, kinds, file sizes and times, and every file's content (folder times left out,
    as in ``inventory.root_digest``). Returns ``(digest, files, bytes)``."""
    h = hashlib.sha256()
    files = size = 0
    for walked in inv.walk(root):
        st = walked.stat
        if st is None:
            h.update(f"{walked.rel}\0unreadable\n".encode("utf-8", "surrogatepass"))
            continue
        if inv.is_placeholder(st) or walked.pruned == "placeholder":
            raise SystemExit("the source holds cloud placeholders; refusing to read (or copy) it")
        when = 0 if st.kind == "dir" else st.mtime_ns
        h.update(f"{walked.rel}\0{st.kind}\0{st.size}\0{when}\n".encode("utf-8", "surrogatepass"))
        if st.kind == "file":
            files += 1
            size += st.size
            with open(inv.os_path(root / walked.rel), "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    h.update(chunk)
    return h.hexdigest(), files, size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, required=True, help="The real folder (read only).")
    parser.add_argument("--out", type=Path, help="Write the aggregate report here (JSON).")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary copy and home.")
    parser.add_argument(
        "--labels",
        type=Path,
        help="The owner's frozen blind label sheet; only then are families ranked.",
    )
    args = parser.parse_args(argv)
    source = args.source.resolve()
    if not source.is_dir():
        parser.error("--source must be a folder")

    original_before = full_digest(source)
    work = Path(tempfile.mkdtemp(prefix="prometheos-dogfood-"))
    copy = work / "copy"
    started = time.monotonic()
    shutil.copytree(inv.os_path(source), inv.os_path(copy), symlinks=True)
    copied = time.monotonic() - started
    copy_before = full_digest(copy)
    report: dict[str, Any] = {"files": original_before[1], "bytes": original_before[2]}
    try:
        svc = Service(home=work / "home", actor="dogfood")
        project = svc.init_project(
            title="Dogfood adoption (temporary)",
            objective="Inventory a copy of a real folder; nothing leaves this machine.",
            roots={"copy": str(copy)},
        )
        pid = project["project_id"]
        started = time.monotonic()
        # Never rank a real folder before the owner's blind labels are frozen (C17-LINEAGE,
        # E15/E16): inventory and analyzers only, unless --labels names the frozen sheet.
        scan = svc.adoption_scan(
            pid, AGENT, root_alias="copy", rank=args.labels is not None, mode="wait"
        )
        if args.labels is not None:
            report["labels_sha256"] = hashlib.sha256(args.labels.read_bytes()).hexdigest()
        scanned = time.monotonic() - started
        rows: list[dict[str, Any]] = []
        offset: int | None = 0
        while offset is not None:
            page = svc.adoption_files(pid, AGENT, scan["scan_gen"], offset=offset, limit=500)
            rows += page["files"]
            offset = page["next_offset"]
        evidence: list[dict[str, Any]] = []
        offset = 0
        while offset is not None:
            page = svc.adoption_evidence(pid, AGENT, scan["scan_gen"], offset=offset, limit=500)
            evidence += page["evidence"]
            offset = page["next_offset"]
        report["analysis"] = _coverage(evidence, scan.get("analysis"))
        reasons = Counter(
            (r["status"], (r["reason"] or "").split(":")[0].split(" (")[0]) for r in rows
            if r["status"] in ("unreadable", "limited")
        )  # fmt: skip
        report |= {
            "scan_state": scan["state"],
            "scan_root_digest_equal": scan["root_digest_before"] == scan["root_digest_after"],
            "summary": scan["summary"],
            "unreadable_or_limited": {f"{s}: {r}": n for (s, r), n in sorted(reasons.items())},
            "text_files_with_line_ending_twins": _normalised_twins(rows),
            "exact_duplicate_groups": _exact_groups(rows),
            "seconds": {"copy": round(copied, 2), "scan": round(scanned, 2)},
        }
    finally:
        report["copy_unchanged"] = full_digest(copy) == copy_before
        report["original_unchanged"] = full_digest(source) == original_before
        if not args.keep:
            shutil.rmtree(inv.os_path(work), ignore_errors=True)
        else:
            report["kept"] = str(work)
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    ok = (
        report["original_unchanged"]
        and report["copy_unchanged"]
        and report.get("scan_state") == "complete"
    )
    return 0 if ok else 1


def _coverage(evidence: list[dict[str, Any]], summary: Any) -> dict[str, Any]:
    """Counts only: evidence by predicate, tier and analyzer; languages; parse errors;
    unresolved references by kind (no names)."""
    languages = Counter(e["object"]["literal"] for e in evidence if e["predicate"] == "language")
    parse_errors = sum(
        e["object"]["literal"] for e in evidence
        if e["predicate"] == "facet" and e["object"].get("facet") == "code.parse_errors"
    )  # fmt: skip
    files_with_errors = sum(
        1 for e in evidence
        if e["predicate"] == "facet" and e["object"].get("facet") == "code.parse_errors"
    )  # fmt: skip
    unresolved = Counter(
        (e["analyzer"], (e["object"].get("literal") or {}).get("kind", "?"))
        for e in evidence if e["predicate"] == "unresolved"
    )  # fmt: skip
    return {
        "summary": summary,
        "by_predicate": dict(sorted(Counter(e["predicate"] for e in evidence).items())),
        "by_tier": dict(sorted(Counter(e["tier"] for e in evidence).items())),
        "by_analyzer": dict(sorted(Counter(e["analyzer"] for e in evidence).items())),
        "languages": dict(sorted(languages.items())),
        "matlab_files_with_parse_errors": files_with_errors,
        "parse_error_nodes": parse_errors,
        "unresolved": {f"{a}: {k}": n for (a, k), n in sorted(unresolved.items())},
    }


def _exact_groups(rows: list[dict[str, Any]]) -> dict[str, int]:
    groups = Counter(r["sha256"] for r in rows if r["sha256"] and r["entry"] == "file")
    multi = [n for n in groups.values() if n > 1]
    return {"groups": len(multi), "files": sum(multi)}


def _normalised_twins(rows: list[dict[str, Any]]) -> dict[str, int]:
    by_text: dict[str, set[str]] = {}
    for r in rows:
        if r["text_sha256"]:
            by_text.setdefault(r["text_sha256"], set()).add(r["sha256"])
    twins = [len(s) for s in by_text.values() if len(s) > 1]
    return {"groups": len(twins), "variants": sum(twins)}


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
