"""Find the Playwright Chromium headless shell the browser tests need (never installs it).

The hub's Playwright suites (``tools/run_hub_e2e.py``) and the C10-WALK (``tools/walk_cp10.py``)
launch Playwright's own Chromium headless shell inside a loopback-only boundary (decision 0028).
Playwright looks for revision R of the shell, the one named in its ``browsers.json``, in
``<browsers path>/chromium_headless_shell-R``. This module only checks that it is there so the
tools can fail with a clear message instead of a Playwright launch error; it never downloads or
installs anything (an owner installs it with Playwright's own installer).
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

BROWSER = "chromium-headless-shell"
DIRECTORY_PREFIX = "chromium_headless_shell"


class BrowserUnavailable(RuntimeError):
    """The Chromium headless shell cannot be used; the message says why and what to do."""


def expected_revision(hub: Path) -> str:
    """The shell revision the installed ``playwright-core`` expects (from its browsers.json)."""
    manifest = hub / "node_modules" / "playwright-core" / "browsers.json"
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
        for entry in entries:
            if entry["name"] == BROWSER:
                return str(entry["revision"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BrowserUnavailable(
            f"cannot read the expected {BROWSER} revision from {manifest}: {exc}. "
            "hub/node_modules is needed (npm ci, run by the owner, installs it)."
        ) from exc
    raise BrowserUnavailable(f"{manifest} lists no {BROWSER}")


def browsers_dir(hub: Path, env: Mapping[str, str] | None = None) -> Path:
    """Playwright's browser cache: PLAYWRIGHT_BROWSERS_PATH, else the per-user default."""
    env = os.environ if env is None else env
    configured = env.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":  # Playwright's switch for "inside node_modules"
        return hub / "node_modules" / "playwright-core" / ".local-browsers"
    if configured:
        return Path(configured)
    if sys.platform == "win32":
        local = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    cache = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "ms-playwright"


def find_headless_shell(hub: Path, env: Mapping[str, str] | None = None) -> Path | None:
    """The shell executable for the expected revision, or None when it is not installed."""
    directory = browsers_dir(hub, env) / f"{DIRECTORY_PREFIX}-{expected_revision(hub)}"
    if not (directory / "INSTALLATION_COMPLETE").is_file():  # Playwright's own completion marker
        return None
    for name in ("chrome-headless-shell.exe", "chrome-headless-shell"):
        for candidate in sorted(directory.rglob(name)):
            if candidate.is_file():
                return candidate
    return None


def require_headless_shell(hub: Path, env: Mapping[str, str] | None = None) -> Path:
    """The shell executable, or ``BrowserUnavailable`` with a message that says what is missing."""
    found = find_headless_shell(hub, env)
    if found is not None:
        return found
    revision = expected_revision(hub)
    where = browsers_dir(hub, env) / f"{DIRECTORY_PREFIX}-{revision}"
    raise BrowserUnavailable(
        f"the Playwright Chromium headless shell (revision {revision}) is not installed at "
        f"{where}. The browser tests use only that browser (never an installed Edge or Chrome) "
        "and these tools never download one: point PLAYWRIGHT_BROWSERS_PATH at a browser cache "
        "that has it, or have the owner install it with `npx playwright install "
        "chromium-headless-shell` in hub/."
    )


def playwright_command(hub: Path) -> list[str]:
    """``node <local @playwright/test cli.js> test``: the locally installed runner, never ``npx``.

    ``npx`` fetches a missing package from the registry when it has no terminal; the browser tests
    must never reach one, so the project's own binary is run directly (and its absence is an error).
    """
    node = shutil.which("node")
    if node is None:
        raise BrowserUnavailable("node was not found: Node is required to run the browser tests")
    cli = hub / "node_modules" / "@playwright" / "test" / "cli.js"
    if not cli.is_file():
        raise BrowserUnavailable(
            f"{cli} is missing: hub/node_modules is not installed (npm ci, run by the owner)"
        )
    return [node, str(cli), "test"]


