"""Provider credentials live outside the project, in the operating system's credential store or
the process environment (spec/08). Callers get a *reference* and a health report; the value
is read only inside the adapter at the moment of a call and never returned, logged or stored.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Protocol

from prometheos.domain.models import PrometheosError

REFERENCE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
TARGET_PREFIX = "PrometheOS:provider:"
MAX_SECRET_CHARS = 4096


def check_reference(reference: str) -> str:
    if not REFERENCE.fullmatch(reference):
        raise PrometheosError(
            "validation_error", "a credential reference is a short lower-case name", {}
        )
    return reference


def check_value(value: str) -> str:
    if not value or len(value) > MAX_SECRET_CHARS or "\n" in value or "\r" in value:
        raise PrometheosError(
            "validation_error", "a credential is one non-empty line of at most 4096 characters", {}
        )
    return value


class SecretStore(Protocol):
    name: str

    def get(self, reference: str) -> str | None: ...

    def set(self, reference: str, value: str) -> None: ...

    def delete(self, reference: str) -> bool: ...


def env_name(reference: str) -> str:
    return "PROMETHEOS_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", reference).upper()


class EnvSecretStore:
    """Read-only: the process environment (``PROMETHEOS_SECRET_<REFERENCE>``)."""

    name = "environment"

    def get(self, reference: str) -> str | None:
        value = os.environ.get(env_name(check_reference(reference)))
        return value if value else None

    def set(self, reference: str, value: str) -> None:
        raise PrometheosError(
            "unsupported",
            f"this store is read-only; set the environment variable {env_name(reference)}",
            {},
        )

    def delete(self, reference: str) -> bool:
        raise PrometheosError("unsupported", "unset the environment variable instead", {})


class MemorySecretStore:
    """In-process store for tests."""

    name = "memory"

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, reference: str) -> str | None:
        return self._values.get(check_reference(reference))

    def set(self, reference: str, value: str) -> None:
        self._values[check_reference(reference)] = check_value(value)

    def delete(self, reference: str) -> bool:
        return self._values.pop(check_reference(reference), None) is not None


class WindowsCredentialStore:
    """Windows Credential Manager (generic credentials, local machine persistence)."""

    name = "windows-credential-manager"

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise PrometheosError("unsupported", "the Windows credential store needs Windows", {})

    @staticmethod
    def _api() -> Any:
        import ctypes
        from ctypes import wintypes

        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        advapi.CredWriteW.restype = wintypes.BOOL
        advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(Credential)),
        ]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi.CredDeleteW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        return ctypes, advapi, Credential

    def get(self, reference: str) -> str | None:
        ctypes, advapi, credential = self._api()
        target = TARGET_PREFIX + check_reference(reference)
        pointer = ctypes.POINTER(credential)()
        if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            return None
        try:
            size = pointer.contents.CredentialBlobSize
            raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
        finally:
            advapi.CredFree(pointer)
        return raw.decode("utf-16-le") or None

    def set(self, reference: str, value: str) -> None:
        ctypes, advapi, credential = self._api()
        target = TARGET_PREFIX + check_reference(reference)
        blob = check_value(value).encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        entry = credential()
        entry.Type = 1  # CRED_TYPE_GENERIC
        entry.TargetName = target
        entry.CredentialBlobSize = len(blob)
        entry.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        entry.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        entry.UserName = "prometheos"
        if not advapi.CredWriteW(ctypes.byref(entry), 0):
            raise PrometheosError("io_error", "the Windows credential store refused the write", {})

    def delete(self, reference: str) -> bool:
        _ctypes, advapi, _credential = self._api()
        return bool(advapi.CredDeleteW(TARGET_PREFIX + check_reference(reference), 1, 0))


class LayeredSecretStore:
    """Reads the OS store first, then the environment; writes go to the OS store."""

    def __init__(self, primary: SecretStore | None, fallback: SecretStore) -> None:
        self.primary, self.fallback = primary, fallback
        self.name = primary.name if primary is not None else fallback.name

    def get(self, reference: str) -> str | None:
        if self.primary is not None:
            found = self.primary.get(reference)
            if found:
                return found
        return self.fallback.get(reference)

    def source(self, reference: str) -> str | None:
        if self.primary is not None and self.primary.get(reference):
            return self.primary.name
        return self.fallback.name if self.fallback.get(reference) else None

    def set(self, reference: str, value: str) -> None:
        (self.primary or self.fallback).set(reference, value)

    def delete(self, reference: str) -> bool:
        return (self.primary or self.fallback).delete(reference)


def default_store() -> LayeredSecretStore:
    primary: SecretStore | None = WindowsCredentialStore() if sys.platform == "win32" else None
    return LayeredSecretStore(primary, EnvSecretStore())


def health(store: SecretStore, reference: str | None) -> dict[str, Any]:
    """What an agent or the hub may see: the reference and whether a value exists. Never the
    value, its length or any fragment."""
    if reference is None:
        return {"reference": None, "present": False, "store": store.name}
    source = store.source(reference) if isinstance(store, LayeredSecretStore) else None
    present = store.get(reference) is not None
    return {
        "reference": reference,
        "present": present,
        "store": source or store.name,
    }
