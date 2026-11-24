"""The web-first launcher (A07, CP21; decision 0088).

One hub process runs per home. Every hub process (``prometheos start``, ``hub serve``, the
desktop sidecar of a build that honours it) holds an exclusive lock on
``<home>/launcher/hub.lock`` for its lifetime. The operating system releases it when the process
dies, so there is no stale-lock recovery and no lock is ever broken on the evidence of
``hub.json``, whose process id, port and start time are advisory metadata for ``status`` only.
MCP servers, CLI acts and job workers are not hubs: they keep D03's transactional writes.

The home's port is chosen once and kept in ``home.json``; ``start`` refuses rather than moves
when it is taken. Launch codes (one use, 60 seconds) are issued only over the owner channel: a
per-process secret in a user-only file, sent in a header on a loopback request that carries no
Origin, fetch metadata or cookie, which a browser cannot make. The code reaches the browser only
in the URL fragment and is never printed.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prometheos.agent_package import package_health
from prometheos.domain.models import PrometheosError, utc_now
from prometheos.store.home import app_home
from prometheos.store.lock import _try_lock, _unlock
from prometheos.store.private_files import write_private

OWNER_HEADER = "X-PrometheOS-Owner-Channel"
LAUNCH_TTL = 60.0
# Decision 0114: below the Linux ephemeral range (32768-60999) and Windows' dynamic range
# (49152-65535). With WSL in mirrored networking mode the two share one port space, and a busy
# Linux side (a Docker stack) held 3,110 of the 4,000 ports of the earlier 41000-44999 range.
PORT_BASE = 27000
PORT_SPAN = 4000
READY_TIMEOUT = 30.0
STOP_TIMEOUT = 15.0
LEGACY_DESKTOP_IMAGE = "prometheos.exe"
LAUNCH_ID_VAR = "PROMETHEOS_LAUNCH_ID"


def launcher_dir(home: Path | None = None) -> Path:
    return (home or app_home()) / "launcher"


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def home_identity(home: Path | None = None) -> dict[str, Any]:
    """The home's stable id and fixed port, chosen once (the first free port from a start
    derived from the id) and never moved afterwards. Written whole to a temporary file and then
    linked into place, which fails if another process got there first, so two first starts on a
    fresh home agree on one identity and no reader ever sees half a file (review cp21-a A1,
    R2-F2). A file that exists but does not parse is refused with what to do, never retried."""
    path = launcher_dir(home) / "home.json"
    known = _read_json(path)
    if known is None and path.exists():
        time.sleep(0.2)  # a writer on a file system without hard links may still be renaming
        known = _read_json(path)
    if known is not None:
        if isinstance(known.get("home_id"), str) and isinstance(known.get("port"), int):
            return known
    if path.exists():
        raise PrometheosError(
            "io_error",
            f"{path} is damaged; delete it and start again (the home gets a new host name and "
            "port, and remembered browsers pair once more)",
            {"path": str(path)},
        )
    home_id = str(uuid.uuid4())
    first = PORT_BASE + int(home_id.replace("-", "")[:8], 16) % PORT_SPAN
    port = next(
        (p for p in range(first, first + 50) if p < PORT_BASE + PORT_SPAN and port_free(p)), first
    )
    identity = {"home_id": home_id, "port": port, "chosen_at": utc_now()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"home.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as written:
        written.write(json.dumps(identity, indent=2, sort_keys=True) + "\n")
        written.flush()
        os.fsync(written.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        chosen = _read_json(path)
        if chosen is None:
            raise PrometheosError("io_error", f"{path} could not be read", {}) from None
        return chosen
    finally:
        temporary.unlink(missing_ok=True)
    return identity


def known_identity(home: Path | None = None) -> dict[str, Any] | None:
    """The identity if one was chosen; never creates it (``status`` on a fresh home)."""
    known = _read_json(launcher_dir(home) / "home.json")
    if known and isinstance(known.get("home_id"), str) and isinstance(known.get("port"), int):
        return known
    return None


# ----- the hub lock -------------------------------------------------------------------------


class HubLock:
    """The home's hub lock, held by one open handle for the life of the hub process."""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd: int | None = fd
        self.path = path

    @classmethod
    def acquire(cls, home: Path | None = None, wait: float = 0.0) -> HubLock | None:
        """Take the lock, or None while another hub holds it. A hub waits ``wait`` seconds, so a
        ``status`` probe holding it for an instant never makes a starting hub fail (A1)."""
        deadline = time.monotonic() + wait
        while True:
            taken = cls._acquire_once(home)
            if taken is not None or time.monotonic() >= deadline:
                return taken
            time.sleep(0.05)

    @classmethod
    def _acquire_once(cls, home: Path | None) -> HubLock | None:
        path = launcher_dir(home) / "hub.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(fd).st_size == 0:
            with contextlib.suppress(OSError):  # a holder may have written and locked it first
                os.write(fd, b"\0")
        if not _try_lock(fd):
            os.close(fd)
            return None
        return cls(fd, path)

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            _unlock(self._fd)
        os.close(self._fd)
        self._fd = None


