"""Run the hub's Playwright browser tests (Chromium headless shell, real hub, synthetic fixture).

    uv run python tools/run_hub_e2e.py [--evidence DIR] [spec ...] [playwright options]

Runs the project's own Playwright runner inside ``hub/`` (``node_modules/@playwright/test/cli.js``,
never ``npx``) against the assets shipped in the package (build them first with
``tools/build_hub.py``). The browser is Playwright's own Chromium headless shell, confined to
loopback by the boundary in ``hub/e2e/boundary.ts`` (decision 0028). Its negative controls
(``e2e/00-boundary.spec.ts``) are the ``boundary`` project, a dependency of the journeys, so they
run first under any spec or ``--grep`` filter; this tool also requires their evidence
(``browser-boundary.json``) to show every control passed, and Playwright's JSON report to show
that every other test ran in a worker whose browser the journey guard checked
(``hub/e2e/boundary-guard.ts``), and fails the run otherwise, so a run that did not prove its
boundary is never a pass. The tool's own configuration, ``--retries=0``, ``--repeat-each=1`` and
reporters are appended after the caller's arguments (for Playwright's command line the last value
wins). Options that would let the proof be bypassed or repeated until it passes, replace the
configuration, or drive another or a headed browser are refused: ``--no-deps``, ``--retries`` and
``--repeat-each`` (other than 0 and 1), ``--config``/``-c`` in any spelling (``-cFILE``, or a
short-option cluster with ``c``), ``--``, ``--browser``, ``--headed``, ``--debug`` and the
``--ui`` options (which can open Playwright's UI in the system's browser), and an option that
needs a value given last. ``--list``, ``--help`` and ``--version`` are passed again after the
tool's options, so Playwright only lists or prints; Playwright's debug environment switches
(``PWDEBUG`` and the like) are not passed on. Never downloads browsers: where the shell is not
installed the run fails with a clear message.

``--evidence DIR`` keeps the run's evidence in ``DIR`` (a relative path is taken from the current
directory): the boundary evidence, Playwright's JSON report, the supplementary screenshots, the
hub's log and, when the run fails, ``hub/test-results``. The tool marks a folder it uses with
``.prometheos-e2e-evidence``; before a run it removes what an earlier run left in a marked folder
(exactly those names, nothing else) and refuses a non-empty folder without the mark, changing
nothing there. Without ``--evidence`` a temporary folder is used and removed after a passing run.
Screenshots never go to the tracked ``hub/screenshots``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from browser_shell import (
    BrowserUnavailable,
    boundary_evidence,
    journey_problems,
    playwright_command,
    playwright_environment,
    require_headless_shell,
)

HUB = Path(__file__).resolve().parents[1] / "hub"
CONFIG = "playwright.config.ts"
INFORMATIONAL = {"--list", "--help", "-h", "--version", "-v"}
REPORT = "playwright-report.json"
# What this tool writes into an evidence folder; a reused (marked) folder is cleared of these.
EVIDENCE_NAMES = ("browser-boundary.json", REPORT, "screenshots", "hub.log", "test-results")
MARKER = ".prometheos-e2e-evidence"
MARKER_TEXT = (
    "Evidence folder of tools/run_hub_e2e.py (decision 0028). Before each run the tool removes\n"
    f"what an earlier run left here ({', '.join(EVIDENCE_NAMES)}) and nothing else.\n"
)
# Playwright options that would let a failed control be repeated until it passes: option -> the
# one value that is harmless.
REFUSED_VALUES = {"--retries": "0", "--repeat-each": "1"}
# Options that would skip the controls, replace the configuration (and with it the boundary), turn
# the tool's own options into test filters (`--`), drive another or a headed browser, or open
# Playwright's UI (with --ui-host, in the system's browser).
REFUSED = {
    "--no-deps",
    "--config",
    "-c",
    "--",
    "--browser",
    "--headed",
    "--debug",
    "--ui",
    "--ui-host",
    "--ui-port",
}
# Playwright `test` options that read the next argument as their value, whatever it is, and the
# short letters among them (-c is refused above). A value option given last would take the tool's
# own --config as its value, so it is refused; an informational flag that one of them swallows
# still comes last again, after the tool's options (review cp10-browser R4-F1).
VALUE_OPTIONS = {
    "--add-reporter",
    "--global-timeout",
    "-g",
    "--grep",
    "-G",
    "--grep-invert",
    "--last-failed-file",
    "--max-failures",
    "--output",
    "--project",
    "--repeat-each",
    "--reporter",
    "--retries",
    "--run-agents",
    "--shard",
    "--test-list",
    "--test-list-invert",
    "--timeout",
    "--trace",
    "--tsconfig",
    "--update-source-method",
    "-j",
    "--workers",
}
VALUE_LETTERS = "gGj"


def tool_options() -> list[str]:
    """Appended after the caller's arguments, so these values win (Commander: the last one)."""
    return [f"--config={HUB / CONFIG}", "--retries=0", "--repeat-each=1"]


def split_evidence(argv: list[str]) -> tuple[Path | None, list[str]]:
    """Take ``--evidence DIR`` (or ``--evidence=DIR``) out of the arguments, as an absolute path.

    The folder is handed to Playwright workers that run in ``hub/``, so a relative path is made
    absolute against this process' directory first: the tool and the workers must use one place.
    """
    rest: list[str] = []
    evidence: Path | None = None
    arguments = iter(argv)
    for argument in arguments:
        value: str | None
        if argument == "--evidence":
            value = next(arguments, None)
        elif argument.startswith("--evidence="):
            value = argument.partition("=")[2]
        else:
            rest.append(argument)
            continue
        if not value or value.startswith("-"):
            raise ValueError("--evidence needs a folder")
        evidence = Path(value).expanduser().resolve()
    return evidence, rest


