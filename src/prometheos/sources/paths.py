"""Safe resolution of research files inside explicitly approved roots.

Resolve approved root + normalized relative path; reject traversal, absolute,
drive-qualified, UNC/device and reserved Windows names; refuse any symlink or
reparse point (including NTFS junctions) on the way; check final containment
and re-check identity after opening. Same-user hostile processes are not
excluded (spec/08-operations.md).
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from prometheos.domain.models import PrometheosError

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$", re.I)
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def is_device_or_unc(raw: str) -> bool:
    text = raw.replace("/", "\\")
    return text.startswith("\\\\") or text.startswith("\\??\\")


def normalize_relative(relative: str) -> str:
    """Validate a root-relative path and return it in POSIX form."""
    if not relative or len(relative) > 1024:
        raise PrometheosError("validation_error", "relative path is empty or too long", {})
    if is_device_or_unc(relative) or re.match(r"^[A-Za-z]:", relative):
        raise PrometheosError(
            "policy_denied", "absolute, drive, UNC or device paths are refused", {}
        )
    candidate = relative.replace("\\", "/")
    if candidate.startswith("/"):
        raise PrometheosError("policy_denied", "absolute paths are refused", {})
    # Split manually: PurePosixPath would silently collapse "." and "//" segments, but
    # the relative path is part of a source's identity, so it must be exact.
    parts = candidate.split("/")
    for part in parts:
        if part in ("..", ".", ""):
            raise PrometheosError("policy_denied", "path traversal is refused", {})
        if _BAD_CHARS.search(part) or _RESERVED.match(part) or part.endswith((" ", ".")):
            raise PrometheosError(
                "policy_denied", "reserved or invalid path component is refused", {}
            )
    return "/".join(parts)


@dataclass(frozen=True)
class ResolvedPath:
    root: Path
    relative: str
    path: Path


# Version-control metadata is never a research source nor a workspace file (decision 0012
# review round 2, R2-2): a clone's .git/config may hold a credential-bearing remote URL.
VCS_DIRECTORIES = frozenset({".git", ".hg", ".svn"})
_SHORT_NAME = re.compile(r"~\d")


def _long_name(folder: Path, part: str) -> str | None:
    """The on-disk (long) name of ``part`` inside ``folder``, or None if it does not exist.

    Directory entries list long names only, so a Windows 8.3 alias (``GIT~1``) finds no
    entry even though the path exists; that case is refused by the caller."""
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    if part in names:
        return part
    folded = part.casefold()
    matches = [n for n in names if n.casefold() == folded]
    return matches[0] if len(matches) == 1 else None


def resolve_in_root(root: Path, relative: str) -> ResolvedPath:
    """Resolve ``relative`` inside ``root`` without following links or reparse points.

    The returned ``relative`` is canonical whether or not the target exists: every existing
    component carries its real long name and case, the rest are appended as given. A
    component that exists only under an 8.3 short alias, a missing component that looks
    like one (``~`` followed by a digit), and version-control metadata (``.git``, ``.hg``,
    ``.svn``, including a ``.git`` file) are refused (decision 0012 review round 2)."""
    rel = normalize_relative(relative)
    if is_device_or_unc(str(root)) and not root.is_absolute():
        raise PrometheosError("policy_denied", "root must be an absolute path", {})
    if not root.is_absolute():
        raise PrometheosError("policy_denied", "root must be an absolute path", {})
    if not root.is_dir():
        raise PrometheosError("source_unavailable", "research root is unavailable", {})
    real_root = Path(os.path.realpath(root))
    current = root
    canonical_parts: list[str] = []
    exists = True
    for part in rel.split("/"):
        name = part
        if exists:
            candidate = current / part
            if os.path.lexists(candidate):
                long_name = _long_name(current, part)
                if long_name is None:
                    raise PrometheosError(
                        "policy_denied", "short (8.3) file names are refused; use the full name", {}
                    )
                name = long_name
            else:
                exists = False
        if not exists and _SHORT_NAME.search(part):
            raise PrometheosError(
                "policy_denied",
                "a new name containing '~' followed by a digit is refused: it could be read "
                "as a Windows short (8.3) name; choose another name",
                {},
            )
        if name.casefold() in VCS_DIRECTORIES:
            raise PrometheosError(
                "policy_denied", "version-control metadata is not a research file", {}
            )
        canonical_parts.append(name)
        current = current / name
        if is_link_or_reparse(current):
            raise PrometheosError(
                "policy_denied", "links and reparse points inside research roots are refused", {}
            )
    real = Path(os.path.realpath(current))
    try:
        real.relative_to(real_root)
    except ValueError as exc:
        raise PrometheosError("policy_denied", "path escapes its research root", {}) from exc
    return ResolvedPath(root=root, relative="/".join(canonical_parts), path=current)


def relative_to_root(root: Path, absolute: Path) -> str:
    """Express an absolute path given by the user relative to an approved root."""
    try:
        rel = Path(os.path.abspath(absolute)).relative_to(Path(os.path.abspath(root)))
    except ValueError as exc:
        raise PrometheosError(
            "policy_denied", "file is outside the approved research root", {}
        ) from exc
    return rel.as_posix()


def open_verified(resolved: ResolvedPath) -> BinaryIO:
    """Open a regular file and check it is the same object that was validated."""
    try:
        before = os.lstat(resolved.path)
    except FileNotFoundError as exc:
        raise PrometheosError("source_unavailable", "source file is missing", {}) from exc
    if not stat.S_ISREG(before.st_mode):
        raise PrometheosError("policy_denied", "only regular files can be imported", {})
    handle = open(resolved.path, "rb")  # returned to the caller, who closes it
    after = os.fstat(handle.fileno())
    if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev) or is_link_or_reparse(
        resolved.path
    ):
        handle.close()
        raise PrometheosError("policy_denied", "file changed identity while opening", {})
    return handle
