"""Project admission and raw snapshots (decision 0025).

Windows snapshots deny write/delete sharing; no SQLite lock byte is touched.
The OS gate serializes supported app connections. Same-thread nested connections
borrow a live lease, including backup's separate blocker and reader connections.
Registry and scratch-copy SQLite connections do not use this module.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sqlite3
import sys
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from prometheos.domain.models import PrometheosError

DB_NAME = "record.sqlite"
SIDES = ("-journal", "-wal", "-shm")
TIMEOUT_MS = 5000
_LOCAL = threading.local()


def _busy(message: str) -> PrometheosError:
    return PrometheosError("busy", message, {"lock": "project_admission"})


def _io(exc: OSError) -> PrometheosError:
    return PrometheosError("io_error", f"source files cannot be read ({exc.strerror})", {})


def _win() -> Any:
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


class RawFile:
    """A read-only file handle; ``strong`` denies write and delete sharing on Windows."""

    def __init__(self, path: Path, *, strong: bool) -> None:
        self.path = path
        if sys.platform == "win32":
            import msvcrt
            from ctypes import wintypes

            kernel = _win()
            handle = kernel.CreateFileW(
                str(path), 0x80000000, 1 if strong else 3, None, 3, 0x80, None
            )
            if handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                if error in (32, 33):
                    raise _busy("source files have writable or unavailable handles; retry")
                raise ctypes.WinError(error)
            try:
                # BY_HANDLE_FILE_INFORMATION: DWORD + three FILETIMEs + seven DWORDs.
                info = (wintypes.DWORD * 13)()
                if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.key = (int(info[7]), (int(info[11]) << 32) | int(info[12]))
                if self.key[1] == 0:
                    raise PrometheosError(
                        "unsupported", "the source has no proven physical file identity", {}
                    )
                name = ctypes.create_unicode_buffer(32768)
                length = kernel.GetFinalPathNameByHandleW(handle, name, len(name), 0)
                if not length or length >= len(name):
                    raise OSError("source final location is unavailable")
                self.final = name.value
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
            except BaseException:
                kernel.CloseHandle(handle)
                raise
            self.reader: BinaryIO = os.fdopen(fd, "rb")
        else:
            self.reader = path.open("rb")
            stat = os.fstat(self.reader.fileno())
            self.key = (stat.st_dev, stat.st_ino)
            self.final = str(path.resolve())

    def close(self) -> None:
        self.reader.close()

    def read(self, size: int = -1) -> bytes:
        self.reader.seek(0)
        return self.reader.read(size)

    def digest(self) -> str:
        self.reader.seek(0)
        digest = hashlib.sha256()
        while chunk := self.reader.read(1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()

    def signature(self) -> tuple[int, int, int, int, int]:
        stat = os.fstat(self.reader.fileno())
        return (*self.key, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def validate(self) -> None:
        if sys.platform != "win32":
            stat = self.path.stat()
            if (stat.st_dev, stat.st_ino) != self.key:
                raise _busy("source physical identity changed; retry")
            return
        other = RawFile(self.path, strong=False)
        try:
            if other.key != self.key or other.final.casefold() != self.final.casefold():
                raise _busy("source physical identity or location changed; retry")
        finally:
            other.close()


def physical_key(path: Path) -> tuple[Any, ...]:
    if sys.platform != "win32":
        try:
            stat = path.stat()
            return (stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            return ("missing", str(path.absolute()))
    try:
        file = RawFile(path, strong=False)
    except FileNotFoundError:
        return ("missing", str(path.absolute()).casefold())
    try:
        return file.key
    finally:
        file.close()


class Gate:
    def __init__(self, key: tuple[Any, ...], wait_ms: int = TIMEOUT_MS) -> None:
        name = hashlib.sha256(repr(key).encode()).hexdigest()
        self.handle: Any = None
        self.fd: int | None = None
        if sys.platform == "win32":
            kernel = _win()
            self.handle = kernel.CreateMutexW(None, False, "Global\\PrometheOS.Admission." + name)
            if not self.handle:
                raise _busy("the cross-process project gate is unavailable")
            result = kernel.WaitForSingleObject(self.handle, wait_ms)
            if result not in (0, 0x80):  # abandoned also requires the ordinary fresh preflight
                kernel.CloseHandle(self.handle)
                self.handle = None
                raise _busy("another project operation holds the admission gate; retry")
        else:
            raise PrometheosError(
                "unsupported", "source admission requires the verified Windows sharing guard", {}
            )

    def close(self) -> None:
        if self.handle is not None:
            kernel = _win()
            kernel.ReleaseMutex(self.handle)
            kernel.CloseHandle(self.handle)
            self.handle = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Lease:
    def __init__(self, path: Path, key: tuple[Any, ...], wait_ms: int = TIMEOUT_MS) -> None:
        self.path, self.key = path, key
        self.owner = (os.getpid(), threading.get_native_id())
        self.gate = Gate(key, wait_ms)
        self.depth = 0
        self.live: set[int] = set()
        self.pin: RawFile | None = None
        self.snapshot: Snapshot | None = None
        self.poisoned = False

    def check_owner(self) -> None:
        if self.owner != (os.getpid(), threading.get_native_id()) or self.poisoned:
            raise _busy("the project lease is unavailable to this caller")

    def prepare(self, path: Path) -> None:
        self.check_owner()
        if self.live:
            assert self.pin is not None
            self.pin.validate()
            requested = RawFile(path, strong=False)
            try:
                if (
                    requested.key != self.pin.key
                    or requested.final.casefold() != self.pin.final.casefold()
                ):
                    raise _busy("a nested connection changes the pinned SQLite journal location")
            finally:
                requested.close()

    def attach(self, conn: Connection) -> None:
        self.check_owner()
        self.live.add(id(conn))
        conn.lease = self

    def detached(self, conn: Connection) -> None:
        self.check_thread()
        self.live.remove(id(conn))
        if not self.live and self.pin is not None:
            self.pin.close()
            self.pin = None
        self.release_if_idle()

    def check_thread(self) -> None:
        if self.owner != (os.getpid(), threading.get_native_id()):
            raise _busy("a project lease cannot be transferred between threads")

    def release_if_idle(self) -> None:
        if self.depth == 0 and not self.live:
            if self.pin is not None:
                self.pin.close()
                self.pin = None
            self.gate.close()
            _owners().pop(self.key)


def _owners() -> dict[tuple[Any, ...], Lease]:
    if not hasattr(_LOCAL, "owners"):
        _LOCAL.owners = {}
    return _LOCAL.owners  # type: ignore[no-any-return]


@contextmanager
def operation(path: Path, *, wait_ms: int = TIMEOUT_MS) -> Iterator[Lease]:
    """The thread's lease on the project at ``path``, taking its gate if the thread does not
    hold it yet. ``wait_ms`` bounds the wait for a gate held elsewhere (0: one attempt, as an
    opportunistic housekeeping read uses; decision 0031) before ``busy``."""
    try:
        key = physical_key(path)
    except OSError as exc:
        raise _io(exc) from exc
    owners = _owners()
    lease = owners.get(key)
    if lease is None:
        if owners and repr(key) < max(map(repr, owners)):
            raise _busy("project gates must be acquired in physical identity order")
        lease = owners[key] = Lease(path, key, wait_ms)
    lease.check_owner()
    lease.depth += 1
    try:
        yield lease
    finally:
        lease.depth -= 1
        lease.release_if_idle()


class Snapshot:
    def __init__(self, directory: Path, lease: Lease) -> None:
        self.directory = directory
        self.files: dict[str, RawFile] = {}
        self.lease = lease
        try:
            for name in (DB_NAME, *(DB_NAME + side for side in SIDES)):
                try:
                    self.files[name] = RawFile(directory / name, strong=True)
                except FileNotFoundError:
                    pass
            database = self.files.get(DB_NAME)
            if database is not None and database.key != lease.key:
                raise _busy("source database identity changed during admission; retry")
            if database is not None:
                for file in self.files.values():
                    if Path(file.final).parent != Path(database.final).parent:
                        raise _busy("a SQLite side file leaves the admitted source location")
            if sys.platform != "win32" and any(name != DB_NAME for name in self.files):
                raise PrometheosError(
                    "unsupported", "live journal inspection requires the Windows source guard", {}
                )
            self.validate()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for file in self.files.values():
            file.close()

    def validate(self) -> None:
        for name in (DB_NAME, *(DB_NAME + side for side in SIDES)):
            file = self.files.get(name)
            if file is not None:
                file.validate()
            else:
                try:
                    (self.directory / name).stat()
                except FileNotFoundError:
                    continue
                raise _busy("source side-file set changed during admission; retry")

    def mode_supported(self) -> bool:
        if any(DB_NAME + side in self.files for side in ("-wal", "-shm")):
            return False
        database = self.files.get(DB_NAME)
        if database is not None:
            # Only a complete 100-byte SQLite header declares a file format; a shorter
            # magic-prefixed main file is corrupt, not an unsupported mode (A04, R2-L1).
            header = database.read(100)
            complete = len(header) == 100 and header[:16] == b"SQLite format 3\x00"
            if complete and header[18:20] != b"\x01\x01":
                return False
        return True

    def rollback_state(self) -> str:
        file = self.files.get(DB_NAME + "-journal")
        if file is None:
            return "none"
        header = file.read(8)
        if header == bytes.fromhex("d9d505f920a163d7"):
            return "hot"  # even a truncated HOT package never authorizes an original open
        if header in (b"", b"\x00" * 8):
            return "none"  # completed DELETE/TRUNCATE/PERSIST invalidation, held stable
        raise PrometheosError(
            "io_error", "the rollback journal header is uncertain; source admission refused", {}
        )

    def pin(self) -> RawFile:
        file = RawFile(self.directory / DB_NAME, strong=False)
        original = self.files[DB_NAME]
        if file.key != original.key or file.final.casefold() != original.final.casefold():
            file.close()
            raise _busy("database identity changed before connection admission; retry")
        return file

    def journal_state(self) -> str:
        state = self.rollback_state()
        if state == "hot":
            return state
        if not self.mode_supported():
            raise PrometheosError(
                "unsupported", "the database is outside supported DELETE journal mode", {}
            )
        return state


@contextmanager
def source_snapshot(directory: Path) -> Iterator[Snapshot]:
    with operation(directory / DB_NAME) as lease:
        if lease.live:
            raise _busy("a live project connection cannot supply a raw recovery snapshot")
        if lease.snapshot is not None:
            yield lease.snapshot
            return
        try:
            snapshot = Snapshot(directory, lease)
        except OSError as exc:
            raise _io(exc) from exc
        lease.snapshot = snapshot
        try:
            yield snapshot
            snapshot.validate()
        except OSError as exc:
            raise _io(exc) from exc
        finally:
            lease.snapshot = None
            snapshot.close()


class Connection(sqlite3.Connection):
    lease: Lease | None = None

    def close(self) -> None:
        lease = self.lease
        if lease is not None:
            lease.check_thread()
        try:
            super().close()
        except BaseException:
            if lease is not None:
                lease.poisoned = True
            raise
        if lease is not None:
            self.lease = None
            lease.detached(self)


def poison(conn: sqlite3.Connection) -> None:
    if isinstance(conn, Connection) and conn.lease is not None:
        conn.lease.poisoned = True


def require_before_maintenance() -> None:
    if _owners():
        raise _busy("maintenance must be acquired before a project admission lease")


@contextmanager
def guarded_sources(*directories: Path) -> Iterator[None]:
    ordered = sorted(directories, key=lambda p: repr(physical_key(p / DB_NAME)))
    with ExitStack() as stack:
        for directory in ordered:
            stack.enter_context(operation(directory / DB_NAME))
        for directory in ordered:
            stack.enter_context(source_snapshot(directory))
        yield