def takes_next(argument: str) -> bool:
    """Whether Playwright's command line reads the next argument as this option's value."""
    if argument in VALUE_OPTIONS:
        return True
    if re.fullmatch(r"-[A-Za-z]{2,}", argument):  # a short cluster: its first value letter
        for index, letter in enumerate(argument[1:], start=1):  # takes the rest, or the next
            if letter in VALUE_LETTERS:
                return index == len(argument) - 1
    return False


def refused_option(argv: list[str]) -> str | None:
    """The first option that must not reach Playwright, with the reason, or None."""
    arguments = iter(argv)
    for argument in arguments:
        name, equals, inline = argument.partition("=")
        if name in REFUSED or argument in REFUSED:
            return f"{name} would skip, replace or escape the browser boundary; it is not accepted"
        if argument.startswith("-") and not argument.startswith("--") and "c" in argument[1:]:
            # Commander reads `-cFILE` and a cluster such as `-xc FILE` as --config.
            return (
                f"{argument} could be read as -c (--config), which would replace the browser "
                "boundary; give a short option's value as a separate argument"
            )
        if name in REFUSED_VALUES:
            value = inline if equals else next(arguments, "")
            if value != REFUSED_VALUES[name]:
                return (
                    f"{name} {value} could repeat a failed boundary control until it passes; "
                    "a failure is a stop, not a retry"
                )
    if argv and takes_next(argv[-1]):
        return (
            f"{argv[-1]} needs a value: given last, it would take the tool's own configuration "
            "as its value"
        )
    return None


def prepare_folder(folder: Path) -> str | None:
    """Make ``folder`` this run's evidence folder, or say why it cannot be (nothing is changed).

    Only a folder the tool marked as its own is cleared, and only of the names it writes there; a
    new or empty folder is marked; any other folder is refused (a mistaken ``--evidence`` must
    never delete someone else's files).
    """
    if folder.exists() and not folder.is_dir():
        return f"the evidence folder {folder} is not a folder"
    marker = folder / MARKER
    if not marker.is_file():
        if folder.is_dir() and any(folder.iterdir()):
            return (
                f"refusing to use {folder} for evidence: it is not empty and has no {MARKER} "
                "(it was not created by this tool); nothing in it was changed"
            )
        folder.mkdir(parents=True, exist_ok=True)
        marker.write_text(MARKER_TEXT, encoding="utf-8")
        return None
    for name in EVIDENCE_NAMES:  # never an earlier run's evidence beside this run's
        stale = folder / name
        if stale.is_dir():
            shutil.rmtree(stale)
        else:
            stale.unlink(missing_ok=True)
    return None


def main(argv: list[str]) -> int:
    try:
        evidence_dir, argv = split_evidence(argv)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    reason = refused_option(argv)
    if reason is not None:
        print(f"FAIL: {reason}")
        return 2
    if not (HUB / "node_modules").is_dir():
        print("FAIL: hub/node_modules missing; run tools/build_hub.py first")
        return 2
    try:
        command = playwright_command(HUB)
        print(f"browser: {require_headless_shell(HUB)}", flush=True)
    except BrowserUnavailable as exc:
        print(f"FAIL: {exc}")
        return 2
    env = playwright_environment()
    env.pop("HUB_STATIC", None)  # always the packaged assets
    informational = list(dict.fromkeys(a for a in argv if a in INFORMATIONAL))
    if informational:
        # Last, after the tool's options: an option that takes the next argument as its value
        # (`--grep --list`) cannot swallow them, so Playwright only lists or prints and runs
        # nothing; that is why this path needs no evidence (review cp10-browser R4-F1).
        return subprocess.run(
            [*command, *argv, *tool_options(), *informational], cwd=HUB, env=env, check=False
        ).returncode

    kept = evidence_dir is not None
    folder = evidence_dir or Path(tempfile.mkdtemp(prefix="prometheos-e2e-"))
    problem = prepare_folder(folder)
    if problem is not None:
        print(f"FAIL: {problem}")
        return 2
    evidence = folder / "browser-boundary.json"
    report = folder / REPORT
    env["BOUNDARY_EVIDENCE"] = str(evidence)
    env["E2E_SHOTS"] = str(folder / "screenshots")
    env["PLAYWRIGHT_JSON_OUTPUT_FILE"] = str(report)
    code = subprocess.run(
        [*command, *argv, *tool_options(), "--reporter=line,json"], cwd=HUB, env=env, check=False
    ).returncode
    summary, problems = boundary_evidence(evidence)
    if not problems:
        problems = journey_problems(report, summary["journey_browsers"])
    if kept:
        log = HUB / "e2e" / ".state" / "hub.log"
        if log.is_file():
            shutil.copyfile(log, folder / "hub.log")
        results = HUB / "test-results"
        if code != 0 and results.is_dir():
            shutil.copytree(results, folder / "test-results", dirs_exist_ok=True)
    if problems:
        print(f"FAIL: {'; '.join(problems)} (evidence folder: {folder})")
        return code or 1
    browser = summary["browser"] or {}
    print(
        f"boundary: all {len(summary['controls'])} controls passed in {browser.get('image')}; "
        f"journey browsers: {len(summary['journey_browsers'])}"
    )
    if code != 0:
        print(f"FAIL: Playwright exited {code} (evidence folder: {folder})")
    elif not kept:
        shutil.rmtree(folder, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
