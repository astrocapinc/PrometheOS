"""Pinned Tectonic engine: explicit install, status and a bounded runner (A01; decision 0012).

The engine lives under the PrometheOS home (``tools/tectonic-<version>/``) or in the
folder named by ``PROMETHEOS_TEX_ENGINE_DIR``. Installing is an explicit action: a plan
first shows source, size, licence, destination and the network disclosure, and the
install proceeds only for that plan's digest. The downloaded archive must match the
pinned size and SHA-256; the extracted executable's digest is written to
``install.json`` and re-verified before every compile. Nothing changes PATH or any
system setting, and no system TeX installation is used.

The runner gives Tectonic a clean environment, a wall-clock timeout, a cap on captured
output and, on Windows, a job object limiting memory and forbidding child processes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError, utc_now

ENGINE_DIR_ENV = "PROMETHEOS_TEX_ENGINE_DIR"
EXE_NAME = "tectonic.exe" if sys.platform == "win32" else "tectonic"
INSTALL_MANIFEST = "install.json"
DOWNLOAD_TIMEOUT = 120.0
DEAD_PROXY = "http://127.0.0.1:9"  # discard port: an offline run that tried to fetch fails


@dataclass(frozen=True)
class Release:
    engine: str
    version: str
    tag: str
    asset: str
    url: str
    size: int
    sha256: str
    license: str
    license_url: str
    bundle_url: str
    bundle_note: str


TECTONIC = Release(
    engine="tectonic",
    version="0.17.0",
    tag="tectonic@0.17.0",
    asset="tectonic-0.17.0-x86_64-pc-windows-msvc.zip",
    url=(
        "https://github.com/tectonic-typesetting/tectonic/releases/download/"
        "tectonic%400.17.0/tectonic-0.17.0-x86_64-pc-windows-msvc.zip"
    ),
    size=21_060_223,
    sha256="f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f",
    license="MIT",
    license_url=("https://github.com/tectonic-typesetting/tectonic/blob/tectonic%400.17.0/LICENSE"),
    bundle_url="https://relay.fullyjustified.net/default_bundle_v33.tar",
    bundle_note=(
        "The TeX support files come from Tectonic's default bundle (TeX Live 2022 material, "
        "each package under its own free licence), fetched on demand from "
        "relay.fullyjustified.net, which redirects to data1b.fullyjustified.net. A fetch "
        "reveals which packages, fonts and support files a document uses and your IP "
        "address, never the manuscript text. Fetched files are cached under the engine "
        "folder; a compile without --fetch uses only that cache and makes no request."
    ),
)


def engine_dir(home: Path) -> Path:
    configured = os.environ.get(ENGINE_DIR_ENV)
    if configured:
        return Path(configured)
    return home / "tools" / f"{TECTONIC.engine}-{TECTONIC.version}"


def display_location(home: Path) -> str:
    """The engine folder as shown outside the CLI: never an absolute local path (decision
    0006 item 19; integration review CP5 INT1-M3)."""
    directory = engine_dir(home)
    try:
        return f"<PrometheOS home>/{directory.relative_to(home).as_posix()}"
    except ValueError:
        return f"<{ENGINE_DIR_ENV}>"


def redacted(home: Path, document: dict[str, Any]) -> dict[str, Any]:
    """Copy of a status or install-plan document with its folder replaced by the label of
    :func:`display_location`. A plan keeps its digest, which binds the real destination."""
    shown = dict(document)
    for key in ("location", "destination"):
        if isinstance(shown.get(key), str):
            shown[key] = display_location(home)
    return shown


def cache_dir(directory: Path) -> Path:
    return directory / "cache"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _dir_size(path: Path, limit: int = 200_000) -> tuple[int, int]:
    total = files = 0
    if not path.is_dir():
        return 0, 0
    for entry in path.rglob("*"):
        if files >= limit:
            break
        if entry.is_file():
            files += 1
            total += entry.stat().st_size
    return total, files


def _read_manifest(directory: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((directory / INSTALL_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def status(home: Path, release: Release = TECTONIC) -> dict[str, Any]:
    directory = engine_dir(home)
    manifest = _read_manifest(directory)
    exe = directory / EXE_NAME
    state = "absent"
    problem = None
    if manifest is not None or exe.exists():
        state = "installed"
        if manifest is None:
            state, problem = "invalid", "install.json is missing or unreadable"
        elif manifest.get("zip_sha256") != release.sha256:
            state, problem = "invalid", "installed archive digest is not the pinned release"
        elif not exe.is_file():
            state, problem = "invalid", "the engine executable is missing"
        elif sha256_file(exe) != manifest.get("exe_sha256"):
            state, problem = "invalid", "the engine executable changed since install"
    cache_bytes, cache_files = _dir_size(cache_dir(directory))
    return {
        "engine": release.engine,
        "pinned_version": release.version,
        "state": state,
        "problem": problem,
        "location": str(directory),
        "location_from": "environment" if os.environ.get(ENGINE_DIR_ENV) else "home",
        "installed": manifest if state == "installed" else None,
        "license": release.license,
        "bundle_url": release.bundle_url,
        "network": release.bundle_note,
        "cache": {"bytes": cache_bytes, "files": cache_files},
    }


def install_plan(home: Path, release: Release = TECTONIC) -> dict[str, Any]:
    directory = engine_dir(home)
    plan = {
        "engine": release.engine,
        "version": release.version,
        "release_tag": release.tag,
        "download_url": release.url,
        "download_bytes": release.size,
        "sha256": release.sha256,
        "license": release.license,
        "license_url": release.license_url,
        "destination": str(directory),
        "already_installed": status(home, release)["state"] == "installed",
        "changes_path_or_system": False,
        "bundle_url": release.bundle_url,
        "network": release.bundle_note,
    }
    plan["plan_digest"] = canonical_sha256(plan)
    return plan


def _download(url: str, target: Path, size: int, opener: Callable[..., Any]) -> None:
    received = 0
    with opener(url, timeout=DOWNLOAD_TIMEOUT) as response, open(target, "xb") as out:
        while chunk := response.read(1024 * 256):
            received += len(chunk)
            if received > size:
                raise PrometheosError(
                    "validation_error", "download is larger than the pinned release", {}
                )
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())


def install(
    home: Path,
    plan_digest: str,
    *,
    archive: Path | None = None,
    release: Release = TECTONIC,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Install exactly the planned release. ``archive`` installs from a zip the owner already
    downloaded (still verified against the pinned size and digest)."""
    plan = install_plan(home, release)
    if plan_digest != plan["plan_digest"]:
        raise PrometheosError(
            "revision_conflict", "the install plan changed; review the new plan", {"plan": plan}
        )
    directory = engine_dir(home)
    directory.parent.mkdir(parents=True, exist_ok=True)
    work = directory.parent / f".install-{uuid.uuid4().hex}"
    work.mkdir()
    try:
        zip_path = work / release.asset
        if archive is not None:
            shutil.copyfile(archive, zip_path)
        else:
            _download(release.url, zip_path, release.size, opener)
        size = zip_path.stat().st_size
        digest = sha256_file(zip_path)
        if size != release.size or digest != release.sha256:
            raise PrometheosError(
                "validation_error",
                "the archive does not match the pinned release (size or SHA-256); nothing "
                "was installed",
                {"bytes": size, "sha256": digest},
            )
        with zipfile.ZipFile(zip_path) as bundle:
            members = [m for m in bundle.infolist() if Path(m.filename).name == EXE_NAME]
            if len(members) != 1 or members[0].file_size > 512 * 1024 * 1024:
                raise PrometheosError(
                    "validation_error", "the archive does not hold exactly one engine binary", {}
                )
            extracted = work / EXE_NAME
            with bundle.open(members[0]) as src, open(extracted, "xb") as dst:
                shutil.copyfileobj(src, dst)
        manifest = {
            "engine": release.engine,
            "version": release.version,
            "release_tag": release.tag,
            "asset": release.asset,
            "source_url": release.url if archive is None else f"local archive {archive.name}",
            "zip_sha256": digest,
            "exe_sha256": sha256_file(extracted),
            "license": release.license,
            "license_url": release.license_url,
            "bundle_url": release.bundle_url,
            "installed_at": utc_now(),
        }
        directory.mkdir(parents=True, exist_ok=True)
        os.replace(extracted, directory / EXE_NAME)
        temp_manifest = directory / f"{INSTALL_MANIFEST}.{uuid.uuid4().hex}.part"
        temp_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_manifest, directory / INSTALL_MANIFEST)
        cache_dir(directory).mkdir(exist_ok=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return status(home, release)


@dataclass(frozen=True)
class Engine:
    executable: Path
    cache: Path
    version: str
    exe_sha256: str


def resolve(home: Path) -> Engine:
    """The installed, digest-verified engine, or a clear refusal."""
    report = status(home)
    if report["state"] != "installed":
        raise PrometheosError(
            "unsupported",
            "the Tectonic engine is not installed or failed verification; run "
            "`prometheos tex engine install` (or set PROMETHEOS_TEX_ENGINE_DIR)",
            {"state": report["state"], "problem": report["problem"]},
        )
    directory = engine_dir(home)
    installed = report["installed"]
    return Engine(
        executable=directory / EXE_NAME,
        cache=cache_dir(directory),
        version=str(installed["version"]),
        exe_sha256=str(installed["exe_sha256"]),
    )


# ----- bounded runner --------------------------------------------------------------------------


@dataclass
class RunLimits:
    timeout_seconds: float = 120.0
    max_captured_bytes: int = 4 * 1024 * 1024
    max_output_dir_bytes: int = 256 * 1024 * 1024
    max_memory_bytes: int = 2 * 1024 * 1024 * 1024


@dataclass
class RunResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_exceeded: bool
    memory_exceeded: bool
    seconds: float


def mark_bundle_checked(cache: Path) -> int:
    """Before an offline compile: record the cached bundles as just checked.

    Tectonic 0.17.0 keeps the time of its last remote check of a bundle as Unix seconds in
    ``bundles/hashes/<bundle>.lock`` and, once that is a week old, connects to the bundle
    host again even under ``--only-cached`` (it then compiles from the cache when the check
    fails). An offline compile must make no request (spec/08, A01.3), so the time is
    renewed; a fetch compile is not touched and checks as Tectonic decides. Only files
    holding a bare integer are rewritten. Returns how many were renewed."""
    renewed = 0
    hashes = cache / "bundles" / "hashes"
    try:
        locks = list(hashes.glob("*.lock"))
    except OSError:
        return 0
    now = str(int(time.time()))
    for lock in locks:
        try:
            if not lock.read_text(encoding="ascii").strip().isdigit():
                continue
            temporary = lock.with_name(f"{lock.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(now, encoding="ascii")
            os.replace(temporary, lock)
            renewed += 1
        except (OSError, UnicodeError):
            continue
    return renewed


def clean_environment(cache: Path, temp: Path, *, network: bool) -> dict[str, str]:
    """Only what the engine needs; no user variables, tokens or TeX settings leak in."""
    env = {
        "TECTONIC_CACHE_DIR": str(cache),
        "TECTONIC_UNTRUSTED_MODE": "1",
        "TEMP": str(temp),
        "TMP": str(temp),
        "TMPDIR": str(temp),
        "NO_COLOR": "1",
    }
    if sys.platform == "win32":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        env["SystemRoot"] = system_root
        env["PATH"] = str(Path(system_root) / "System32")
    else:
        env["PATH"] = "/usr/bin:/bin"
    if network:
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy"):
            if os.environ.get(name):
                env[name] = os.environ[name]
    else:
        # Defence in depth: --only-cached with a renewed bundle check (mark_bundle_checked)
        # makes no request; any request that still happened would go to a closed local port.
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy"):
            env[name] = DEAD_PROXY
        env["NO_PROXY"] = ""
    return env


class _Capture(threading.Thread):
    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.data = bytearray()
        self.exceeded = False

    def run(self) -> None:
        while chunk := self.stream.read(65536):
            if len(self.data) < self.limit:
                self.data += chunk[: self.limit - len(self.data)]
            if len(self.data) >= self.limit:
                self.exceeded = True


def _tree_bytes(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


class _WindowsJob:
    """A job object: memory cap, at most one process (no children), killed on close."""

    def __init__(self, memory: int) -> None:
        self.handle: Any = None
        self.peak = 0
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_ulonglong)
                for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")
            ]

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._ctypes = ctypes
        self._extended = Extended
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = Extended()
        # PROCESS_MEMORY | ACTIVE_PROCESS | KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = 0x100 | 0x8 | 0x2000
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = memory
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, process: subprocess.Popen[bytes]) -> bool:
        if self.handle is None:
            return False
        return bool(self._k.AssignProcessToJobObject(self.handle, int(process._handle)))  # type: ignore[attr-defined]

    def close(self) -> None:
        if self.handle is None:
            return
        info = self._extended()
        if self._k.QueryInformationJobObject(
            self.handle, 9, self._ctypes.byref(info), self._ctypes.sizeof(info), None
        ):
            self.peak = int(info.PeakProcessMemoryUsed)
        self._k.CloseHandle(self.handle)
        self.handle = None


