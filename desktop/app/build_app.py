"""Build the desktop app offline (CP19.2/CP19.4; decisions 0054 and 0055).

Usage (repository root, main environment):
    .venv/Scripts/python.exe desktop/app/build_app.py [--dir] [--release] [--seed-cache]

Runs electron-builder (pinned in desktop/app/package-lock.json) with nothing downloaded:
- Electron itself comes from ``desktop/app/node_modules/electron/dist`` (``electronDist`` in
  package.json), never from Electron's download cache;
- electron-builder's own tools (NSIS 3.0.4.1, its resources, 7-Zip) come from the project-local
  cache ``local/toolchains/electron-builder-cache`` (git-ignored, decision 0049's project-local
  toolchains), set through ELECTRON_BUILDER_CACHE; the build fails when the cache is missing.
- ``--seed-cache`` fills that cache once from the per-user cache an earlier online build left
  (``%LOCALAPPDATA%\\electron-builder\\Cache``); it never downloads.

The build log is printed; a line saying that electron-builder downloads anything fails the build.
This is detection after the fact, not prevention: under the integration launcher's offline
environment a cache miss would also fail, and either way the miss is never silent.
``--dir`` builds only ``dist/win-unpacked`` (what C19-WALK drives); the default builds it and
the NSIS installer.

Every build first writes the installer's notices (``build/notices/THIRD-PARTY-NOTICES.txt``,
from the built core, by ``tools/third_party_notices.py``) and ships it with LICENSE and NOTICE.

``--release`` (decision 0072) makes the public release build, into ``dist-release/``: Electron's
``enableNodeCliInspectArguments`` fuse is off, and the packaged package.json is stamped
``prometheosRelease``, which makes main.js ignore ``PROMETHEOS_DESKTOP_TEST`` (no test hooks).
Without it the build is the test build that the shell test, the installer check and C19-WALK
drive: Playwright attaches through the inspector, so the fuse stays on there. After either
build ``desktop/app/test/release_check.js`` reads the result back (fuses, stamp, notices) and
``desktop/app/test/release_hooks_check.js`` runs the built main.js with the test switch on; the
build fails unless both are what that kind of build must have. A release build is also
privacy-scanned (``tools/privacy_scan.py``): the owner's denylist over everything under
``resources/``, every rule over what PrometheOS itself puts there; it needs the git-ignored
``local/privacy-denylist.txt`` and fails on any finding.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

APP = Path(__file__).resolve().parent
REPO = APP.parents[1]
CACHE = REPO / "local" / "toolchains" / "electron-builder-cache"
NEEDED = ("nsis-3.0.4.1", "nsis-resources-3.4.1", "7zip@1.0.0")
CLI = APP / "node_modules" / "electron-builder" / "cli.js"
DOWNLOAD = re.compile(r"\bdownload(ing|ed)?\b", re.IGNORECASE)
NOTICES = APP / "build" / "notices" / "THIRD-PARTY-NOTICES.txt"
RELEASE_CONFIG = APP / "build" / "release-config.json"


def release_config() -> Path:
    """package.json's build section with the inspector fuse off and the release stamp."""
    config = copy.deepcopy(json.loads((APP / "package.json").read_text("utf-8"))["build"])
    config["electronFuses"]["enableNodeCliInspectArguments"] = False
    config["extraMetadata"] = {"prometheosRelease": True}
    config["directories"]["output"] = "dist-release"
    RELEASE_CONFIG.write_text(json.dumps(config, indent=2) + "\n", "utf-8")
    return RELEASE_CONFIG


def check_built(node: str, folder: Path, release: bool) -> list[str]:
    """What a release (or a test) build must be, read back from the built app."""
    out = subprocess.run(
        [node, str(APP / "test" / "release_check.js"), str(folder)],
        cwd=APP, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )  # fmt: skip
    if out.returncode != 0:
        return [f"release_check failed: {out.stderr.strip()}"]
    report = json.loads(out.stdout)
    print(f"built app: {json.dumps(report)}")
    problems = []
    if report["inspect_arguments_fuse"] == release:
        problems.append(f"inspector fuse {'on' if release else 'off'} in a {kind(release)} build")
    if report["release_build"] != release:
        problems.append(f"release stamp {'missing' if release else 'present'} ({kind(release)})")
    if report["run_as_node_fuse"]:
        problems.append("the runAsNode fuse is on")
    if len(report["notices"]) != 3 or len(report["electron_notices"]) != 2:
        problems.append(f"notices missing: {report}")
    # Behaviour, not only the stamp: the built main.js under PROMETHEOS_DESKTOP_TEST=1.
    work = APP / "build" / f"hooks-check-{kind(release)}"
    shutil.rmtree(work, ignore_errors=True)
    hooks = subprocess.run(
        [node, str(APP / "test" / "release_hooks_check.js"), str(folder), str(work)],
        cwd=APP, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )  # fmt: skip
    shutil.rmtree(work, ignore_errors=True)
    if hooks.returncode != 0:
        return [*problems, f"release_hooks_check failed: {hooks.stderr.strip()}"]
    exposed = json.loads(hooks.stdout)["test_hooks_exposed"]
    print(f"test hooks under PROMETHEOS_DESKTOP_TEST=1: {'exposed' if exposed else 'absent'}")
    if exposed == release:
        state = "exposed" if release else "absent"
        problems.append(f"test hooks {state} in a {kind(release)} build")
    return problems


