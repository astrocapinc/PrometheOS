"""Research folders: adding an approved research root to an existing project (A02, CP11
S11.4b; decision 0066 §2).

A research root widens what PrometheOS may read, so adding one is an owner act: an agent's
request is an approval request the owner grants (``approval_required``). The change is a
``project_update`` revision of ``allowed_roots`` (audited and versioned, like every settings
change). Removing a root is out of scope: imported sources would lose their binding.

The folder must be (decision 0067 §1 and its review round 1 repairs):

- an existing local directory, given as an absolute path, resolved (links followed once, as
  ``project init`` and a restore's rebinding do), not a UNC/network location;
- not the PrometheOS home, inside it or containing it, and not containing the project store;
- not a system folder (a drive or file-system root, the operating system's and programs'
  folders) nor the user's home folder itself;
- not a folder of keys (``.ssh``, ``.gnupg``), not inside ``AppData`` (programs' data and
  saved credentials; the temporary folder inside it is allowed), not another user's profile
  and not the shared ``Public`` profile;
- not inside or containing another research root, and under an alias not already used.

An agent's request is checked only lexically, with one refusal for every location rule
(:func:`check_requested_root`): it never learns whether a folder exists or which rule it broke.
The full rules run when the owner reviews, grants and the grant executes, and a folder that is
a link to another folder is refused then (the owner approves the folder they were shown).

The owner sees a requested folder as :func:`describe_folder` puts it: a base named by a
placeholder (your user folder, Documents, Desktop, OneDrive, the temporary folder, a drive) and
the folders below it spelled out, with warnings for hidden folders and sensitive names. No
absolute path is in it, so the hub's no-local-paths rule holds.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from prometheos.domain.models import PrometheosError
from prometheos.store.home import DRIVE_REMOTE, _drive_type

ALIAS_PATTERN = r"^[a-z][a-z0-9_-]{0,39}$"
POSIX_SYSTEM = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/dev", "/proc", "/sys", "/var",
    "/System", "/Library",
)  # fmt: skip
KEY_FOLDERS = frozenset({".ssh", ".gnupg"})
SENSITIVE_WORDS = frozenset(
    {
        "credential", "credentials", "secret", "secrets", "key", "keys", "password", "passwords",
        "token", "tokens", "wallet", "keychain", "private", "ssh", "gnupg", "appdata",
    }
)  # fmt: skip
REQUEST_REFUSED = (
    "this folder cannot be requested as a research folder (the same answer for every location "
    "rule, so that a request cannot probe this computer's folders; the owner sees the reason)"
)
LINKED = (
    "the folder is a link (junction or symbolic link) or another name for a different folder; "
    "ask for the folder it points to"
)
REMOVAL_NOTE = (
    "Research folders cannot be removed here: the sources imported from a folder keep their "
    "binding to it. Removing one needs its own design."
)


def _invalid(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


def _norm(path: Path) -> str:
    return os.path.normcase(str(path)).rstrip("\\/")


def _inside(child: Path, parent: Path) -> bool:
    """``child`` is ``parent`` or lies under it (case-insensitively on Windows)."""
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p + os.sep)


def system_folders() -> tuple[list[Path], list[Path]]:
    """(folders refused with everything inside them, folders refused only themselves)."""
    deep: list[Path] = []
    exact: list[Path] = [Path.home()]
    if sys.platform == "win32":
        for env in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(env)
            if value:
                deep.append(Path(value))
        exact += _users_folders()  # C:\Users itself
    else:
        deep += [Path(p) for p in POSIX_SYSTEM]
        exact += [Path("/home"), Path("/Users")]
    return deep, exact


def _windows_profiles_directory() -> Path | None:
    """The folder Windows keeps every user's profile in, asked of Windows itself: USERPROFILE
    can name any folder (a sandbox, a test environment), so its parent is only the fallback."""
    try:
        import ctypes
        from ctypes import wintypes

        get = ctypes.WinDLL("userenv").GetProfilesDirectoryW
        get.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        get.restype = wintypes.BOOL
        size = wintypes.DWORD(0)
        get(None, ctypes.byref(size))
        if not 0 < size.value <= 32768:
            return None
        buffer = ctypes.create_unicode_buffer(size.value)
        if get(buffer, ctypes.byref(size)) and buffer.value:
            return Path(buffer.value)
    except (OSError, AttributeError, ValueError):
        pass
    return None


def _users_folders() -> list[Path]:
    """The folders that hold every user's profile (``C:/Users``, ``/home``, ``/Users``)."""
    if sys.platform == "win32":
        folder = _windows_profiles_directory()
        if folder is not None:
            return [folder]
        profile = os.environ.get("USERPROFILE")
        return [Path(profile).parent] if profile else []
    return [Path("/home"), Path("/Users")]


def _temporary() -> list[Path]:
    temp = Path(tempfile.gettempdir())
    return [temp, temp.resolve()]


