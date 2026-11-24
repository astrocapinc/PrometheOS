"""The browser-test boundary's Python side (decision 0028): finding Playwright's Chromium headless
shell without ever installing it, and the configuration guards that keep Edge/Chrome out.

The browser itself is exercised by ``hub/e2e/00-boundary.spec.ts`` (every hub e2e run) and
``hub/walk/00-boundary.spec.ts`` (every C10-WALK run); nothing here launches a browser.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import browser_shell
import pytest
from browser_shell import BrowserUnavailable
from conftest import REPO

HUB = REPO / "hub"
REVISION = "1243"


def fake_hub(tmp_path: Path, revision: str = REVISION) -> Path:
    hub = tmp_path / "hub"
    manifest = hub / "node_modules" / "playwright-core" / "browsers.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium", "revision": "9999"},
                    {"name": "chromium-headless-shell", "revision": revision},
                ]
            }
        ),
        encoding="utf-8",
    )
    return hub


def install_shell(cache: Path, revision: str = REVISION, *, complete: bool = True) -> Path:
    directory = cache / f"chromium_headless_shell-{revision}"
    binary = directory / "chrome-headless-shell-win64" / "chrome-headless-shell.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ")
    if complete:
        (directory / "INSTALLATION_COMPLETE").write_bytes(b"")
    return binary


def test_the_expected_revision_comes_from_the_installed_playwright(tmp_path: Path) -> None:
    assert browser_shell.expected_revision(fake_hub(tmp_path)) == REVISION  # not "chromium"'s
    with pytest.raises(BrowserUnavailable, match=r"browsers\.json"):
        browser_shell.expected_revision(tmp_path / "no-hub")


def test_an_installed_shell_of_the_expected_revision_is_found(tmp_path: Path) -> None:
    hub = fake_hub(tmp_path)
    cache = tmp_path / "cache"
    binary = install_shell(cache)
    env = {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}
    assert browser_shell.find_headless_shell(hub, env) == binary
    assert browser_shell.require_headless_shell(hub, env) == binary


def test_the_local_browsers_switch_points_inside_node_modules(tmp_path: Path) -> None:
    hub = fake_hub(tmp_path)
    binary = install_shell(hub / "node_modules" / "playwright-core" / ".local-browsers")
    assert browser_shell.find_headless_shell(hub, {"PLAYWRIGHT_BROWSERS_PATH": "0"}) == binary


def test_a_missing_incomplete_or_other_revision_shell_is_refused_without_installing(
    tmp_path: Path,
) -> None:
    hub = fake_hub(tmp_path)
    cache = tmp_path / "cache"
    env = {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}
    with pytest.raises(BrowserUnavailable) as missing:
        browser_shell.require_headless_shell(hub, env)
    message = str(missing.value)
    assert REVISION in message and str(cache) in message
    assert "never download" in message and "Edge or Chrome" in message
    assert not cache.exists()  # looking for the browser creates nothing and fetches nothing
    install_shell(cache, "1217")  # a different revision does not count
    assert browser_shell.find_headless_shell(hub, env) is None
    install_shell(cache, complete=False)  # an interrupted install does not count either
    assert browser_shell.find_headless_shell(hub, env) is None
    (cache / f"chromium_headless_shell-{REVISION}" / "INSTALLATION_COMPLETE").write_bytes(b"")
    assert browser_shell.find_headless_shell(hub, env) is not None


def without_comments(text: str) -> str:
    """TypeScript without its // comments (they explain what is forbidden); `://` in a URL is
    not a comment, so the code after a URL on the same line is still checked."""
    return re.sub(r"(?<!:)//.*", "", text)


def typescript_code(path: Path) -> str:
    return without_comments(path.read_text(encoding="utf-8"))


def test_no_playwright_config_selects_an_installed_browser_or_the_proxy_option() -> None:
    forbidden = r"\bchannel\s*:|msedge|\bproxy\s*:|userDataDir|executablePath"
    boundary = typescript_code(HUB / "e2e" / "boundary.ts")
    for config in (
        "playwright.config.ts",
        "playwright.walk.config.ts",
        "playwright.walk18.config.ts",
        "playwright.walk21.config.ts",
        "playwright.walk22.config.ts",
    ):
        text = typescript_code(HUB / config)
        assert "...boundaryUse" in text and 'from "./e2e/boundary"' in text
        assert "projects: boundaryProjects()" in text  # neither project overrides `use`
        assert not re.search(forbidden, text), config
    assert not re.search(forbidden, boundary)
    # The controls are a dependency project of the journeys (so they run under any file filter or
    # --grep, and a failing control means no journey runs), and neither project sets `use`.
    projects = boundary[boundary.index("export function boundaryProjects") :]
    assert 'name: "boundary", testMatch: controls' in projects
    assert 'name: "chromium", testIgnore: controls, dependencies: ["boundary"]' in projects
    assert "use" not in projects
    # Raw Chromium arguments, because Playwright's proxy option force-appends <-loopback>.
    for argument in (
        "--proxy-server=",
        "--proxy-bypass-list=localhost;127.0.0.1;[::1]",
        "--host-resolver-rules=MAP * ~NOTFOUND",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-quic",
    ):
        assert argument in boundary
    # Each suite's first spec runs the controls against its own hub.
    assert (HUB / "e2e" / "00-boundary.spec.ts").is_file()
    assert (HUB / "walk" / "00-boundary.spec.ts").is_file()
    assert (HUB / "walk18" / "00-boundary.spec.ts").is_file()  # C18-WALK (decision 0036)
    assert (HUB / "walk21" / "00-boundary.spec.ts").is_file()  # C21-WALK (decision 0091)
    assert (HUB / "walk22" / "00-boundary.spec.ts").is_file()  # C22-WALK (decision 0099)


def test_this_checkout_expects_the_revision_the_decision_pins() -> None:
    if not (HUB / "node_modules").is_dir():
        pytest.skip("hub/node_modules is not installed")
    assert browser_shell.expected_revision(HUB) == REVISION


def test_the_e2e_hub_logs_every_api_request_without_its_query(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review cp10-browser R4-F2: the browser tests' hub.log names each API request (server,
    method, path, status, milliseconds), never its query, so a held-up request can be found."""
    import asyncio
    import os
    from unittest import mock

    with mock.patch.dict(os.environ):  # its synthetic-engine switches stay out of this process
        import hub_fixture

    sent: list[Any] = []

    async def answering(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b"busy"})

    async def failing(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("no answer")

    async def receive() -> Any:
        return {"type": "http.request"}

    async def send(message: Any) -> None:
        sent.append(message)

    def request(path: str, query: bytes = b"") -> dict[str, Any]:
        return {"type": "http", "method": "POST", "path": path, "query_string": query}

    apply = "/api/v1/papers/p1/overleaf/pull/apply"
    asyncio.run(
        hub_fixture.RequestLog(answering, "hub")(request(apply, b"t=secret"), receive, send)
    )
    asyncio.run(hub_fixture.RequestLog(answering, "hub")(request("/assets/a.js"), receive, send))
    with pytest.raises(RuntimeError):
        asyncio.run(
            hub_fixture.RequestLog(failing, "recovery")(request("/api/v1/session"), receive, send)
        )
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2  # API requests only, one line each
    assert re.fullmatch(rf"\d\d:\d\d:\d\d hub POST {apply} 503 \d+ ms", lines[0])
    assert re.fullmatch(
        r"\d\d:\d\d:\d\d recovery POST /api/v1/session no-response \d+ ms", lines[1]
    )
    assert "secret" not in "\n".join(lines)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"] * 2


def test_a_stripped_windows_environment_gets_a_system_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_shell.sys, "platform", "win32")
    stripped = {"SystemRoot": "C:/WINDOWS", "PATH": "x"}
    env = browser_shell.playwright_environment(stripped)
    assert env["SystemDrive"] == "C:" and env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"
    assert "SystemDrive" not in stripped  # the caller's mapping is not changed
    # An existing value, in any letter case, is kept; so is a drive other than C:.
    assert browser_shell.playwright_environment({"SYSTEMDRIVE": "D:", "WINDIR": "E:/W"}) == {
        "SYSTEMDRIVE": "D:",
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        "WINDIR": "E:/W",
    }
    assert browser_shell.playwright_environment({"windir": "E:/W"})["SystemDrive"] == "E:"
    monkeypatch.setattr(browser_shell.sys, "platform", "linux")
    assert "SystemDrive" not in browser_shell.playwright_environment({"SystemRoot": "C:/WINDOWS"})


