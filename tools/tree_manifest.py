"""Candidate tree identity: a sorted manifest of implementation/spec/config/test files.

The manifest is canonical JSON (sorted keys, compact) so ``tree_sha256`` is the
SHA-256 of the manifest file bytes. Excluded: VCS metadata, caches, virtual
environments, generated build outputs, local stores/secrets and mutable
development evidence (receipts, impact records, state, handoff) to avoid
recursive hashing. Lockfiles and source assets are included.

Usage: uv run python tools/tree_manifest.py [--out PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".claude",
}
# Repository-relative directory prefixes excluded entirely.
EXCLUDED_PREFIXES = (
    "build/",
    "dist/",
    "local/",
    "scratch/",
    "dogfood/",  # owner research material for dogfooding (decision 0008), never a candidate file
    "hub/dist/",
    "hub/test-results/",
    "hub/playwright-report/",
    "hub/screenshots/",
    "hub/test-output.txt",
    "hub/e2e/.state/",
    # The desktop app's and the CP19.1 spike's build outputs (git-ignored; decisions 0050 and
    # 0055): the CP19 suite builds the app inside a run, which must not change its candidate.
    "desktop/app/build/",
    "desktop/app/dist/",
    "desktop/app/dist-release/",  # the public release build (decision 0072)
    "desktop/spike/electron/dist/",
    "desktop/spike/pywebview/build/",
    "desktop/spike/pywebview/dist/",
    "desktop/spike/tauri/src-tauri/target/",
    "desktop/spike/tauri/src-tauri/gen/",
)
# Development records are excluded except reviewed design/dependency inputs.
DEVELOPMENT_INCLUDED = ("development/dependencies.json", "development/decisions/")
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".sqlite", ".sqlite-journal")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ManifestError(RuntimeError):
    pass


def is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def excluded(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info") for part in parts):
        return True
    if relative.startswith(EXCLUDED_PREFIXES):
        return True
    if relative.startswith("development/"):
        return not any(
            relative == inc or (inc.endswith("/") and relative.startswith(inc))
            for inc in DEVELOPMENT_INCLUDED
        )
    if relative.startswith("desktop/spike/pywebview/") and relative.endswith(".spec"):
        return True  # PyInstaller's generated spec (git-ignored)
    return relative.endswith(EXCLUDED_SUFFIXES)


def dir_excluded(relative_dir: str) -> bool:
    """``relative_dir`` ends with '/'. Development evidence directories are skipped."""
    name = relative_dir.rstrip("/").rsplit("/", 1)[-1]
    if name in EXCLUDED_DIR_NAMES or name.endswith(".egg-info"):
        return True
    if relative_dir.startswith(("development/receipts/", "development/impact/")):
        return True
    return relative_dir.startswith(EXCLUDED_PREFIXES)


def build_manifest(root: Path = ROOT) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            rel = (current / name).relative_to(root).as_posix()
            if dir_excluded(rel + "/"):
                continue
            if is_link_or_reparse(current / name):
                raise ManifestError(f"refusing to manifest linked directory: {rel}")
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            path = current / name
            rel = path.relative_to(root).as_posix()
            if excluded(rel):
                continue
            if is_link_or_reparse(path):
                raise ManifestError(f"refusing to manifest linked file: {rel}")
            data = path.read_bytes()
            files.append(
                {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            )
    files.sort(key=lambda row: row["path"])
    return {"schema_version": 1, "kind": "prometheos-tree-manifest", "files": files}


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def tree_sha256(root: Path = ROOT) -> str:
    return hashlib.sha256(manifest_bytes(build_manifest(root))).hexdigest()


def write_manifest(out: Path, root: Path = ROOT) -> str:
    data = manifest_bytes(build_manifest(root))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def diff_paths(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    before = {row["path"]: row["sha256"] for row in old["files"]}
    after = {row["path"]: row["sha256"] for row in new["files"]}
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or print the candidate tree manifest.")
    parser.add_argument("--out", type=Path, help="write canonical manifest bytes to this path")
    args = parser.parse_args(argv)
    if args.out:
        digest = write_manifest(args.out)
    else:
        digest = tree_sha256()
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
