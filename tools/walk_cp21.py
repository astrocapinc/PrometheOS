"""C21-WALK: the CP21 end-to-end walk on the web-first application (spec/10 CP21.4; decision 0091).

    uv run python tools/walk_cp21.py [--out DIR] [--print-transcript] [--only hub|mcp] [--keep]

1. **Hub leg (store A).** The core is installed as a uv tool (editable, from this checkout, into a
   temporary tool folder, offline, pinned to the lockfile) and its ``prometheos start`` launches
   the hub for a fresh home; a stand-in browser (``BROWSER``) records the URL ``start`` opens. A
   Playwright spec (``hub/walk21/walk.spec.ts``, the headless shell inside the loopback-only
   boundary of decision 0028, which admits the home's own host name by name only; proved first by
   ``hub/walk21/00-boundary.spec.ts``) does the owner's steps at the home's own host name: a
   second ``start`` opens a paired browser on the running hub and the page pairs from the
   fragment; a note is captured; the agent's approval request is reviewed and granted. Between
   them an agent session over the real ``prometheos mcp serve --stdio`` process (the official MCP
   SDK client) suggests a work item, asks to acknowledge its event and resumes the granted
   request: CP19's story (tools/walk_cp19.py). Then the core restarts (``stop``, ``start
   --no-open``) and a reload resumes the session from the remembered browser, and a fresh agent
   session reads the result.
2. **The launcher and pairing steps (hub leg only, S66 and S67).** A forged process id in the
   advisory metadata takes nothing over; a used and an expired launch code are refused; the
   browser's device credential is refused with a hostile Origin, on 127.0.0.1 and by another
   home's hub; a killed hub leaves no lock and the next ``start`` keeps the port, after which a
   reload resumes again; revoking the browser on the Recovery page signs it out and ``device
   list`` shows it revoked. No launch code reaches a request line, the history entry or the hub's
   log. These steps change no record.
3. **MCP leg (store B).** On a second fresh store, with no hub running, CP19's script through MCP
   alone (the owner's steps through the service in the harness, a synthetic owner channel).
4. The two stores' normalised outcomes (taken on store A after the last scripted step), every
   scripted step's outcome and the normalised answer of every agent step must be equal.

Output in ``--out`` (a new folder): ``transcript.json`` and ``transcript.md``, ``outcome-hub.json``
and ``outcome-mcp.json``, ``evidence/``, ``hub-steps.jsonl`` (what the browser showed), the
screenshots, ``browser-boundary.json``, ``playwright-report.json`` and the Playwright log. Exit 0
only when every assertion passed and the outcomes are equal.

Synthetic data only; nothing is sent anywhere. Never point it at a real home.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

from browser_shell import (  # noqa: E402
    BrowserUnavailable,
    playwright_command,
    playwright_environment,
    require_headless_shell,
)
from walk_cp10 import boundary_evidence, check, first_difference  # noqa: E402
from walk_cp18 import McpChat  # noqa: E402

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _private_cp19() -> Any:
    """A private copy of tools/walk_cp19.py: this walk gives it its own script and words, which
    must never change the module C19-WALK and its tests use."""
    spec = importlib.util.spec_from_file_location(
        "_walk_cp19_for_c21", ROOT / "tools" / "walk_cp19.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


walk_cp19 = _private_cp19()
# CP19's story with the hub's own words (both legs use them, so the stores stay equal).
walk_cp19.NOTE_TITLE = "Walk note from the hub"
walk_cp19.NOTE_TEXT = "Captured in the browser hub (C21-WALK)."

LAUNCHER_STEPS: list[dict[str, Any]] = [
    {"id": "L1", "actor": "owner", "operation": "a second start opens a paired browser on the "
     "running hub and starts no second hub"},
    {"id": "L2", "actor": "owner", "operation": "forge the advisory metadata's process id"},
    {"id": "L3", "actor": "owner", "operation": "replay launch codes; present the device "
     "credential with a hostile Origin, on 127.0.0.1 and to another home's hub"},
    {"id": "L4", "actor": "owner", "operation": "kill the hub, start again; a reload resumes"},
    {"id": "L5", "actor": "owner", "operation": "revoke this browser on the Recovery page"},
]  # fmt: skip
WORDS = {
    "W1.1": "open the hub from the launcher; the page pairs from the fragment",
    "W2.1": "capture a note in the hub",
    "W4.1": "the approval request waits on the Approvals page",
    "W4.2": "review the request's plan in the hub and grant it",
    "W6.1": "restart the core (stop, start); a reload resumes from the remembered browser",
}
# In the order the hub leg runs them: L1 (the second start) opens the browser for W1.1.
STEPS = [
    LAUNCHER_STEPS[0],
    *({**s, "operation": WORDS.get(s["id"], s["operation"])} for s in walk_cp19.STEPS),
    *LAUNCHER_STEPS[1:],
]
# The recorder of C19 looks steps up in its module's list: give it this walk's script.
walk_cp19.STEPS = STEPS
walk_cp19.SCRIPT_IDS = [s["id"] for s in STEPS]
SCRIPT_IDS = [s["id"] for s in walk_cp19.STEPS if not s["id"].startswith("L")]
AGENT_IDS = [s["id"] for s in STEPS if s["actor"] == "agent"]
BROWSER_IDS = ["W1.1", "W2.1", "W4.1", "W4.2", "W6.1", "L4", "L5"]


class Store(walk_cp19.Store):
    def __init__(self, work: Path) -> None:
        from prometheos.app.reads import Caller
        from prometheos.app.service import Service

        self.work, self.due = work, {}
        self.home = work / "home"
        self.svc = Service(home=self.home, actor="hub")
        self.owner = Caller(actor="hub-owner", scope="owner")
        self.pid: str = self.svc.init_project(
            title="Synthetic study (C21-WALK)",
            objective="Show the web-first hub, its pairing and the agent's approval flow.",
            timezone=walk_cp19.ZONE,
        )["project_id"]
        self.names: dict[str, str] = {}


# ----- the launcher ---------------------------------------------------------------------------


def tool_install(work: Path) -> dict[str, Any]:
    """``uv tool install --editable`` of this checkout into a temporary tool folder, offline and
    pinned to the lockfile; answers the installed ``prometheos`` executable."""
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is needed to install the core as a tool")
    constraints = work / "constraints.txt"
    exported = subprocess.run(
        [uv, "export", "--frozen", "--no-hashes", "--no-dev", "--no-emit-project",
         "--format", "requirements-txt", "-o", str(constraints)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if exported.returncode != 0:
        raise RuntimeError(f"uv export failed: {exported.stderr[-600:]}")
    env = {**os.environ, "UV_TOOL_DIR": str(work / "tool"), "UV_TOOL_BIN_DIR": str(work / "bin")}
    base = [uv, "tool", "install", "--editable", str(ROOT), "-c", str(constraints)]
    # Offline first (uv's cache); a pinned wheel the cache lacks comes from the package index,
    # and the transcript says so.
    network = False
    done = subprocess.run([*base, "--offline"], cwd=work, env=env, capture_output=True,
                          text=True, check=False)  # fmt: skip
    if done.returncode != 0:
        network = True
        done = subprocess.run(base, cwd=work, env=env, capture_output=True, text=True,
                              check=False)  # fmt: skip
    exe = work / "bin" / ("prometheos.exe" if sys.platform == "win32" else "prometheos")
    if done.returncode != 0 or not exe.is_file():
        raise RuntimeError(f"uv tool install failed: {done.stderr[-800:]}")
    version = subprocess.run([uv, "--version"], capture_output=True, text=True).stdout.strip()
    return {"exe": str(exe), "uv": version, "package_index_used": network}


class Launcher:
    """The installed ``prometheos`` for one home, with a stand-in browser that records URLs."""

    def __init__(self, exe: Path, home: Path, work: Path) -> None:
        self.exe, self.home = exe, home
        self.opened = work / "opened.txt"
        browser = work / "browser.py"
        browser.write_text(
            "import sys\nwith open(sys.argv[1], 'a', encoding='utf-8') as f:\n"
            "    f.write(sys.argv[2] + '\\n')\n",
            encoding="utf-8",
        )
        exe_, script, out = (Path(p).as_posix() for p in (sys.executable, browser, self.opened))
        self.env = {**os.environ, "PROMETHEOS_HOME": str(home), "PYTHONIOENCODING": "utf-8",
                    "BROWSER": f'"{exe_}" "{script}" "{out}" %s'}  # fmt: skip
        self.env.pop("PROMETHEOS_PYTHON", None)
        self.calls: list[dict[str, Any]] = []

    def run(self, *args: str, timeout: float = 120) -> dict[str, Any]:
        done = subprocess.run([str(self.exe), *args], env=self.env, capture_output=True,
                              text=True, timeout=timeout, check=False)  # fmt: skip
        try:
            body = json.loads(done.stdout) if done.stdout.strip() else {}
        except ValueError:
            body = {"text": done.stdout[-400:]}
        call = {"argv": ["prometheos", *args], "exit": done.returncode, "body": body,
                "stdout": done.stdout, "stderr": done.stderr}  # fmt: skip
        self.calls.append(call)
        return call

    def urls(self) -> list[str]:
        if not self.opened.exists():
            return []
        return self.opened.read_text(encoding="utf-8").split()

    def meta(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(
            (self.home / "launcher" / "hub.json").read_text(encoding="utf-8")
        )
        return data

    def running(self) -> bool:
        return bool(self.run("status", "--json")["body"].get("running"))


def code_of(url: str) -> str:
    return url.split("#pair=", 1)[1] if "#pair=" in url else ""


def post(port: int, path: str, host: str, origin: str | None, body: Any,
         cookie: str | None = None) -> tuple[int, dict[str, Any]]:  # fmt: skip
    """A request to the hub on 127.0.0.1 with the given Host, Origin and cookie."""
    headers = {"Host": host, "Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    if cookie is not None:
        headers["Cookie"] = cookie
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("POST", path, body=json.dumps(body), headers=headers)
        reply = connection.getresponse()
        raw = reply.read()
        try:
            return reply.status, json.loads(raw or b"{}")
        except ValueError:
            return reply.status, {"text": raw[:200].decode("utf-8", "replace")}
    finally:
        connection.close()


def without_secrets(call: dict[str, Any], codes: list[str]) -> dict[str, Any]:
    """A launcher call for the evidence, with every launch code replaced."""
    text = json.dumps(call)
    for code in codes:
        if code:
            text = text.replace(code, "<launch-code>")
    out: dict[str, Any] = json.loads(text)
    return out


# ----- the hub leg -----------------------------------------------------------------------------


class Leg(walk_cp19.Leg):
    pass


async def hub_story(
    st: Store, rec: walk_cp19.Recorder, leg: Leg, owner: dict[str, list[dict[str, Any]]],
    launcher: Launcher, state: dict[str, Any], work: Path,
) -> None:  # fmt: skip
    before = state["before"]
    await leg.wait("ready", 300)  # the boundary controls have passed against this hub
    # L1: a second start opens a paired browser on the running hub and starts no second hub.
    first_meta = launcher.meta()
    second = launcher.run("start", "--json")
    urls = launcher.urls()
    state["codes"] = [code_of(u) for u in urls]
    second_meta = launcher.meta()
    owner["L1"] = [
        check("the second start answered that the hub was already running",
              second["exit"] == 0 and second["body"].get("already_running") is True,
              second["body"]),
        check("no second hub: the same process serves on the same port",
              (second_meta["pid"], second_meta["port"]) == (first_meta["pid"], first_meta["port"]),
              {"before": first_meta["pid"], "after": second_meta["pid"]}),
        check("it opened the browser again, at the home's own host name with a new code",
              len(urls) == 2 and urls[1].startswith(state["origin"] + "/#pair=")
              and code_of(urls[1]) not in ("", code_of(urls[0]))),
        check("neither start printed a launch code",
              not any(c and c in call["stdout"] + call["stderr"]
                      for c in state["codes"] for call in launcher.calls)),
    ]  # fmt: skip
    state["L1"] = second
    leg.send("pair-url", {"url": urls[-1]})
    await leg.wait("owner-opened", 180)
    owner["W1.1"] = walk_cp19.fresh_checks(st, before)
    leg.send("opened-checked")
    await leg.wait("owner-captured")
    owner["W2.1"] = walk_cp19.capture_checks(st)
    async with McpChat(st) as chat:
        approval_id = await walk_cp19.agent_suggests(st, rec, chat)
        plan = next((a["plan_sha256"] for a in st.approvals() if a["id"] == approval_id), "")
        leg.send("agent-requested", {"approval_id": approval_id, "plan_sha256": plan})
        owner["W4.1"] = walk_cp19.announced_checks(st)
        await leg.wait("owner-granted")
        owner["W4.2"] = walk_cp19.granted_checks(st, approval_id)
        await walk_cp19.agent_resumes(st, rec, chat, approval_id)
    leg.send("agent-resumed")

    # W6.1: the core restarts.
    await leg.wait("owner-restart")
    before = st.digest()
    port = launcher.meta()["port"]
    stopped = launcher.run("stop", "--json")
    restarted = launcher.run("start", "--no-open", "--json")
    state["W6.1"] = {"stop": stopped, "start": restarted}
    leg.send("restarted")
    resumed = await leg.wait("owner-resumed", 180)
    owner["W6.1"] = [
        check("stopping the core ended its hub", stopped["exit"] == 0
              and stopped["body"].get("stopped") is True, stopped["body"]),
        check("the restarted hub is a new process on the home's same port",
              restarted["exit"] == 0 and restarted["body"].get("already_running") is False
              and launcher.meta()["port"] == port, restarted["body"]),
        check("restarting the core changed nothing in the store", st.digest() == before),
    ]  # fmt: skip
    await walk_cp19.agent_rereads(st, rec, approval_id)
    # The outcome compared with the MCP leg: store A after the last scripted step.
    state["outcome"] = walk_cp19.snapshot(st)
    state["digest_before_launcher_steps"] = st.digest()
    cookie = f"{resumed.get('cookie_name')}={resumed.get('cookie_value')}"
    await launcher_steps(st, leg, owner, launcher, state, work, cookie)


async def launcher_steps(
    st: Store, leg: Leg, owner: dict[str, list[dict[str, Any]]], launcher: Launcher,
    state: dict[str, Any], work: Path, cookie: str,
) -> None:  # fmt: skip
    meta = launcher.meta()
    port, host = int(meta["port"]), state["host"]
    origin = state["origin"]

    # L2: a forged process id in the advisory metadata takes nothing over.
    path = launcher.home / "launcher" / "hub.json"
    real = path.read_text(encoding="utf-8")
    path.write_text(json.dumps({**meta, "pid": os.getpid()}), encoding="utf-8")
    status = launcher.run("status", "--json")
    serve = launcher.run("hub", "serve", "--project", st.pid, timeout=90)
    again = launcher.run("start", "--no-open", "--json")
    path.write_text(real, encoding="utf-8")
    owner["L2"] = [
        check("status still reports the running hub (the lock decides)",
              status["body"].get("running") is True, status["body"].get("running")),
        check("a second hub process refuses while the lock is held",
              serve["exit"] != 0 and "already running" in serve["stdout"] + serve["stderr"],
              serve["exit"]),
        check("start opens the running hub instead of starting one",
              again["body"].get("already_running") is True, again["body"]),
    ]  # fmt: skip
    state["L2"] = {"status": status, "serve": serve, "start": again}

    # L3: replayed codes and the device credential where it does not belong.
    replay = post(port, "/api/v1/pair", f"{host}:{port}", origin, {"token": state["codes"][1]})
    # A fresh code over the owner channel, left unused past its 60 seconds (review cp21-a A4).
    from prometheos import launcher as launcher_module

    fresh = str(launcher_module._owner_call(launcher.home, port, "code")["code"])
    state["codes"].append(fresh)
    await asyncio.sleep(launcher_module.LAUNCH_TTL + 2)
    old = post(port, "/api/v1/pair", f"{host}:{port}", origin, {"token": fresh})
    hostile = post(port, "/api/v1/device/resume", f"{host}:{port}",
                   f"http://evil.localhost:{port}", {}, cookie)  # fmt: skip
    loopback = post(port, "/api/v1/device/resume", f"127.0.0.1:{port}",
                    f"http://127.0.0.1:{port}", {}, cookie)  # fmt: skip
    loopback_origin = post(port, "/api/v1/device/resume", f"{host}:{port}",
                           f"http://127.0.0.1:{port}", {}, cookie)  # fmt: skip
    other_store = Store(work / "store-other")
    other = Launcher(Path(launcher.exe), other_store.home, work / "store-other")
    other_started = other.run("start", "--no-open", "--project", other_store.pid, "--json")
    try:
        other_meta = other.meta()
        other_host = other_started["body"]["url"].split("//", 1)[1].split(":", 1)[0]
        other_port = int(other_meta["port"])
        elsewhere = post(other_port, "/api/v1/device/resume", f"{other_host}:{other_port}",
                         f"http://{other_host}:{other_port}", {}, cookie)  # fmt: skip
        foreign_host = post(other_port, "/api/v1/device/resume", f"{host}:{other_port}",
                            f"http://{host}:{other_port}", {}, cookie)  # fmt: skip
    finally:
        other.run("stop", "--json")
    owner["L3"] = [
        check("a used launch code is refused", replay[0] in (400, 401, 403), replay[0]),
        check("an unused code older than 60 seconds is refused", old[0] in (400, 401, 403),
              old[0]),
        check("the device credential with a hostile Origin is refused", hostile[0] == 403,
              hostile[0]),
        check("the device credential on 127.0.0.1 is refused", loopback[0] in (401, 403),
              loopback[0]),
        check("the home's host name with a loopback Origin is refused",
              loopback_origin[0] == 403, loopback_origin[0]),
        check("another home's hub refuses the credential", elsewhere[0] in (401, 403),
              elsewhere[0]),
        check("another home's hub refuses this home's host name", foreign_host[0] == 421,
              foreign_host[0]),
        check("the other home's hub started on its own port",
              other_started["exit"] == 0 and other_port != port, other_started["body"]),
    ]  # fmt: skip
    state["L3"] = {"replay": replay, "expired": old, "hostile_origin": hostile,
                   "loopback": loopback, "loopback_origin": loopback_origin,
                   "other_home": elsewhere,
                   "foreign_host": foreign_host}  # fmt: skip

    # L4: a killed hub leaves no lock; the next start keeps the port.
    killed = subprocess.run(["taskkill", "/F", "/PID", str(meta["pid"])], capture_output=True,
                            text=True, check=False)  # fmt: skip
    deadline = time.monotonic() + 20
    while launcher.running() and time.monotonic() < deadline:
        time.sleep(0.5)
    released = not launcher.running()
    after_kill = launcher.run("start", "--no-open", "--json")
    owner["L4"] = [
        check("the hub process was killed", killed.returncode == 0, killed.stdout.strip()),
        check("the OS released the lock: status reports no hub", released),
        check("the next start succeeds on the same port",
              after_kill["exit"] == 0 and after_kill["body"].get("already_running") is False
              and launcher.meta()["port"] == port, after_kill["body"]),
    ]  # fmt: skip
    state["L4"] = {"taskkill": killed.returncode, "start": after_kill}
    leg.send("killed-restarted")

    # L5: revoked on the Recovery page.
    await leg.wait("owner-revoked", 180)
    listed = launcher.run("device", "list", "--json")
    devices = listed["body"].get("devices", [])
    revoked_resume = post(port, "/api/v1/device/resume", f"{host}:{port}", origin, {}, cookie)
    owner["L5"] = [
        check("device list shows the one remembered browser revoked",
              [d.get("state") for d in devices] == ["revoked"], [d.get("state") for d in devices]),
        check("its credential no longer resumes a session", revoked_resume[0] == 401,
              revoked_resume[0]),
    ]  # fmt: skip
    state["L5"] = {"device_list": listed, "resume_after_revoke": revoked_resume}
    owner["L5"].append(
        check("the launcher, pairing and revocation steps changed no record",
              st.digest() == state["digest_before_launcher_steps"])
    )  # fmt: skip
    stopped = launcher.run("stop", "--json")
    log = (launcher.home / "launcher" / "hub.log").read_text(encoding="utf-8", errors="replace")
    owner["L5"] += [
        check("the hub stopped at the end", stopped["body"].get("stopped") is True),
        check("no launch code is in the hub's log",
              not any(c and c in log for c in state["codes"])),
    ]  # fmt: skip
    leg.send("done")


def browser_checks(step_id: str, shown: dict[str, Any]) -> list[dict[str, Any]]:
    """What the browser showed (recorded by the spec), as assertions."""
    if not shown:
        return [check("the browser spec recorded this step", False)]
    if step_id == "W1.1":
        cookie = shown.get("device_cookie") or {}
        return [
            check("the page paired from the fragment", shown.get("paired") is True),
            check("the code is gone from the history entry",
                  shown.get("hash_after") == "" and shown.get("href_has_code") is False,
                  shown.get("hash_after")),
            check("no request line carried the code", shown.get("request_lines_with_code") == 0),
            check("the page runs at the home's own host name",
                  str(shown.get("url_host", "")).split(":")[0].endswith(".localhost")),
            check("a host-only, HttpOnly, SameSite=Strict device cookie was set",
                  cookie.get("host_only") is True and cookie.get("http_only") is True
                  and cookie.get("same_site") == "Strict", cookie),
        ]  # fmt: skip
    if step_id == "W2.1":
        return [check("the hub confirmed the saved note", shown.get("saved") is True)]
    if step_id == "W4.1":
        return [check("the request waits on the Approvals page",
                      shown.get("shown_on_approvals_page") is True)]  # fmt: skip
    if step_id == "W4.2":
        return [
            check("granted in the hub", shown.get("granted_in_hub") is True),
            check("the plan digest was on the card before the grant",
                  shown.get("plan_digest_shown") is True),
        ]  # fmt: skip
    if step_id in ("W6.1", "L4"):
        return [
            check(
                "the reload resumed the session from the remembered browser",
                shown.get("resumed_with_device") is True,
            ),
            check("without pairing again", shown.get("paired_again") is False),
        ]
    if step_id == "L5":
        return [
            check("revoking signed this browser out", shown.get("signed_out") is True),
            check(
                "it stays signed out after a reload",
                shown.get("still_signed_out_after_reload") is True,
            ),
            check(
                "no request line ever carried a launch code",
                shown.get("request_lines_with_code") == 0,
            ),
        ]
    return []


def hub_leg(st: Store, out: Path, rec: walk_cp19.Recorder, work: Path) -> dict[str, Any]:
    if sys.platform != "win32":
        raise RuntimeError("the hub leg is proved on Windows")
    try:
        require_headless_shell(ROOT / "hub")
        playwright = playwright_command(ROOT / "hub")
    except BrowserUnavailable as exc:
        raise RuntimeError(f"the browser leg cannot run: {exc}") from exc
    installed = tool_install(work)
    launcher = Launcher(Path(installed["exe"]), st.home, work)
    state: dict[str, Any] = {"before": st.digest()}
    first = launcher.run("start", "--project", st.pid, "--json")
    if first["exit"] != 0:
        raise RuntimeError(f"prometheos start failed: {first['stderr'][-600:]}")
    url = first["body"]["url"]
    state["origin"] = url.rstrip("/")
    state["host"] = url.split("//", 1)[1].split(":", 1)[0]
    signals = out / "signals"
    signals.mkdir()
    steps_path = out / "hub-steps.jsonl"
    info = {"url": url, "host": state["host"], "signals": str(signals),
            "shots": str(out / "screenshots"), "steps": str(steps_path)}  # fmt: skip
    (out / "hub-info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    env = {
        **playwright_environment(),
        "WALK_INFO": str(out / "hub-info.json"),
        "WALK_PW_OUT": tempfile.mkdtemp(prefix="prometheos-walk21-pw-"),
        "BOUNDARY_EVIDENCE": str(out / "browser-boundary.json"),
        "PLAYWRIGHT_JSON_OUTPUT_FILE": str(out / "playwright-report.json"),
        "PROMETHEOS_BOUNDARY_HOME_HOST": state["host"],
    }
    owner: dict[str, list[dict[str, Any]]] = {}
    code = None
    with open(out / "playwright.log", "w", encoding="utf-8") as log:
        browser = subprocess.Popen(
            [*playwright, "-c", "playwright.walk21.config.ts", "--reporter=line,json"],
            cwd=ROOT / "hub", env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
        )  # fmt: skip
        try:
            asyncio.run(hub_story(st, rec, Leg(signals, browser), owner, launcher, state, work))
            code = browser.wait(timeout=300)
        except Exception as exc:
            rec.failures.append(f"hub: {walk_cp19.describe(exc)}")
        finally:
            if browser.poll() is None:
                browser.kill()
            if launcher.running():
                launcher.run("stop", "--json")
    if code != 0:
        rec.failures.append(f"hub: the Playwright walk failed (exit {code})")
        print((out / "playwright.log").read_text(encoding="utf-8")[-4000:])
    summary, problems = boundary_evidence(out)
    rec.failures += problems
    seen: dict[str, dict[str, Any]] = {}
    if steps_path.exists():
        for line in steps_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                step = json.loads(line)
                seen[step["id"]] = step
    codes = state.get("codes", [])
    for step in STEPS:
        step_id = step["id"]
        if step["actor"] != "owner" or (step_id not in seen and step_id not in owner):
            continue
        shown = seen.get(step_id, {})
        asserted = owner.get(step_id, [])
        if step_id in BROWSER_IDS:
            asserted = asserted + browser_checks(step_id, shown)
        response = without_secrets({"browser": shown, "launcher": state.get(step_id)}, codes)
        rec.add(step_id, "hub" if step_id in BROWSER_IDS else "launcher", {}, response,
                "ok" if all(a["passed"] for a in asserted) else "error", asserted)  # fmt: skip
    return {"tool": installed, "url": url, "boundary": summary, "outcome": state.get("outcome"),
            "launcher_calls": [without_secrets({k: c[k] for k in ("argv", "exit", "body")},
                                               codes) for c in launcher.calls]}  # fmt: skip


# ----- orchestration ---------------------------------------------------------------------------


def judge(doc: dict[str, Any], outcomes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    equivalence: dict[str, Any] = {}
    failures: list[str] = []
    difference = first_difference(outcomes["hub"], outcomes["mcp"])
    equivalence["stored outcomes"] = "equal" if difference is None else difference
    if difference is not None:
        failures.append(f"stored outcomes differ: {difference}")
    one = {s["id"]: s for s in doc["hub"]["steps"]}
    two = {s["id"]: s for s in doc["mcp"]["steps"]}
    missing = [i for i in SCRIPT_IDS if i not in one or i not in two]
    equivalence["scripted steps present on both legs"] = "all" if not missing else missing
    if missing:
        failures.append(f"steps missing: {missing}")
    both = [i for i in SCRIPT_IDS if i in one and i in two]
    differing = {i: (one[i]["outcome"], two[i]["outcome"]) for i in both
                 if one[i]["outcome"] != two[i]["outcome"]}  # fmt: skip
    equivalence["step outcomes (ok / error:code)"] = "equal" if not differing else differing
    if differing:
        failures.append(f"step outcomes differ: {differing}")
    compared = [i for i in both if i in AGENT_IDS]
    unequal = [
        i
        for i in compared
        if one[i]["normalised_response_sha256"] != two[i]["normalised_response_sha256"]
    ]
    equivalence["normalised answers of every agent step"] = (
        f"equal for {len(compared)} steps" if not unequal else f"differ at {unequal}"
    )  # fmt: skip
    if unequal:
        failures.append(f"agent answers differ at {unequal}")
    hub_only = [s for s in doc["hub"]["steps"] if s["id"].startswith("L")]
    equivalence["launcher and pairing steps (hub leg only)"] = (
        f"{len(hub_only)} of {len(LAUNCHER_STEPS)} recorded"
    )
    if len(hub_only) != len(LAUNCHER_STEPS):
        failures.append("launcher steps missing on the hub leg")
    return equivalence, failures


def transcript_markdown(doc: dict[str, Any]) -> str:
    text = walk_cp19.transcript_markdown({**doc, "desktop": doc.get("hub"), "built_app": None})
    text = text.replace("# C19-WALK transcript", "# C21-WALK transcript", 1)
    hub = doc.get("hub_leg") or {}
    tool = hub.get("tool") or {}
    head = f"Installed core: `{tool.get('exe')}` ({tool.get('uv')}); hub at {hub.get('url')}\n\n"
    return text.replace("\n\nCandidate:", "\n\n" + head + "Candidate:", 1).replace(
        "## desktop leg", "## hub leg", 1
    )


def run(out: Path | None, only: str | None, keep: bool, show: bool) -> int:
    out = (out or Path(tempfile.mkdtemp(prefix="prometheos-walk21-out-"))).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into a folder that is not empty: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prometheos-walk21-"))
    doc: dict[str, Any] = {
        "walk": "C21-WALK",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "script": STEPS,
        "limitations": [
            "The MCP leg's owner steps run through the service in the harness, a synthetic owner "
            "channel, as in C19-WALK; the hub leg's owner steps are the real hub in a browser.",
            "The browser is Playwright's headless shell with a fresh profile; the launcher's "
            "browser is a stand-in that records the URL `start` opens, which the spec then loads.",
            "The agent is a scripted MCP client; real hosts are the external gates E01 and E02.",
            "The agent's MCP server runs from the checkout's environment (.venv); the hub runs "
            "from the uv tool install of the same checkout (editable).",
            "The launcher and pairing steps (L1-L5) exist only on the hub leg; they are asserted "
            "to change no record, and the compared outcome is taken before them.",
        ],
        "candidate": walk_cp19.candidate(),
    }
    failures: list[str] = []
    outcomes: dict[str, Any] = {}
    equivalence: dict[str, Any] = {}
    try:
        if only in (None, "hub"):
            store_a = Store(work / "store-a")
            rec_a = walk_cp19.Recorder("A", "hub", store_a, out / "evidence" / "hub")
            hub: dict[str, Any] = {}
            try:
                hub = hub_leg(store_a, out, rec_a, work)
            except RuntimeError as exc:
                rec_a.failures.append(f"hub: {exc}")
            doc["hub_leg"] = {k: v for k, v in hub.items() if k != "outcome"}
            outcomes["hub"] = hub.get("outcome") or walk_cp19.snapshot(store_a)
            (out / "outcome-hub.json").write_text(
                json.dumps(outcomes["hub"], indent=2, sort_keys=True), encoding="utf-8"
            )
            doc["hub"] = {
                "store": "A",
                "transport": "web-first hub (prometheos start; owner in the browser) + agent "
                "over MCP stdio",
                "steps": rec_a.ordered(),
            }
            failures += rec_a.failures
        if only in (None, "mcp"):
            if only is None:
                url = (doc.get("hub_leg") or {}).get("url")
                refused = walk_cp19.ports_refused([url] if url else [])
                equivalence["no hub ran during the MCP leg"] = refused
                if not refused.startswith("confirmed"):
                    failures.append("a hub still ran during the MCP leg")
            store_b = Store(work / "store-b")
            rec_b = walk_cp19.Recorder("B", "mcp", store_b, out / "evidence" / "mcp")
            asyncio.run(walk_cp19.mcp_leg(store_b, rec_b))
            outcomes["mcp"] = walk_cp19.snapshot(store_b)
            (out / "outcome-mcp.json").write_text(
                json.dumps(outcomes["mcp"], indent=2, sort_keys=True), encoding="utf-8"
            )
            doc["mcp"] = {
                "store": "B",
                "transport": "MCP stdio (official SDK client), no hub running",
                "steps": rec_b.ordered(),
            }
            failures += rec_b.failures
        if "hub" in outcomes and "mcp" in outcomes:
            judged, problems = judge(doc, outcomes)
            equivalence = judged | equivalence
            failures += problems
        doc["equivalence"] = equivalence
        doc["failures"] = failures
        if failures:
            doc["summary"] = f"FAIL: {len(failures)} problem(s); see failures."
        elif only is None:
            doc["summary"] = ("PASS: every step's assertions held and the two stores reached "
                              "equivalent outcomes.")  # fmt: skip
        else:
            doc["summary"] = f"PASS: {only} leg only; assertions held. Equivalence was not checked."
        doc["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (out / "transcript.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        markdown = transcript_markdown(doc)
        (out / "transcript.md").write_text(markdown, encoding="utf-8")
        if show:
            print(markdown)
        print(doc["summary"])
        for failure in failures:
            print(" -", failure)
        print("transcript:", out / "transcript.json")
        return 0 if not failures else 1
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="a new folder for the evidence (default: temp)")
    parser.add_argument("--only", choices=("hub", "mcp"), help="run one leg (debugging)")
    parser.add_argument("--keep", action="store_true", help="keep the disposable stores")
    parser.add_argument(
        "--print-transcript",
        action="store_true",
        help="print the transcript (the checkpoint runner retains stdout)",
    )
    args = parser.parse_args(argv)
    return run(args.out, args.only, args.keep, args.print_transcript)


if __name__ == "__main__":
    sys.exit(main())