def test_the_exact_browser_version_in_the_boundary_is_the_one_playwright_pins() -> None:
    manifest = HUB / "node_modules" / "playwright-core" / "browsers.json"
    if not manifest.is_file():
        pytest.skip("hub/node_modules is not installed")
    pinned = next(
        b["browserVersion"]
        for b in json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
        if b["name"] == "chromium-headless-shell"
    )
    declared = re.search(
        r'EXPECTED_BROWSER_VERSION = "([\d.]+)"', (HUB / "e2e" / "boundary.ts").read_text("utf-8")
    )
    assert declared and declared.group(1) == pinned  # a Playwright bump must update the boundary


def test_the_local_playwright_runner_is_used_and_its_absence_is_an_error(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    with pytest.raises(BrowserUnavailable, match="not installed"):
        browser_shell.playwright_command(hub)
    cli = hub / "node_modules" / "@playwright" / "test" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    command = browser_shell.playwright_command(hub)
    assert command[1:] == [str(cli), "test"] and "npx" not in command[0].lower()


JOURNEY = {"project": "chromium", "worker": 1, "version": "153.0.8010.12", "image": "x/shell.exe"}
# (project, worker index, status) of each test result a fake run reports.
RAN = (("boundary", 0, "passed"), ("chromium", 1, "passed"))


def evidence_file(
    path: Path,
    statuses: dict[str, str],
    *,
    all_passed: bool = True,
    journeys: list[dict[str, Any]] | None = None,
) -> Path:
    evidence: dict[str, Any] = {
        "decision": "0028",
        "launch_args": ["--proxy-server=http://127.0.0.1:9"],
        "observed": {"browser": {"name": "chromium", "image": "x/chrome-headless-shell.exe"}},
        "controls": [{"control": c, "status": s} for c, s in statuses.items()],
        "all_passed": all_passed,
    }
    if journeys is not None:
        evidence["journeys"] = journeys
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def report_file(path: Path, ran: tuple[tuple[str, int, str], ...]) -> Path:
    """A Playwright JSON report (the shape of playwright 1.63's json reporter) of these results:
    the first in a file's suite, the rest in a describe block inside it."""
    specs = [
        {
            "title": f"test {i}",
            "tests": [{"projectName": p, "results": [{"workerIndex": w, "status": s}]}],
        }
        for i, (p, w, s) in enumerate(ran)
    ]
    nested = {"title": "describe", "specs": specs[1:]}
    report = {"suites": [{"title": "x.spec.ts", "specs": specs[:1], "suites": [nested]}]}
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_boundary_evidence_must_show_every_control_passed(tmp_path: Path) -> None:
    path = tmp_path / "browser-boundary.json"
    assert browser_shell.boundary_evidence(path)[1]  # none
    path.write_text("{", encoding="utf-8")
    assert browser_shell.boundary_evidence(path)[1]  # unreadable
    evidence_file(path, {"a": "passed", "b": "failed"}, all_passed=False)
    summary, problems = browser_shell.boundary_evidence(path)
    assert problems and "'b'" in problems[0] and summary["all_passed"] is False
    evidence_file(path, {"a": "passed", "b": "failed"})  # a claim of success is not taken
    assert browser_shell.boundary_evidence(path)[1]
    evidence_file(path, {"a": "passed"}, all_passed=False)  # a control that never ran
    assert browser_shell.boundary_evidence(path)[1]
    evidence_file(path, {})
    assert browser_shell.boundary_evidence(path)[1]
    evidence_file(path, {"a": "passed", "b": "passed"})
    assert browser_shell.boundary_evidence(path) == (
        {
            "evidence": "browser-boundary.json",
            "all_passed": True,
            "controls": {"a": "passed", "b": "passed"},
            "browser": {"name": "chromium", "image": "x/chrome-headless-shell.exe"},
            "journey_browsers": [],
            "launch_args": ["--proxy-server=http://127.0.0.1:9"],
        },
        [],
    )


def test_every_test_outside_the_controls_must_run_in_a_guarded_worker(tmp_path: Path) -> None:
    """Review cp10-browser R3-F3: Playwright's report says which worker ran each test; a worker
    with no journey record ran a spec that bypassed the guard, and the run is not a pass."""
    report = tmp_path / "playwright-report.json"
    problems = browser_shell.journey_problems
    assert problems(report, [JOURNEY])  # no report
    report.write_text("[]", encoding="utf-8")
    assert problems(report, [JOURNEY])  # unreadable
    report_file(report, RAN)
    assert problems(report, [JOURNEY]) == []
    assert problems(report, [])  # a journey ran, but no worker was checked
    assert problems(report, [{**JOURNEY, "project": "boundary"}])  # nor its project's
    # C3: a second worker ran an unguarded spec beside a guarded one.
    report_file(report, (*RAN, ("chromium", 2, "failed")))
    (problem,) = problems(report, [JOURNEY])
    assert "describe > test 2 [chromium, worker 2]" in problem
    assert problems(report, [JOURNEY, {**JOURNEY, "worker": 2}]) == []
    # Only the controls ran (a boundary-only filter), or the rest was skipped: nothing to check.
    report_file(report, (("boundary", 0, "passed"), ("chromium", -1, "skipped")))
    assert problems(report, []) == []


class FakePlaywright:
    """Stands in for the Playwright runner: exit code, and optionally the evidence and the JSON
    report it writes."""

    def __init__(
        self,
        code: int,
        statuses: dict[str, str] | None,
        *,
        journeys: tuple[dict[str, Any], ...] = (JOURNEY,),
        ran: tuple[tuple[str, int, str], ...] = RAN,
    ) -> None:
        self.code, self.statuses, self.journeys, self.ran = code, statuses, journeys, ran
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        env = kwargs["env"]
        assert isinstance(env, dict)
        if self.statuses is not None:
            evidence = Path(env["BOUNDARY_EVIDENCE"])
            report = Path(env["PLAYWRIGHT_JSON_OUTPUT_FILE"])
            evidence_file(evidence, self.statuses, journeys=list(self.journeys))
            report_file(report, self.ran)
            assert Path(env["E2E_SHOTS"]).parent == evidence.parent == report.parent
        return subprocess.CompletedProcess(command, self.code)


@pytest.fixture
def run_hub_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    import run_hub_e2e as tool  # tools/ is on sys.path (conftest)

    hub = tmp_path / "hub"
    (hub / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(tool, "HUB", hub)
    monkeypatch.setattr(tool, "playwright_command", lambda _hub: ["node", "cli.js", "test"])
    monkeypatch.setattr(tool, "require_headless_shell", lambda _hub: Path("shell.exe"))
    return tool


def own_options(tool: Any) -> list[str]:
    """What the tool appends after the caller's arguments (the last value wins in Playwright)."""
    config = f"--config={tool.HUB / 'playwright.config.ts'}"
    return [config, "--retries=0", "--repeat-each=1", "--reporter=line,json"]


def test_a_browser_run_that_did_not_prove_its_boundary_is_never_a_pass(
    run_hub_e2e: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "evidence"
    argv = ["--evidence", str(folder), "e2e/05-capture.spec.ts", "-g", "journey"]
    # Playwright passed, but no boundary control ran (a spec filter without the dependency).
    ran = FakePlaywright(0, None)
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", ran)
    assert run_hub_e2e.main(argv) == 1
    assert ran.calls == [
        ["node", "cli.js", "test", *argv[2:], *own_options(run_hub_e2e)],
    ]
    # A control failed: the run is not a pass even if Playwright's own exit code were 0.
    monkeypatch.setattr(
        run_hub_e2e.subprocess, "run", FakePlaywright(0, {"a": "passed", "b": "failed"})
    )
    assert run_hub_e2e.main(argv) == 1
    # A stale evidence file from an earlier run is never taken for this run's.
    evidence_file(folder / "browser-boundary.json", {"a": "passed"}, journeys=[JOURNEY])
    report_file(folder / "playwright-report.json", RAN)
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", FakePlaywright(0, None))
    assert run_hub_e2e.main(argv) == 1
    # The controls passed, but a journey ran in a worker the guard did not check (R3-F3, C3).
    unguarded = FakePlaywright(0, {"a": "passed"}, ran=(*RAN, ("chromium", 2, "passed")))
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", unguarded)
    assert run_hub_e2e.main(argv) == 1
    unrecorded = FakePlaywright(0, {"a": "passed"}, journeys=())
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", unrecorded)
    assert run_hub_e2e.main(argv) == 1
    # Proven boundary and a passing Playwright run: a pass; a failing run keeps its own code.
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", FakePlaywright(0, {"a": "passed"}))
    assert run_hub_e2e.main(argv) == 0
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", FakePlaywright(3, {"a": "passed"}))
    assert run_hub_e2e.main(argv) == 3
    assert (tmp_path / "hub" / "node_modules").is_dir()


def test_the_boundary_controls_cannot_be_skipped_or_repeated_until_they_pass(
    run_hub_e2e: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = FakePlaywright(0, {"a": "passed"})
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", ran)
    spec = "e2e/05-capture.spec.ts"
    for refused in (
        ["--no-deps"],
        ["--retries=1"],
        ["--retries", "2"],
        ["--retries"],
        ["--repeat-each=2"],
        ["--repeat-each", "3"],
        ["--config=other.config.ts"],
        ["--config", "other.config.ts"],
        ["-c", "other.config.ts"],
        # Commander's attached value and short-option clusters (review cp10-browser R3-F2).
        ["-cother.config.ts"],
        ["-xc", "other.config.ts"],
        ["-xcother.config.ts"],
        # After `--` the tool's own options would become test filters.
        ["--"],
        # Another, a headed or a remote-UI browser.
        ["--browser=firefox"],
        ["--headed"],
        ["--debug"],
        ["--ui"],
        ["--ui-host=127.0.0.1"],
        ["--ui-port", "0"],
    ):
        assert run_hub_e2e.main([*refused, spec]) == 2, refused
    assert ran.calls == []  # nothing reached Playwright
    for harmless in (["--retries=0"], ["--retries", "0"], ["--repeat-each=1"], ["-g", "journey"]):
        assert run_hub_e2e.main([*harmless, spec]) == 0, harmless
        # Whatever the caller passed, the tool's configuration and values come last and win.
        tail = [*harmless, spec, *own_options(run_hub_e2e)]
        assert ran.calls[-1] == ["node", "cli.js", "test", *tail]
    listing = FakePlaywright(0, None)  # informational: no evidence, the same configuration
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", listing)
    assert run_hub_e2e.main(["--list"]) == 0
    configured = own_options(run_hub_e2e)[:3]
    assert listing.calls == [["node", "cli.js", "test", "--list", *configured, "--list"]]


def test_an_informational_flag_is_never_swallowed_as_an_options_value(
    run_hub_e2e: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review cp10-browser R4-F1: `--grep --list` used to run tests (grep "--list") on the
    unchecked informational path. The flag now comes last again, after the tool's options, where
    no option can take it as its value, so Playwright lists and runs nothing."""
    listing = FakePlaywright(0, None)
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", listing)
    spec = "e2e/01-pairing.spec.ts"
    configured = own_options(run_hub_e2e)[:3]
    for swallowing in (["--grep"], ["--grep-invert"], ["-G"], ["-xg"], ["--project"]):
        argv = [spec, *swallowing, "--list"]
        assert run_hub_e2e.main(argv) == 0, swallowing
        assert listing.calls[-1] == ["node", "cli.js", "test", *argv, *configured, "--list"]
    assert run_hub_e2e.main([spec, "--help", "-g", "x", "--list"]) == 0
    assert listing.calls[-1][-2:] == ["--help", "--list"]
    # An option that needs a value, given last, would take the tool's --config: refused.
    calls = len(listing.calls)
    for trailing in (["--grep"], ["-g"], ["-xg"], ["--workers"], ["--project"], ["--timeout"]):
        assert run_hub_e2e.main([spec, *trailing]) == 2, trailing
    assert len(listing.calls) == calls
    # A short cluster whose value letter is not last carries its value itself (-gx: grep "x").
    assert not run_hub_e2e.takes_next("-gx") and run_hub_e2e.takes_next("-xg")


def test_playwrights_debug_switches_never_reach_the_runner(
    run_hub_e2e: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PWDEBUG is the environment form of the refused --debug (headed browsers, the Inspector);
    it and Playwright's other debug and test-hook switches are not passed on (review R4)."""
    switches = {
        "PWDEBUG": "1",
        "pwdebug": "console",
        "PWDEBUGIMPL": "1",
        "PWPAUSE": "1",
        "PW_TEST_DEBUG_REPORTERS": "1",
        "PWTEST_HEADED_FOR_TEST": "1",
        "PW_TEST_CONNECT_WS_ENDPOINT": "ws://127.0.0.1:1/",
        "PW_CHROMIUM_ATTACH_TO_OTHER": "1",
    }
    kept = {"PWD": "/x", "PLAYWRIGHT_BROWSERS_PATH": "cache", "PATH": "p"}
    env = browser_shell.playwright_environment({**switches, **kept})
    assert {k: env[k] for k in kept} == kept
    assert not set(switches) & set(env)
    for name, value in switches.items():
        monkeypatch.setenv(name, value)
    ran = FakePlaywright(0, {"a": "passed"})
    seen: list[set[str]] = []

    def recording(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append({name.upper() for name in kwargs["env"]})
        return ran(command, **kwargs)

    monkeypatch.setattr(run_hub_e2e.subprocess, "run", recording)
    assert run_hub_e2e.main(["e2e/05-capture.spec.ts"]) == 0
    assert seen and not {name.upper() for name in switches} & seen[0]


def test_the_evidence_folder_is_one_place_for_the_tool_and_the_workers(
    run_hub_e2e: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Workers run in hub/: a relative folder must already be absolute when they are handed it.
    monkeypatch.chdir(tmp_path)
    seen: list[dict[str, str]] = []
    names = ("BOUNDARY_EVIDENCE", "E2E_SHOTS", "PLAYWRIGHT_JSON_OUTPUT_FILE")

    class Recording(FakePlaywright):
        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen.append({k: kwargs["env"][k] for k in names})
            assert kwargs["cwd"] == tmp_path / "hub"
            return super().__call__(command, **kwargs)

    monkeypatch.setattr(run_hub_e2e.subprocess, "run", Recording(0, {"a": "passed"}))
    assert run_hub_e2e.main(["--evidence", "runs/rel", "e2e/05-capture.spec.ts"]) == 0
    folder = tmp_path / "runs" / "rel"
    assert seen == [
        {
            "BOUNDARY_EVIDENCE": str(folder / "browser-boundary.json"),
            "E2E_SHOTS": str(folder / "screenshots"),
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(folder / "playwright-report.json"),
        }
    ]
    assert (folder / "browser-boundary.json").is_file()  # read where the worker wrote it
    assert not (tmp_path / "hub" / "runs").exists()
    # A value is required (a trailing --evidence used to become the current folder).
    assert run_hub_e2e.main(["e2e/05-capture.spec.ts", "--evidence"]) == 2
    assert run_hub_e2e.main(["--evidence", "--project=chromium"]) == 2
    assert run_hub_e2e.main(["--evidence="]) == 2
    assert run_hub_e2e.split_evidence(["a", "--evidence=x", "b"]) == ((tmp_path / "x"), ["a", "b"])
    assert run_hub_e2e.split_evidence(["--evidence", "y", "-g", "z"]) == (
        tmp_path / "y",
        ["-g", "z"],
    )


def files_in(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def test_the_tool_never_clears_a_folder_it_did_not_create(
    run_hub_e2e: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review cp10-browser R3-F4: a mistaken --evidence (a user's Pictures folder, which Windows
    matches to `screenshots` case-insensitively) is refused and left exactly as it was."""
    ran = FakePlaywright(0, {"a": "passed"})
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", ran)
    pictures = tmp_path / "Pictures"
    (pictures / "Screenshots").mkdir(parents=True)
    (pictures / "Screenshots" / "holiday.png").write_bytes(b"image")
    (pictures / "test-results").mkdir()
    (pictures / "test-results" / "thesis-grades.csv").write_text("x", encoding="utf-8")
    (pictures / "hub.log").write_text("mine", encoding="utf-8")
    before = files_in(pictures)
    assert run_hub_e2e.main(["--evidence", str(pictures), "e2e/05-capture.spec.ts"]) == 2
    assert files_in(pictures) == before and ran.calls == []
    # A file is not a folder either; it is left alone.
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert run_hub_e2e.main(["--evidence", str(tmp_path / "file")]) == 2
    assert (tmp_path / "file").read_text(encoding="utf-8") == "x"
    # An existing empty folder, like a new one, becomes the tool's own (marked) folder.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_hub_e2e.main(["--evidence", str(empty), "e2e/05-capture.spec.ts"]) == 0
    assert (empty / ".prometheos-e2e-evidence").is_file()


def test_a_reused_evidence_folder_starts_clean_and_a_failure_names_it(
    run_hub_e2e: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "evidence"
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", FakePlaywright(0, {"a": "passed"}))
    assert run_hub_e2e.main(["--evidence", str(folder), "e2e/05-capture.spec.ts"]) == 0
    assert (folder / ".prometheos-e2e-evidence").is_file()  # the tool's own folder from now on
    (folder / "screenshots").mkdir()
    (folder / "screenshots" / "old.png").write_bytes(b"x")
    (folder / "test-results").mkdir()
    (folder / "test-results" / "old.zip").write_bytes(b"x")
    (folder / "hub.log").write_text("old", encoding="utf-8")
    (folder / "notes.txt").write_text("someone else's", encoding="utf-8")
    # The rerun writes no evidence: what is there afterwards is only what the cleanup kept.
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", FakePlaywright(0, None))
    assert run_hub_e2e.main(["--evidence", str(folder), "e2e/05-capture.spec.ts"]) == 1
    assert files_in(folder) == [".prometheos-e2e-evidence", "notes.txt"]
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "someone else's"
    # Playwright itself failing: the code is kept and the folder is named for the receipt reader.
    monkeypatch.setattr(run_hub_e2e.subprocess, "run", FakePlaywright(1, {"a": "passed"}))
    capsys.readouterr()
    assert run_hub_e2e.main(["--evidence", str(folder), "e2e/05-capture.spec.ts"]) == 1
    assert str(folder) in capsys.readouterr().out


# Only these modules may bind Playwright's plain `test`: they are the boundary itself, the guard
# that builds the guarded `test` from it, and the controls (which check the launch themselves).
BOUNDARY_MODULES = {"boundary.ts", "boundary-guard.ts", "boundary-checks.ts"}
# A module specifier after `from`, `import` or `require`.
SPECIFIER = re.compile(r"""(?:\bfrom|\bimport|\brequire)\s*\(?\s*["']([^"'\n]+)["']""")
# Add-ons named after Playwright that take a page and launch nothing (the a11y spec's axe).
PLAYWRIGHT_ADDONS = {"@axe-core/playwright"}
# The one accepted way to use @playwright/test: named (and type) imports.
NAMED_IMPORT = re.compile(
    r"""\bimport\s+(?:type\s+)?\{([^}]*)\}\s*from\s*["']@playwright/test["']"""
)
# Exports that are Playwright's own `test` (the default export is `test` too) or a browser type
# that can launch a browser outside the boundary.
UNGUARDED = {"test", "default", "chromium", "firefox", "webkit", "_electron", "_android"}
# Options that select, launch, connect or proxy the browser: never set by a spec.
LAUNCH_KEYS = re.compile(
    r"\b(?:launchOptions|contextOptions|connectOptions|channel|browserName|defaultBrowserType"
    r"|executablePath|proxy|headless|userDataDir)\b"
)
OWN_BROWSER = re.compile(
    r"\.\s*(?:launch|launchPersistentContext|launchServer|connect|connectOverCDP)\s*\("
)


def call_argument(text: str, open_paren: int) -> str:
    """The text between the parenthesis at ``open_paren`` and the one that closes it."""
    depth = 0
    for index in range(open_paren, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : index].strip()
    return text[open_paren + 1 :].strip()


def boundary_bypasses(code: str) -> list[str]:
    """What in a spec or helper module (code without comments) could run a journey without the
    guard (hub/e2e/boundary-guard.ts) or a browser outside the boundary (review cp10-browser
    R3-F3). Static and deliberately strict; the guard and the run tools check at run time."""
    found = []
    named = NAMED_IMPORT.findall(code)
    for names in named:
        for entry in (n.strip() for n in names.split(",")):
            imported = entry.split(" as ")[0].strip()
            if entry and not entry.startswith("type ") and imported in UNGUARDED:
                found.append(f"imports {imported!r} from @playwright/test")
    playwright = [
        s
        for s in SPECIFIER.findall(code)
        if "playwright" in s.lower() and s not in PLAYWRIGHT_ADDONS
    ]
    if len(playwright) != len(named):
        found.append(
            "binds Playwright other than by a named import from @playwright/test "
            f"(a namespace, default, path, require, dynamic import or re-export): {playwright}"
        )
    for call, literal_only in ((".use", True), (".newContext", False)):
        for match in re.finditer(re.escape(call) + r"\s*\(", code):
            argument = call_argument(code, match.end() - 1)
            if (argument or literal_only) and (
                not argument.startswith("{") or "..." in argument or LAUNCH_KEYS.search(argument)
            ):
                found.append(f"{call}({argument[:60]}) is not a plain literal of allowed options")
    if re.search(r"\bproxy\b", code):
        found.append("sets a proxy (a context-level proxy replaces the boundary's dead proxy)")
    if OWN_BROWSER.search(code):
        found.append("launches or connects a browser itself")
    return found


def test_the_bypass_check_refuses_the_review_probes_and_their_variants() -> None:
    """The reviewer's guard probes (C1 launch override, C2 context proxy, C3 aliased plain test)
    and other spellings of the same bypasses are refused; ordinary journey code is not."""
    guarded = 'import { guardedTest as test } from "./boundary-guard";\n'
    refused = {
        "C1": guarded + 'test.use({ launchOptions: { args: [...BOUNDARY_ARGS, "--x"] } });',
        "C2": guarded + "test.use({ proxy: { server: `http://127.0.0.1:${PORT}` } });",
        "C2 by path": 'import { expect } from "../node_modules/@playwright/test";',
        "C3": 'import { test as base } from "@playwright/test";\n'
        "const journey = base.extend({});\njourney.use({ launchOptions: {} });",
        "plain test": 'import { expect, test } from "@playwright/test";',
        "namespace": 'import * as pw from "@playwright/test";',
        "default": 'import test from "@playwright/test";',
        "default and named": 'import base, { expect } from "@playwright/test";',
        "default by name": 'import { default as base } from "@playwright/test";',
        "require": 'const { test } = require("@playwright/test");',
        "dynamic import": 'const pw = await import("@playwright/test");',
        "re-export": 'export { test } from "@playwright/test";',
        "core": 'import { chromium } from "playwright-core";',
        "browser type": 'import { chromium } from "@playwright/test";',
        "context proxy": "await browser.newContext({ proxy: { server: 'http://x' } });",
        "context options": 'test.use({ contextOptions: { locale: "en" } });',
        "spread use": "test.use({ ...devices['Desktop Firefox'] });",
        "variable use": "test.use(options);",
        "variable context": "await browser.newContext(options);",
        "launch": "const b = await playwright.chromium.launch();",
        "connect": "await chromium.connectOverCDP(endpoint);",
        "after a URL": 'await page.goto("http://127.0.0.1/"); test.use({ channel: "msedge" });',
    }
    for case, code in refused.items():
        assert boundary_bypasses(without_comments(code)), case
    allowed = (
        guarded + 'import { expect, request as pwRequest, type Page } from "@playwright/test";\n'
        'import type { APIRequestContext } from "@playwright/test";\n'
        'import AxeBuilder from "@axe-core/playwright";\n'
        "test.use({ viewport: { width: 390, height: 844 } });\n"
        "const ctx = await pwRequest.newContext({ baseURL: origin });\n"
        "const context = await browser.newContext();\n"
        '// a comment may explain test.use({ proxy }) and import { test } from "@playwright/test"\n'
        'await page.goto("http://127.0.0.1:9/");\n'
    )
    assert boundary_bypasses(without_comments(allowed)) == []


def test_every_journey_runs_with_the_guarded_test_and_no_launch_override() -> None:
    """Journeys' browsers are checked per worker and their contexts per test
    (hub/e2e/boundary-guard.ts); no spec or helper module may override the launch, set a proxy,
    launch or connect a browser itself, or skip the guard by binding Playwright's plain test."""
    modules = sorted(
        [*(HUB / "e2e").glob("*.ts"), *(HUB / "walk").glob("*.ts"), *(HUB / "walk18").glob("*.ts")]
    )
    checked = [m for m in modules if m.name not in BOUNDARY_MODULES]
    assert {m.name for m in modules} - {m.name for m in checked} == BOUNDARY_MODULES
    for module in checked:
        assert boundary_bypasses(typescript_code(module)) == [], module.name
    # The retries are pinned in both configs, as everywhere: a failure is a stop.
    for config in (
        "playwright.config.ts",
        "playwright.walk.config.ts",
        "playwright.walk18.config.ts",
    ):
        text = typescript_code(HUB / config)
        assert "retries: 0" in text and "workers: 1" in text, config


def test_every_hub_e2e_command_of_the_suites_keeps_its_evidence_in_its_run_folder() -> None:
    suites = json.loads((REPO / "tools" / "checkpoint_suites.json").read_text(encoding="utf-8"))
    folders: dict[str, list[str]] = {}
    for checkpoint, checks in suites["suites"].items():
        for check, spec in checks.items():
            for name, command in spec["commands"].items():
                if "tools/run_hub_e2e.py" not in command:
                    continue
                at = command.index("tools/run_hub_e2e.py")
                assert command[at + 1] == "--evidence", (checkpoint, check, name)
                value = command[at + 2]
                # One folder per command, named after it (command names are unique per run).
                assert value == f"{{run_dir}}/{name}-evidence", (checkpoint, check, name, value)
                folders.setdefault(f"{checkpoint}:{value}", []).append(f"{check}/{name}")
    # CP18 adds browser-work-items and browser-events; CP17 adds browser-adoption; CP11 adds
    # browser-library and browser-matrix.
    assert len(folders) == 19
    assert all(len(users) == 1 for users in folders.values()), folders