def run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    limits: RunLimits,
    watch: Path | None = None,
) -> RunResult:
    """Run a command under the limits; kill it on timeout or runaway output."""
    job = _WindowsJob(limits.max_memory_bytes)
    started = time.monotonic()
    creation = 0
    if sys.platform == "win32":
        creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation,
    )
    job.assign(process)
    out = _Capture(process.stdout, limits.max_captured_bytes)
    err = _Capture(process.stderr, limits.max_captured_bytes)
    out.start()
    err.start()
    timed_out = exceeded = False
    try:
        while process.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed > limits.timeout_seconds:
                timed_out = True
                break
            if out.exceeded or err.exceeded:
                exceeded = True
                break
            if watch is not None and _tree_bytes(watch) > limits.max_output_dir_bytes:
                exceeded = True
                break
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)
        out.join(timeout=10)
        err.join(timeout=10)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        job.close()
    memory_exceeded = job.peak >= limits.max_memory_bytes * 0.95 and process.returncode not in (
        0,
        None,
    )
    return RunResult(
        returncode=process.returncode,
        stdout=bytes(out.data),
        stderr=bytes(err.data),
        timed_out=timed_out,
        output_exceeded=exceeded or out.exceeded or err.exceeded,
        memory_exceeded=memory_exceeded,
        seconds=round(time.monotonic() - started, 3),
    )