def first_party(rel: str) -> bool:
    """What a PrometheOS build itself adds under resources/."""
    return rel in {
        "app.asar",
        "LICENSE",
        "NOTICE",
        "THIRD-PARTY-NOTICES.txt",
        "core/MANIFEST.json",
    } or (rel.startswith("core/Lib/site-packages/prometheos"))


def published(resources: Path, finding: Any) -> bool:
    """A denylist finding on a line the owner published: a shipped copy of NOTICE whose line is
    exactly a line of the repository's committed NOTICE (E08 put the owner's copyright name
    there). Only NOTICE files, only those exact lines; the term stays a finding anywhere else
    (dogfood P18, review kers-p18-a)."""
    if finding.rule != "denylist" or Path(finding.path).name != "NOTICE":
        return False
    committed = set((REPO / "NOTICE").read_text(encoding="utf-8").splitlines())
    lines = (resources / finding.path).read_text(encoding="utf-8").splitlines()
    return 0 < finding.line <= len(lines) and lines[finding.line - 1] in committed


def privacy_problems(resources: Path) -> list[str]:
    """A release build ships nothing of this machine's owner (review publish-ready-a F1): the
    privacy scan over everything the app adds to Electron (resources/: the core, app.asar and
    the notices), with the owner's denylist and the research names under dogfood/."""
    sys.path.insert(0, str(REPO / "tools"))
    import privacy_scan

    if not privacy_scan.DEFAULT_DENYLIST.is_file():
        return [f"no owner denylist at {privacy_scan.DEFAULT_DENYLIST}; a release build needs one"]
    terms = privacy_scan.load_denylist([privacy_scan.DEFAULT_DENYLIST])
    research = REPO / "dogfood"
    names = privacy_scan.harvest_names(research) if research.is_dir() else set()
    # The owner's terms anywhere; every rule over the files PrometheOS itself puts there (the
    # third-party files carry their maintainers' addresses and build paths by design).
    findings = privacy_scan.scan(resources, terms, names, rules=frozenset({"denylist"}))
    findings += privacy_scan.scan(resources, terms, names, only=first_party)
    findings = [f for f in findings if not published(resources, f)]
    print(f"privacy scan of {resources}: {len(findings)} finding(s)")
    return [f"privacy: {f.path}:{f.line}: {f.rule}: {f.detail}" for f in findings[:20]]


def kind(release: bool) -> str:
    return "release" if release else "test"


def seed() -> int:
    source = Path(os.environ["LOCALAPPDATA"]) / "electron-builder" / "Cache"
    missing = [n for n in NEEDED if not (source / n).is_dir()]
    if missing:
        print(f"the per-user cache lacks {missing}: {source}", file=sys.stderr)
        return 2
    shutil.copytree(source, CACHE, dirs_exist_ok=True)
    print(f"seeded {CACHE} from {source}")
    return 0


def main(argv: list[str]) -> int:
    if "--seed-cache" in argv:
        return seed()
    missing = [n for n in NEEDED if not (CACHE / n).is_dir()]
    if missing:
        print(f"the project-local electron-builder cache lacks {missing}: {CACHE} "
              "(run build_app.py --seed-cache once)", file=sys.stderr)  # fmt: skip
        return 2
    node = shutil.which("node")
    if node is None or not CLI.is_file():
        print(
            "Node and desktop/app/node_modules are needed (npm ci in desktop/app)", file=sys.stderr
        )
        return 2
    target = "dir" if "--dir" in argv else "nsis"
    release = "--release" in argv
    notices = subprocess.run(
        [sys.executable, str(REPO / "tools" / "third_party_notices.py"), "--installer",
         str(NOTICES)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )  # fmt: skip
    if notices.returncode != 0:
        print(notices.stdout + notices.stderr, file=sys.stderr)
        return notices.returncode
    env = {**os.environ, "ELECTRON_BUILDER_CACHE": str(CACHE),
           "ELECTRON_CACHE": str(CACHE / "electron-unused")}  # fmt: skip
    config = ["--config", str(release_config())] if release else []
    out = subprocess.run(
        [node, str(CLI), "--projectDir", str(APP), "--win", target, "--publish", "never", *config],
        cwd=APP, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )  # fmt: skip
    log = out.stdout + out.stderr
    print(log)
    fetched = [line for line in log.splitlines() if DOWNLOAD.search(line)]
    if fetched:
        print(f"FAIL: electron-builder downloaded: {fetched}", file=sys.stderr)
        return 1
    if out.returncode != 0:
        return out.returncode
    unpacked = APP / ("dist-release" if release else "dist") / "win-unpacked"
    problems = check_built(node, unpacked, release)
    if release:
        problems += privacy_problems(unpacked / "resources")
    if problems:
        print(f"FAIL: {problems}", file=sys.stderr)
        return 1
    print(f"PASS: built the {kind(release)} {target} offline from {CACHE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
