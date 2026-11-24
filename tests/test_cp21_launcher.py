"""CP21.1 and the launch-code part of CP21.2 (A07; decision 0088): the web-first launcher.

Real detached hubs on disposable homes. A stand-in browser (``BROWSER``) records the URL it is
asked to open, so the tests read the launch code exactly where a browser would: in the fragment.
- one hub process per home through an OS-held lock; a second `start` opens a paired browser on
  the running hub; a second `hub serve` refuses; a forged pid in the advisory metadata takes
  nothing over; a killed hub's lock is released by the OS;
- CLI writes keep working while the hub runs (D03);
- launch codes come only over the owner channel, last 60 seconds, work once and are never
  printed; a request a browser could make never gets one;
- the home's port is fixed: a taken port is refused, never moved; the pre-CP21 desktop app on the
  same home is refused.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cp1_support import Env, create_entry, request

from prometheos import launcher
from prometheos.domain.models import PrometheosError
from prometheos.hub.security import Gate

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the launcher is proved on Windows")


class Home:
    def __init__(self, tmp: Path) -> None:
        self.env = Env(tmp)
        self.home = self.env.home
        self.opened = tmp / "opened.txt"
        browser = tmp / "browser.py"
        browser.write_text(
            "import sys\nwith open(sys.argv[1], 'a', encoding='utf-8') as f:\n"
            "    f.write(sys.argv[2] + '\\n')\n",
            encoding="utf-8",
        )
        exe, script, out = (Path(p).as_posix() for p in (sys.executable, browser, self.opened))
        self.vars = {
            **os.environ,
            "PROMETHEOS_HOME": str(self.home),
            "PYTHONIOENCODING": "utf-8",
            "BROWSER": f'"{exe}" "{script}" "{out}" %s',
        }

    def cli(self, *args: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "prometheos", *args],
            env=self.vars, capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip

    def urls(self) -> list[str]:
        if not self.opened.exists():
            return []
        return self.opened.read_text(encoding="utf-8").split()

    def meta(self) -> dict[str, Any]:
        return json.loads((self.home / "launcher" / "hub.json").read_text(encoding="utf-8"))

    def port(self) -> int:
        identity = json.loads((self.home / "launcher" / "home.json").read_text(encoding="utf-8"))
        return int(identity["port"])

    def secret(self) -> str:
        return (self.home / "launcher" / "owner.secret").read_text(encoding="ascii").strip()

    def stop(self) -> None:
        if not (self.home / "launcher").exists():
            return
        done = self.cli("stop", "--json")
        if done.returncode != 0 and (self.home / "launcher" / "hub.json").exists():
            subprocess.run(["taskkill", "/F", "/PID", str(self.meta()["pid"])], check=False)


def post(port: int, path: str, body: Any, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", path, body=json.dumps(body), headers=headers)
        reply = connection.getresponse()
        return reply.status, json.loads(reply.read() or b"{}")
    finally:
        connection.close()


def pair(port: int, code: str) -> int:
    origin = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
    return post(port, "/api/v1/pair", {"token": code}, origin)[0]


def code_of(url: str) -> str:
    assert "#pair=" in url and "?" not in url, url
    return url.split("#pair=", 1)[1]


@pytest.fixture
def started(tmp_path: Path) -> Iterator[Home]:
    home = Home(tmp_path)
    done = home.cli("start", "--project", home.env.project_id, "--json")
    assert done.returncode == 0, done.stderr
    try:
        yield home
    finally:
        home.stop()


def test_start_opens_a_paired_browser_and_never_prints_the_code(started: Home) -> None:
    [url] = started.urls()
    port = started.port()
    # A07, CP21.2 (decision 0089): the hub opens at the home's own host name.
    assert re.fullmatch(rf"http://h[0-9a-f]{{16}}\.localhost:{port}/#pair=[\w-]+", url), url
    code = code_of(url)
    status = json.loads(started.cli("status", "--json").stdout)
    assert status["running"] is True and status["port"] == port
    assert status["project_id"] == started.env.project_id
    assert pair(port, code) == 200
    assert pair(port, code) != 200  # one use
    log = (started.home / "launcher" / "hub.log").read_text(encoding="utf-8", errors="replace")
    assert code not in log


def test_a_second_start_opens_the_running_hub_and_starts_no_second_hub(started: Home) -> None:
    pid = started.meta()["pid"]
    again = started.cli("start", "--json")
    assert again.returncode == 0, again.stderr
    result = json.loads(again.stdout)
    assert result["already_running"] is True and result["pid"] == pid
    first, second = started.urls()
    assert code_of(first) != code_of(second)
    assert code_of(second) not in again.stdout + again.stderr
    assert pair(started.port(), code_of(second)) == 200
    assert started.meta()["pid"] == pid


def test_a_second_hub_process_refuses_while_the_lock_is_held(started: Home) -> None:
    done = started.cli("hub", "serve", "--project", started.env.project_id)
    assert done.returncode == 6  # busy
    assert "already running" in done.stdout + done.stderr


def test_a_forged_pid_in_the_metadata_takes_nothing_over(started: Home) -> None:
    meta_path = started.home / "launcher" / "hub.json"
    real = started.meta()
    forged = {**real, "pid": 4}  # the System process: alive, and never this hub
    meta_path.write_text(json.dumps(forged), encoding="utf-8")
    done = started.cli("start", "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["already_running"] is True
    assert pair(started.port(), code_of(started.urls()[-1])) == 200
    meta_path.write_text(json.dumps(real), encoding="utf-8")


def test_cli_writes_keep_working_while_the_hub_runs(started: Home) -> None:
    pid = started.env.project_id
    req = started.home.parent / "req.json"
    seq = json.loads(started.cli("now", "--project", pid, "--json").stdout)["project_seq"]
    req.write_bytes(request(pid, [create_entry(title="Written by the CLI")], seq))
    done = started.cli("apply", "--file", str(req), "--project", pid, "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(started.cli("now", "--project", pid, "--json").stdout)["project_seq"] > seq


def test_the_owner_channel_refuses_whatever_a_browser_could_send(started: Home) -> None:
    port, secret = started.port(), started.secret()
    good = {"Content-Type": "application/json", launcher.OWNER_HEADER: secret}
    path = "/api/v1/launcher/code"
    assert post(port, path, {}, {"Content-Type": "application/json"})[0] == 403
    assert post(port, path, {}, {**good, launcher.OWNER_HEADER: "wrong"})[0] == 403
    assert post(port, path, {}, {**good, "Origin": f"http://127.0.0.1:{port}"})[0] == 403
    assert post(port, path, {}, {**good, "Sec-Fetch-Site": "same-origin"})[0] == 403
    assert post(port, path, {}, {**good, "Sec-Fetch-Mode": "cors"})[0] == 403
    assert post(port, path, {}, {**good, "Cookie": "x=1"})[0] == 403
    assert post(port, "/api/v1/launcher/stop", {}, {"Content-Type": "application/json"})[0] == 403
    status, body = post(port, path, {}, good)
    assert status == 200 and body["ttl_seconds"] == 60
    assert pair(port, body["code"]) == 200


def test_stop_ends_the_hub_and_frees_the_lock(started: Home) -> None:
    pid = started.meta()["pid"]
    done = started.cli("stop", "--json")
    assert done.returncode == 0 and json.loads(done.stdout)["stopped"] is True
    status = json.loads(started.cli("status", "--json").stdout)
    assert status["running"] is False and status["pid"] is None
    assert not (started.home / "launcher" / "owner.secret").exists()
    assert pid not in launcher.legacy_desktop_pids()


def test_stop_does_not_wait_for_an_open_long_poll(started: Home) -> None:
    """Found in the A1a demo: a paired browser's changes long-poll held a launched hub's
    graceful stop until the poll ended. A launched hub caps the wait at 5 seconds."""
    import threading

    port = started.port()
    origin = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(
        "POST", "/api/v1/pair", body=json.dumps({"token": code_of(started.urls()[-1])}),
        headers=origin,
    )  # fmt: skip
    reply = connection.getresponse()
    cookie = reply.getheader("set-cookie", "").split(";", 1)[0]
    reply.read()
    connection.close()
    assert reply.status == 200 and cookie

    probe = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    probe.request("GET", "/api/v1/changes?after_seq=0&wait=0", headers={"Cookie": cookie})
    latest = json.loads(probe.getresponse().read())["latest_seq"]
    probe.close()

    def poll() -> None:  # waits for a change that never comes, as an idle browser tab does
        held = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        with contextlib.suppress(OSError, http.client.HTTPException):
            held.request("GET", f"/api/v1/changes?after_seq={latest}", headers={"Cookie": cookie})
            held.getresponse().read()
        held.close()

    threading.Thread(target=poll, daemon=True).start()
    time.sleep(1.0)
    began = time.monotonic()
    done = started.cli("stop", "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    assert time.monotonic() - began < 12


def test_a_killed_hub_leaves_no_lock_and_the_next_start_keeps_the_port(tmp_path: Path) -> None:
    home = Home(tmp_path)
    assert home.cli("start", "--project", home.env.project_id, "--no-open").returncode == 0
    port, pid = home.port(), home.meta()["pid"]
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True, capture_output=True)
    deadline = time.monotonic() + 20
    while launcher.hub_running(home.home) and time.monotonic() < deadline:
        time.sleep(0.1)
    try:
        done = home.cli("start", "--project", home.env.project_id, "--json")
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["already_running"] is False
        assert home.port() == port and home.meta()["pid"] != pid
    finally:
        home.stop()


def test_a_taken_port_is_refused_and_never_moved(tmp_path: Path) -> None:
    home = Home(tmp_path)
    port = int(launcher.home_identity(home.home)["port"])  # chosen once and recorded
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", port))
        squatter.listen()
        done = home.cli("start", "--project", home.env.project_id, "--json")
    assert done.returncode == 6 and "never moves its port" in done.stdout + done.stderr
    assert home.port() == port and home.urls() == []


def test_the_pre_cp21_desktop_app_on_this_home_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    monkeypatch.setattr(launcher, "desktop_home", lambda: env.home)
    monkeypatch.setattr(launcher, "legacy_desktop_pids", lambda: [4242])
    with pytest.raises(PrometheosError) as refused:
        launcher.start(project=env.project_id, open_browser=False)
    assert refused.value.code == "busy" and refused.value.details == {"desktop_pids": [4242]}
    monkeypatch.setattr(launcher, "desktop_home", lambda: tmp_path / "another-home")
    assert launcher.legacy_desktop_on(env.home) == []


def test_a_launch_code_lasts_sixty_seconds() -> None:
    now = [0.0]
    gate = Gate(port=1, clock=lambda: now[0])
    code = gate.new_pairing_token(ttl=launcher.LAUNCH_TTL)
    now[0] = 60.5
    assert gate.pair(code) is None
    code = gate.new_pairing_token(ttl=launcher.LAUNCH_TTL)
    now[0] = 110.0
    assert gate.pair(code) is not None


# ----- review cp21-a (decision 0091 records the repairs) ----------------------------------------


def _sleeper(folder: Path, name: str) -> subprocess.Popen[bytes]:
    """A harmless process with the given image name: a copy of ping waiting on loopback."""
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / name
    shutil.copyfile(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "PING.EXE",
                    image)  # fmt: skip
    return subprocess.Popen([str(image), "-n", "60", "127.0.0.1"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)  # fmt: skip


def test_the_uv_command_is_never_mistaken_for_the_desktop_app(tmp_path: Path) -> None:
    """F1: `uv tool install` writes `prometheos.exe` too; only the desktop build, with its app
    archive beside it, is the pre-CP21 app. The real detection runs (no stand-in)."""
    command = _sleeper(tmp_path / "tool" / "bin", "prometheos.exe")
    desktop_folder = tmp_path / "Programs" / "PrometheOS"
    (desktop_folder / "resources").mkdir(parents=True)
    (desktop_folder / "resources" / "app.asar").write_bytes(b"asar")
    desktop = _sleeper(desktop_folder, "PrometheOS.exe")
    try:
        time.sleep(0.5)
        found = launcher.legacy_desktop_pids()
        assert desktop.pid in found
        assert command.pid not in found
        assert os.getpid() not in found
    finally:
        for process in (command, desktop):
            process.kill()
            process.wait(timeout=10)


def test_two_starts_at_once_give_one_hub_and_both_open_it(tmp_path: Path) -> None:
    """A1: the loser waits for the winner instead of failing; one identity, one hub."""
    home = Home(tmp_path)
    argv = [sys.executable, "-m", "prometheos", "start", "--project", home.env.project_id,
            "--json"]  # fmt: skip
    racers = [subprocess.Popen(argv, env=home.vars, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True) for _ in range(2)]  # fmt: skip
    try:
        replies = [racer.communicate(timeout=120) for racer in racers]
        assert [r.returncode for r in racers] == [0, 0], [e for _, e in replies]
        answers = [json.loads(out) for out, _ in replies]
        assert sorted(a["already_running"] for a in answers) == [False, True]
        assert answers[0]["pid"] == answers[1]["pid"] == home.meta()["pid"]
        assert answers[0]["url"] == answers[1]["url"]
        assert len(home.urls()) == 2  # both opened a paired browser on the one hub
    finally:
        home.stop()


def test_status_on_a_fresh_home_creates_nothing(tmp_path: Path) -> None:
    """A7: status only reads."""
    home = Home(tmp_path)
    done = home.cli("status", "--json")
    assert done.returncode == 0, done.stderr
    shown = json.loads(done.stdout)
    assert shown["running"] is False and shown["port"] is None and shown["url"] is None
    assert not (home.home / "launcher" / "home.json").exists()
    assert not (home.home / "launcher" / "hub.lock").exists()


def test_a_terminal_hub_on_another_port_is_not_at_the_home_host(tmp_path: Path) -> None:
    """F2 (advisory part): the home's host name only on the home's own fixed port."""
    home = Home(tmp_path)
    label = launcher.hub_url(home.home, 1).split("//", 1)[1].split(".", 1)[0]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        other = probe.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "prometheos", "hub", "serve", "--port", str(other),
         "--project", home.env.project_id],
        env=home.vars, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline:
            try:
                connection = http.client.HTTPConnection("127.0.0.1", other, timeout=5)
                connection.request("GET", "/api/v1/session",
                                   headers={"Host": f"{label}.localhost:{other}"})  # fmt: skip
                status = connection.getresponse().status
                connection.close()
                break
            except OSError:
                time.sleep(0.3)
        assert status == 421
    finally:
        server.terminate()
        server.wait(timeout=30)


def test_a_damaged_identity_is_refused_at_once_never_retried(tmp_path: Path) -> None:
    """R2-F2: an empty or broken home.json ends in a clear error, not a hang."""
    home = tmp_path / "home"
    (home / "launcher").mkdir(parents=True)
    for broken in ("", "{not json", '{"home_id": 3}'):
        (home / "launcher" / "home.json").write_text(broken, encoding="utf-8")
        began = time.monotonic()
        with pytest.raises(PrometheosError) as refused:
            launcher.home_identity(home)
        assert refused.value.code == "io_error" and "damaged" in refused.value.message
        assert time.monotonic() - began < 2


def test_the_owner_secret_goes_only_to_the_port_the_hub_recorded(tmp_path: Path) -> None:
    """R2-F1: a listener of any account on the home's fixed port never receives the secret;
    a terminal hub on another port is stopped through its own port and opened on 127.0.0.1."""
    home = Home(tmp_path)
    fixed = int(launcher.home_identity(home.home)["port"])
    received: list[bytes] = []
    decoy = socket.socket()
    decoy.bind(("127.0.0.1", fixed))
    decoy.listen()
    decoy.settimeout(0.2)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        other = probe.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "prometheos", "hub", "serve", "--port", str(other),
         "--project", home.env.project_id],
        env=home.vars, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        while not (home.home / "launcher" / "hub.json").exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        opened = home.cli("start", "--json")
        assert opened.returncode == 0, opened.stderr
        assert json.loads(opened.stdout)["url"] == f"http://127.0.0.1:{other}/"
        stopped = home.cli("stop", "--json")
        assert stopped.returncode == 0, stopped.stderr
        assert json.loads(stopped.stdout)["stopped"] is True
        while True:
            try:
                connection, _ = decoy.accept()
            except TimeoutError:
                break
            connection.settimeout(1)
            with contextlib.suppress(OSError):
                received.append(connection.recv(65536))
            connection.close()
        assert not any(OWNER_SECRET_HEADER in chunk for chunk in received)
        assert received == []
    finally:
        decoy.close()
        if server.poll() is None:
            server.terminate()
        server.wait(timeout=30)


OWNER_SECRET_HEADER = launcher.OWNER_HEADER.lower().encode()