def _location(p: Path, settings: dict[str, Any], *, home: Path, store: Path) -> None:
    """The location rules on an absolute, normalised path (resolved for the owner's check,
    lexical for an agent's request); a refusal names the rule."""
    if p == Path(p.anchor) or len(p.parts) <= 1:
        raise _invalid("a drive or file-system root cannot be a research folder")
    deep, exact = system_folders()
    for system in deep:
        if _inside(p, system) or _inside(p, system.resolve()):
            raise _invalid("a system folder cannot be a research folder", folder=str(system))
    for folder in exact:
        if _norm(p) in (_norm(folder), _norm(folder.resolve())):
            raise _invalid(
                "a whole home or users folder cannot be a research folder; choose the folder "
                "that holds your papers",
            )
    names = [part.casefold() for part in p.parts]
    if KEY_FOLDERS.intersection(names):
        raise _invalid("a folder of keys (.ssh, .gnupg) cannot be a research folder")
    if "appdata" in names and not any(_inside(p, t) for t in _temporary()):
        raise _invalid(
            "AppData holds programs' data and saved credentials; it cannot be a research folder"
        )
    own = Path.home()
    for users in _users_folders():
        if _inside(p, users) and _norm(p) != _norm(users):
            first = p.parts[len(users.parts)].casefold()
            if first == "public":
                raise _invalid("the shared Public profile cannot be a research folder")
            mine = _inside(own, users) and len(own.parts) > len(users.parts)
            if not (mine and first == own.parts[len(users.parts)].casefold()):
                raise _invalid("another user's profile folder cannot be a research folder")
    for managed in (home, home.resolve(), store, store.resolve()):
        if _inside(p, managed) or _inside(managed, p):
            raise _invalid(
                "the PrometheOS home and its project stores cannot be (or be inside) a research "
                "folder"
            )
    for root in settings["allowed_roots"]:
        other = Path(root["path"])
        if _inside(p, other) or _inside(other, p):
            raise _invalid(
                "the folder overlaps the research folder " + root["alias"], alias=root["alias"]
            )


def _alias(settings: dict[str, Any], alias: str) -> None:
    if not re.fullmatch(ALIAS_PATTERN, alias):
        raise _invalid("an alias is a-z, then a-z, 0-9, '_' or '-' (at most 40)", alias=alias)
    if any(r["alias"] == alias for r in settings["allowed_roots"]):
        raise _invalid("this alias is already a research folder", alias=alias)


def _network(path: str) -> bool:
    return path.startswith(("\\\\", "//"))


def check_new_root(
    settings: dict[str, Any],
    alias: str,
    path: str,
    *,
    home: Path,
    store: Path,
) -> str:
    """The resolved path of a research root that may be added, or a refusal naming the rule
    (the owner's check: the folder must exist)."""
    _alias(settings, alias)
    if _network(path):
        raise _invalid("a network (UNC) location cannot be a research folder")
    raw = Path(path)
    if not raw.is_absolute():
        raise _invalid("give the folder's absolute path")
    if not raw.is_dir():
        raise _invalid("the folder does not exist (or is not a directory)", path=path)
    resolved = raw.resolve()
    if _drive_type(resolved) == DRIVE_REMOTE:
        raise _invalid("a network drive cannot be a research folder")
    _location(resolved, settings, home=home, store=store)
    return str(resolved)


def check_requested_root(
    settings: dict[str, Any],
    alias: str,
    path: str,
    *,
    home: Path,
    store: Path,
) -> str:
    """An agent's request, checked without touching the disk: the normalised absolute path, or
    one refusal for every location rule (review round 1, F5). The alias rules are named (they
    reveal nothing about this computer)."""
    _alias(settings, alias)
    try:
        if _network(path) or not Path(path).is_absolute():
            raise _invalid("not a local absolute path")
        lexical = Path(os.path.normpath(path))
        _location(lexical, settings, home=home, store=store)
    except PrometheosError:
        raise _invalid(REQUEST_REFUSED) from None
    return str(lexical)


def same_folder(a: str, b: str) -> bool:
    return _norm(Path(a)) == _norm(Path(b))


def _bases() -> list[tuple[str, Path]]:
    user = Path.home()
    bases = [
        ("your Documents", user / "Documents"),
        ("your Desktop", user / "Desktop"),
        ("your Downloads", user / "Downloads"),
        ("your user folder", user),
        ("the temporary folder", Path(tempfile.gettempdir())),
    ]
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(env)
        if value:
            bases.append(("your OneDrive", Path(value)))
    return bases


def describe_folder(path: str) -> dict[str, Any]:
    """A folder as the owner reviews it, without an absolute path (review round 1, F1): the
    nearest known base by a placeholder, the folders below it, and warnings."""
    p = Path(os.path.normpath(path))
    found = [(label, b) for label, b in _bases() if _inside(p, b)]
    if found:
        label, base = max(found, key=lambda item: len(_norm(item[1])))
        segments = list(p.parts[len(base.parts) :])
    else:
        drive = p.drive.rstrip(":")
        label = f"drive {drive}" if drive else "the file-system root"
        segments = list(p.parts[1:])
    warnings: list[str] = []
    for segment in segments:
        if segment.startswith("."):
            warnings.append(f"hidden folder: {segment}")
        words = set(re.split(r"[^0-9a-z]+", segment.casefold()))
        if words & SENSITIVE_WORDS:
            warnings.append(f"sensitive name: {segment}")
    return {"base": label, "segments": segments, "warnings": warnings}


