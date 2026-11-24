"""Build the hub web assets (Node is build-time only) and copy them into the package.

    uv run python tools/build_hub.py            # npm ci + npm run build in hub/, then copy

The built files land in ``src/prometheos/hub/static/`` (shipped in the wheel) together
with ``build-info.json``: the digest of the frontend sources and lockfile they were built
from, the Node/npm versions and the file list. ``tools/check_hub_assets.py`` verifies that
the shipped assets still match their sources.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUB = ROOT / "hub"
STATIC = ROOT / "src" / "prometheos" / "hub" / "static"
sys.path.insert(0, str(ROOT / "tools"))

from check_hub_assets import asset_manifest, source_digest  # noqa: E402


def run(cmd: list[str]) -> str:
    npm = shutil.which(cmd[0]) or cmd[0]
    done = subprocess.run([npm, *cmd[1:]], cwd=HUB, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        sys.stderr.write(done.stdout + done.stderr)
        raise SystemExit(f"{' '.join(cmd)} failed ({done.returncode})")
    return done.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-install", action="store_true", help="reuse hub/node_modules")
    args = parser.parse_args()
    if not args.skip_install:
        run(["npm", "ci", "--no-audit", "--no-fund"])
    run(["npm", "run", "build"])
    dist = HUB / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("build produced no index.html")
    if STATIC.exists():
        shutil.rmtree(STATIC)
    shutil.copytree(dist, STATIC)
    info = {
        "source_sha256": source_digest(HUB),
        "node": run(["node", "--version"]),
        "npm": run(["npm", "--version"]),
        "files": asset_manifest(STATIC),
    }
    (STATIC / "build-info.json").write_text(
        json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps({"static": str(STATIC), "source_sha256": info["source_sha256"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