def hub_running(home: Path | None = None) -> bool:
    if not (launcher_dir(home) / "hub.lock").exists():
        return False  # no hub ever ran here; the probe creates nothing (A7)
    probe = HubLock.acquire(home)
    if probe is None:
        return True
    probe.release()
    return False


# ----- advisory metadata and the owner channel ----------------------------------------------


def write_metadata(home: Path, *, port: int, project_id: str, mode: str) -> None:
    _write_json(
        launcher_dir(home) / "hub.json",
        {
            "pid": os.getpid(),
            "port": port,
            "project_id": project_id,
            "mode": mode,
            "started_at": utc_now(),
            "launch_id": os.environ.get(LAUNCH_ID_VAR),
            "advisory": "for status only; the lock is hub.lock, held by the hub process",
        },
    )


def read_metadata(home: Path | None = None) -> dict[str, Any] | None:
    return _read_json(launcher_dir(home) / "hub.json")


def write_owner_secret(home: Path) -> str:
    """A new secret for this hub process in a file restricted to the current user. Called
    while the hub lock is held, so a file left by a killed hub is replaced."""
    path = launcher_dir(home) / "owner.secret"
    path.unlink(missing_ok=True)
    secret = secrets.token_urlsafe(32)
    write_private(path, secret.encode("ascii"))
    return secret


def clear_hub_files(home: Path) -> None:
    for name in ("owner.secret", "hub.json"):
        with contextlib.suppress(OSError):
            (launcher_dir(home) / name).unlink(missing_ok=True)


def owner_channel_ok(secret: str | None, headers: Any) -> bool:
    """A request on the owner channel: the secret header, and none of what a browser sends
    (Origin, fetch metadata, cookies)."""
    if not secret:
        return False
    if headers.get("origin") is not None or headers.get("cookie") is not None:
        return False
    if any(headers.get(name) is not None for name in ("sec-fetch-site", "sec-fetch-mode")):
        return False
    presented = headers.get(OWNER_HEADER.lower()) or ""
    return hmac.compare_digest(presented.encode(), secret.encode())