def folder_text(folder: dict[str, Any]) -> str:
    """``your Documents > papers > 2026``."""
    return " > ".join([folder["base"], *folder["segments"]])


def listing(settings: dict[str, Any], settings_rev: int) -> dict[str, Any]:
    return {
        "settings_rev": settings_rev,
        "roots": [
            {
                "alias": r["alias"],
                "path": r["path"],
                "available": Path(r["path"]).is_dir(),
            }
            for r in settings["allowed_roots"]
        ],
        "removal": REMOVAL_NOTE,
    }


def plan_add(
    settings: dict[str, Any],
    settings_rev: int,
    alias: str,
    path: str,
    *,
    home: Path,
    store: Path,
    project_id: str,
) -> dict[str, Any]:
    resolved = check_new_root(settings, alias, path, home=home, store=store)
    return {
        "project_id": project_id,
        "alias": alias,
        "path": resolved,
        "settings_rev": settings_rev,
        "folder_view": describe_folder(resolved),
    }


def plan_request(
    settings: dict[str, Any],
    settings_rev: int,
    alias: str,
    path: str,
    *,
    home: Path,
    store: Path,
    project_id: str,
) -> dict[str, Any]:
    """An agent's request (an approval plan): checked lexically only (F5); the owner's review,
    the grant and the execution apply the full rules."""
    lexical = check_requested_root(settings, alias, path, home=home, store=store)
    return {
        "project_id": project_id,
        "alias": alias,
        "path": lexical,
        "settings_rev": settings_rev,
        "folder_view": describe_folder(lexical),
    }


def review(
    settings: dict[str, Any],
    settings_rev: int,
    plan: dict[str, Any],
    *,
    home: Path,
    store: Path,
) -> dict[str, Any]:
    """The owner's review of a requested folder: the full rules now, and whether the folder is
    still exactly the one requested (not a link to another)."""
    shown: dict[str, Any] = {"kind": "project_root_add", "folder_view": plan.get("folder_view")}
    if settings_rev != plan["settings_rev"]:
        return shown | {
            "matches_plan": False,
            "error": "the project settings changed since the request; deny it and let the "
            "agent ask again",
        }
    try:
        resolved = check_new_root(settings, plan["alias"], plan["path"], home=home, store=store)
    except PrometheosError as exc:
        return shown | {"matches_plan": False, "error": f"cannot be added: {exc.message}"}
    if not same_folder(resolved, plan["path"]):
        return shown | {"matches_plan": False, "error": LINKED}
    return shown | {"matches_plan": True}


def execute_add(
    store: Any,
    plan: dict[str, Any],
    actor: str,
    *,
    home: Path,
    request_id: str,
    grant: Any = None,
    grant_digest: str | None = None,
) -> dict[str, Any]:
    """One transaction: the project settings at the planned revision gain the root (checked
    again: the folder may have moved since the plan), as a ``project_update`` revision."""
    from prometheos.app.apply import validate_body
    from prometheos.domain.canonical import canonical_sha256
    from prometheos.store import capabilities
    from prometheos.store.project import Txn

    stored: list[str] = []

    def handler(txn: Txn) -> None:
        if grant is not None:
            capabilities.consume(
                txn.conn,
                secret=grant,
                project_id=plan["project_id"],
                operation="exact_request",
                digest=grant_digest,
            )
        state = txn.get(plan["project_id"])
        if state is None or state.rev != plan["settings_rev"]:
            raise PrometheosError(
                "revision_conflict",
                "the project settings changed since the folder was planned; plan it again",
                {},
            )
        resolved = check_new_root(
            state.body, plan["alias"], plan["path"], home=home, store=store.directory
        )
        if not same_folder(resolved, plan["path"]):
            raise _invalid(LINKED)
        stored.append(resolved)
        roots = [*state.body["allowed_roots"], {"alias": plan["alias"], "path": resolved}]
        body = validate_body("project", {**state.body, "allowed_roots": roots}).model_dump(
            mode="json"
        )
        txn.op_index = 0
        txn.write_revision(
            plan["project_id"], "project", state.rev + 1, txn.insert_body(body), state.sensitivity,
            "active", None, "project_update", body=body,
        )  # fmt: skip
        txn.audit(f"project_root_add:{plan['alias']}", plan["project_id"], state.rev + 1)
        txn.result(plan["project_id"], state.rev + 1)

    commit = store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256({"op": "project_root_add", "plan": plan}),
        expected_seq=None,
        actor=actor,
        handler=handler,
    )
    return {"alias": plan["alias"], "path": stored[0], "commit": commit}