def boundary_evidence(path: Path) -> tuple[dict[str, Any], list[str]]:
    """What the browser boundary controls recorded (hub/e2e/boundary-checks.ts; decision 0028).

    A run whose browser did not prove its boundary fails: no file, an unreadable one, a control
    that did not pass, or ``all_passed`` not true (a registered control that never ran).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        statuses = {c["control"]: c["status"] for c in data["controls"]}
        summary: dict[str, Any] = {
            "evidence": path.name,
            "all_passed": data["all_passed"] is True
            and bool(statuses)
            and all(s == "passed" for s in statuses.values()),
            "controls": statuses,
            "browser": data["observed"].get("browser"),
            "journey_browsers": data.get("journeys", []),
            "launch_args": data["launch_args"],
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {"evidence": None}, [
            f"the browser boundary controls left no usable evidence ({path.name})"
        ]
    if not summary["all_passed"]:
        failed = sorted(name for name, status in statuses.items() if status != "passed")
        return summary, [
            f"the browser boundary controls did not all pass: {failed or 'some did not run'}"
        ]
    return summary, []


BOUNDARY_PROJECT = "boundary"  # the controls' project (hub/e2e/boundary.ts, boundaryProjects)


def tests_that_ran(report: Any) -> list[tuple[str, int, str]]:
    """``(project, worker index, title)`` of every test result in a Playwright JSON report that
    ran (any status but ``skipped``)."""
    found: list[tuple[str, int, str]] = []

    def visit(suite: dict[str, Any], titles: list[str]) -> None:
        for spec in suite.get("specs", []):
            for test in spec["tests"]:
                for result in test["results"]:
                    if result["status"] != "skipped":
                        title = " > ".join([*titles, spec["title"]])
                        found.append((test["projectName"], result["workerIndex"], title))
        for child in suite.get("suites", []):
            visit(child, [*titles, child["title"]])

    for suite in report["suites"]:
        visit(suite, [suite["title"]])
    return found


def journey_problems(report: Path, journeys: list[Any]) -> list[str]:
    """Every test outside the ``boundary`` project must have run in a worker whose browser the
    journey guard checked (``hub/e2e/boundary-guard.ts`` adds each such worker to the evidence's
    ``journeys``). A spec that runs without the guard (Playwright's own ``test`` under any name)
    runs in a worker with no record, so the run is not a pass. Playwright's JSON report, not the
    specs, says which tests ran in which worker."""
    try:
        ran = tests_that_ran(json.loads(report.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return [f"Playwright left no usable test report ({report.name})"]
    checked = {(j.get("project"), j.get("worker")) for j in journeys if isinstance(j, dict)}
    unchecked = sorted(
        {
            f"{title} [{project}, worker {worker}]"
            for project, worker, title in ran
            if project != BOUNDARY_PROJECT and (project, worker) not in checked
        }
    )
    if not unchecked:
        return []
    more = f" and {len(unchecked) - 5} more" if len(unchecked) > 5 else ""
    return [
        "tests ran in a browser worker whose browser the journey guard did not check (no journey "
        f"record; a spec bypassed the guarded test): {'; '.join(unchecked[:5])}{more}"
    ]


# Playwright's debug and test-hook switches: PWDEBUG (the environment form of the refused --debug:
# headed browsers and the Inspector), PWPAUSE, PWTEST_*, PW_TEST_* (PW_TEST_CONNECT_* would use a
# remote browser) and the other PW_* internals. None belongs in a run inside the boundary.
PLAYWRIGHT_SWITCHES = re.compile(r"PW(?:DEBUG|PAUSE|TEST_|_)", re.IGNORECASE)


def playwright_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for the local Playwright runner (``node cli.js test``).

    Never downloads a browser, and carries none of Playwright's debug or test-hook switches
    (``PLAYWRIGHT_SWITCHES``). On Windows it also makes sure ``SystemDrive`` exists: a stripped
    environment (the synthetic ones the checks run in) lacks it, and Windows shell components
    that read a ``%SystemDrive%`` path then treat the unexpanded text as a relative path under
    the browser's working directory and create ``hub/%SystemDrive%/ProgramData/...`` inside the
    repository (seen in the first hub e2e run on the headless shell).
    """
    env = {
        name: value
        for name, value in (os.environ if base is None else base).items()
        if not PLAYWRIGHT_SWITCHES.match(name)
    }
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    if sys.platform == "win32" and not any(name.upper() == "SYSTEMDRIVE" for name in env):
        root = next((v for k, v in env.items() if k.upper() in ("SYSTEMROOT", "WINDIR")), "")
        drive = ntpath.splitdrive(root)[0]
        if drive:
            env["SystemDrive"] = drive
    return env