def _owner_call(home: Path, port: int, action: str) -> dict[str, Any]:
    try:
        secret = (launcher_dir(home) / "owner.secret").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise PrometheosError(
            "io_error", "the running hub's owner channel is not readable", {}
        ) from exc
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/launcher/{action}",
        data=b"{}",
        method="POST",
        headers={OWNER_HEADER: secret, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:  # noqa: S310 - loopback only
            document = json.loads(reply.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PrometheosError(
            "io_error", f"the running hub did not answer on 127.0.0.1:{port}", {}
        ) from exc
    return document if isinstance(document, dict) else {}


# ----- the pre-CP21 desktop build -------------------------------------------------------------


def desktop_home() -> Path:
    """The home a desktop build from before CP21 uses: the user's PROMETHEOS_HOME setting, or
    the default home. Its sidecar does not take the hub lock."""
    if sys.platform == "win32":
        import winreg

        with contextlib.suppress(OSError):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, "PROMETHEOS_HOME")
                if value:
                    return Path(str(value))
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "PrometheOS-v1"
    return Path.home() / ".local/share/prometheos-v1"


def _image_path(pid: int) -> Path | None:
    """The full path of a process's executable (Windows), or None when it cannot be read."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value)
    finally:
        kernel.CloseHandle(handle)


def is_desktop_build(executable: Path) -> bool:
    """The Electron desktop build: ``PrometheOS.exe`` with its ``resources/app.asar`` beside it.
    The console command that ``uv tool install`` writes is also ``prometheos.exe`` but has no
    app archive, so it is never mistaken for the desktop app (review cp21-a F1)."""
    return (
        executable.name.lower() == LEGACY_DESKTOP_IMAGE
        and (executable.parent / "resources" / "app.asar").is_file()
    )


def _process_ids() -> list[int]:
    """Every process id on this computer (Windows ``EnumProcesses``; no subprocess, so a
    stripped environment cannot hide the list)."""
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    size = 4096
    while True:
        ids = (wintypes.DWORD * size)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(needed)):
            return []
        count = needed.value // ctypes.sizeof(wintypes.DWORD)
        if count < size:
            return [int(ids[i]) for i in range(count) if ids[i]]
        size *= 2


def legacy_desktop_pids() -> list[int]:
    """The processes of a running desktop build: recognised by the executable's own folder
    holding the app archive, never by the image name alone (review cp21-a F1)."""
    if sys.platform != "win32":
        return []
    found = []
    for pid in _process_ids():
        image = _image_path(pid)
        if image is not None and is_desktop_build(image):
            found.append(pid)
    return found


def legacy_desktop_on(home: Path) -> list[int]:
    same = os.path.normcase(os.path.abspath(home)) == os.path.normcase(
        os.path.abspath(desktop_home())
    )
    return legacy_desktop_pids() if same else []


# ----- start, stop, status --------------------------------------------------------------------


@dataclass
class Opened:
    url: str
    already_running: bool
    pid: int | None
    browser_opened: bool


def hub_url(home: Path, port: int) -> str:
    """The hub at the home's own host name (A07, CP21.2), where it can remember the browser.
    A hub on another port (a terminal ``hub serve``) serves 127.0.0.1 only (R2-F1)."""
    from prometheos.devices import host_label

    identity = home_identity(home)
    if port != int(identity["port"]):
        return f"http://127.0.0.1:{port}/"
    return f"http://{host_label(identity['home_id'])}.localhost:{port}/"


def _open_paired(home: Path, port: int, open_browser: bool) -> bool:
    if not open_browser:
        return False
    code = _owner_call(home, port, "code").get("code")
    if not isinstance(code, str) or not code:
        raise PrometheosError("io_error", "the running hub issued no launch code", {})
    return bool(webbrowser.open(f"{hub_url(home, port)}#pair={code}"))


def _spawn(home: Path, port: int, project: str | None, launch_id: str) -> subprocess.Popen[bytes]:
    argv = [sys.executable, "-m", "prometheos", "hub", "serve", "--port", str(port), "--launched"]
    if project:
        argv += ["--project", project]
    log = (launcher_dir(home) / "hub.log").open("ab")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    # The venv's python.exe may be a launcher that starts the interpreter as a child, so the
    # hub is recognised by this launch id in its metadata, never by the child's pid.
    environment = dict(os.environ, PROMETHEOS_HOME=str(home), **{LAUNCH_ID_VAR: launch_id})
    try:
        for extra in (0x01000000, 0) if sys.platform == "win32" else (0,):  # BREAKAWAY_FROM_JOB
            try:
                return subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=environment,
                    creationflags=flags | extra,
                    close_fds=True,
                    start_new_session=sys.platform != "win32",
                )
            except OSError:
                if extra == 0:
                    raise
        raise AssertionError("unreachable")
    finally:
        log.close()


def _log_tail(home: Path, lines: int = 15) -> str:
    try:
        text = (launcher_dir(home) / "hub.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _running_hub(home: Path, timeout: float = READY_TIMEOUT) -> dict[str, Any]:
    """The running hub's port once it serves: the port the lock holder recorded in its own
    metadata, answering, with its owner-channel secret written. The secret is only ever sent
    there: a process of another account that listens on the home's fixed port never receives
    it (review cp21-a R2-F1); only the home's own user can write the metadata. A start racing
    another start, or arriving while a hub is still starting, waits for it (A1)."""
    deadline = time.monotonic() + timeout
    checked = 0.0
    while True:
        meta = read_metadata(home) or {}
        port = meta.get("port")
        if (
            isinstance(port, int)
            and (launcher_dir(home) / "owner.secret").exists()
            and _listening(port)
        ):
            pid = meta.get("pid") if isinstance(meta.get("pid"), int) else None
            return {"port": port, "pid": pid}
        now = time.monotonic()
        if now - checked > 1.0:
            checked = now
            if not hub_running(home):
                raise PrometheosError("io_error", "the running hub stopped while starting", {})
        if now > deadline:
            raise PrometheosError("io_error", "the running hub did not become ready in time", {})
        time.sleep(0.1)


def start(project: str | None = None, open_browser: bool = True) -> Opened:
    home = app_home()
    legacy = legacy_desktop_on(home)
    if legacy:
        raise PrometheosError(
            "busy",
            "the PrometheOS desktop app is running on this home; quit it (tray icon, Quit) "
            "before starting the web hub",
            {"desktop_pids": legacy},
        )
    port = int(home_identity(home)["port"])
    if hub_running(home):
        meta = _running_hub(home)
        port = int(meta["port"])
        opened = _open_paired(home, port, open_browser)
        return Opened(hub_url(home, port), True, meta["pid"], opened)
    if not port_free(port):
        raise PrometheosError(
            "busy",
            f"port {port} of this home is used by another program; PrometheOS never moves its "
            "port, so stop that program first (with WSL in mirrored networking mode, a busy "
            "Linux side can hold ports that no Windows program shows)",
            {"port": port},
        )
    launch_id = secrets.token_hex(16)
    child = _spawn(home, port, project, launch_id)
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        ready = read_metadata(home)
        if (
            ready
            and ready.get("launch_id") == launch_id
            and (launcher_dir(home) / "owner.secret").exists()
        ):
            break
        if child.poll() is not None:
            if hub_running(home):  # another start won the lock first: open that hub instead
                meta = _running_hub(home)
                port = int(meta["port"])
                opened = _open_paired(home, port, open_browser)
                return Opened(hub_url(home, port), True, meta["pid"], opened)
            raise PrometheosError(
                "io_error",
                "the hub stopped while starting:\n" + _log_tail(home),
                {"exit_code": child.returncode},
            )
        time.sleep(0.1)
    else:
        raise PrometheosError("io_error", "the hub did not become ready in time", {})
    opened = _open_paired(home, port, open_browser)
    pid = ready.get("pid") if ready and isinstance(ready.get("pid"), int) else None
    return Opened(hub_url(home, port), False, pid, opened)


def stop() -> dict[str, Any]:
    home = app_home()
    if not hub_running(home):
        # Nothing is cleared here: a hub starting right now may just have written its files
        # (review cp21-a A1); a hub removes its own files when it ends.
        return {"stopped": False, "running": False}
    port = int(_running_hub(home)["port"])
    _owner_call(home, port, "stop")
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not hub_running(home):
            return {"stopped": True, "running": False, "port": port}
        time.sleep(0.1)
    raise PrometheosError("io_error", "the hub did not stop in time", {"port": port})


def status() -> dict[str, Any]:
    home = app_home()
    identity = known_identity(home)  # status creates nothing on a home with no hub yet (A7)
    running = hub_running(home)
    meta = read_metadata(home) if running else None
    port = (meta or {}).get("port") or (identity or {}).get("port")
    return {
        "running": running,
        "home": str(home),
        "port": port,
        "url": hub_url(home, int(port)) if identity and port else None,
        "pid": (meta or {}).get("pid"),
        "project_id": (meta or {}).get("project_id"),
        "mode": (meta or {}).get("mode"),
        "started_at": (meta or {}).get("started_at"),
        "metadata": "advisory (hub.json); the hub lock decides whether a hub runs",
        "desktop_app_running_on_this_home": bool(legacy_desktop_on(home)),
        "agent_packages": package_health(home),
        "agent_packages_note": "only packages written by `prometheos host setup` from this "
        "version on are recorded and checked; write an older package again to have it checked",
    }
