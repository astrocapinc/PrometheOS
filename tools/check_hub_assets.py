"""Check that the shipped hub assets match the frontend sources they were built from.

    uv run python tools/check_hub_assets.py

Fails when the frontend sources or lockfile changed after the last ``build_hub.py`` run,
when a shipped file differs from ``build-info.json``, or when ``index.html`` references
a script/stylesheet that is not shipped or uses inline script/style (the hub's CSP
forbids them). No Node is needed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUB = ROOT / "hub"
STATIC = ROOT / "src" / "prometheos" / "hub" / "static"
SOURCE_EXCLUDED = {
    "node_modules",
    "dist",
    "test-results",
    "playwright-report",
    "screenshots",
    ".state",
}
SOURCE_EXCLUDED_FILES = {"test-output.txt"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_digest(hub: Path) -> str:
    """Digest of every frontend source file (paths and bytes), lockfile included."""
    lines = []
    for path in sorted(hub.rglob("*")):
        rel = path.relative_to(hub)
        if any(part in SOURCE_EXCLUDED for part in rel.parts) or rel.name in SOURCE_EXCLUDED_FILES:
            continue
        if path.is_file():
            lines.append(f"{rel.as_posix()}\t{_sha(path)}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def asset_manifest(static: Path) -> dict[str, str]:
    return {
        p.relative_to(static).as_posix(): _sha(p)
        for p in sorted(static.rglob("*"))
        if p.is_file() and p.name != "build-info.json"
    }


def check() -> list[str]:
    problems: list[str] = []
    info_path = STATIC / "build-info.json"
    if not info_path.is_file():
        return ["no build-info.json: run tools/build_hub.py"]
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if HUB.is_dir() and source_digest(HUB) != info["source_sha256"]:
        problems.append("hub/ sources changed since the shipped build: run tools/build_hub.py")
    if asset_manifest(STATIC) != info["files"]:
        problems.append("shipped assets differ from build-info.json")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", index) or "<style" in index:
        problems.append("index.html contains inline script or style (forbidden by the CSP)")
    if re.search(r"\b(?:src|href)=\"(?:https?:)?//", index):
        problems.append("index.html loads a remote resource")
    for ref in re.findall(r"(?:src|href)=\"/([^\"]+)\"", index):
        if not (STATIC / ref).is_file():
            problems.append(f"index.html references a missing asset: {ref}")
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(f"FAIL: {problem}")
    if not problems:
        print("PASS: hub assets match their sources; no inline or remote resources")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
