"""The adoption inventory (A05, CP17.1; decision 0039 section 4; C17-INVENTORY).

Walks an adoption root without writing into it and returns one row per entry: stat, content
digest, normalised-text digest, container type, zone, and what could not be read and why.
Cloud placeholders are recorded from their directory entry and never opened. Links and
reparse points are recorded and never followed. Version-control metadata is summarised, never
listed. ZIP archives are listed from their central directory within limits; nothing is
extracted to disk. Nothing here opens a file for writing, creates, renames or deletes anything,
or runs a program.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat as stat_mod
import struct
import sys
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from prometheos.adoption.contracts import (
    MEMBER_SEPARATOR,
    AdoptionFile,
    ArchiveLimits,
    ScanConfig,
    ScanSummary,
    escape_segment,
    member_path,
)

# Windows file attributes of cloud placeholders (decision 0039 section 4; R4 1.3 M2).
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
PLACEHOLDER_BITS = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

VCS_FOLDERS = frozenset({".git", ".hg", ".svn"})
CHUNK = 1024 * 1024
SNIFF_BYTES = 8192
TEXT_LIMIT = 16 * 1024 * 1024  # larger text files get a content digest, not a text digest
NORMALISATION = "text-v1"

GENERIC_NOISE: tuple[str, ...] = (
    "**/__pycache__/**",
    "**/.venv/**",
    "**/node_modules/**",
    "**/.ipynb_checkpoints/**",
    "**/slprj/**",
    "**/Thumbs.db",
    "**/desktop.ini",
    "**/.DS_Store",
    "**/~$*",
    "**/.~lock.*#",
)


# ----- globs ---------------------------------------------------------------------------------


def compile_glob(pattern: str) -> re.Pattern[str]:
    """A path glob over root-relative POSIX paths: ``**`` spans folders, ``*`` and ``?`` stay
    inside one name, everything else is literal. Matching ignores case (Windows names)."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


@dataclass(frozen=True)
class Globs:
    patterns: tuple[re.Pattern[str], ...]

    @classmethod
    def of(cls, globs: list[str] | tuple[str, ...]) -> Globs:
        return cls(tuple(compile_glob(g) for g in globs))

    def match(self, path: str, folder: bool = False) -> bool:
        """``folder``: also try ``path/`` so ``**/name/**`` excludes the folder itself."""
        probes = (path, path + "/") if folder else (path,)
        return any(p.match(probe) for p in self.patterns for probe in probes)


# ----- stat ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stat:
    kind: str  # "dir", "file", "link", "other"
    size: int
    mtime_ns: int
    attributes: int
    reparse_tag: int = 0


# A reparse tag with the name-surrogate bit redirects to another name (symbolic links,
# junctions/mount points): a link, never followed. Other reparse points (cloud placeholders,
# deduplicated or compressed files) are files or folders whose attributes decide the rest
# (review cp17-1-a F4).
NAME_SURROGATE_BIT = 0x20000000


def entry_stat(entry: os.DirEntry[str]) -> Stat:
    """The stat of a directory entry without following links. On Windows it comes from the
    directory listing itself, so no file is opened (a placeholder is never hydrated)."""
    info = entry.stat(follow_symlinks=False)
    attributes = int(getattr(info, "st_file_attributes", 0))
    tag = int(getattr(info, "st_reparse_tag", 0))
    kind = kind_of(info.st_mode, attributes, tag)
    return Stat(kind, int(info.st_size), int(info.st_mtime_ns), attributes, tag)


def kind_of(mode: int, attributes: int, reparse_tag: int) -> str:
    """``link`` for a symbolic link or a name-surrogate reparse point (junction, mount point);
    otherwise ``dir``, ``file`` or ``other`` from the mode and attributes, so a cloud
    placeholder (a reparse point without the name-surrogate bit) stays a file or folder."""
    reparse_link = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT) and bool(
        reparse_tag & NAME_SURROGATE_BIT
    )
    if stat_mod.S_ISLNK(mode) or reparse_link:
        return "link"
    if stat_mod.S_ISDIR(mode) or attributes & 0x10:  # FILE_ATTRIBUTE_DIRECTORY
        return "dir"
    if stat_mod.S_ISREG(mode):
        return "file"
    return "other"


StatProvider = Callable[[os.DirEntry[str]], Stat]


def os_path(path: Path) -> str:
    """The form handed to the OS: extended-length on Windows, so paths over 260 characters
    are read like any other."""
    text = str(path)
    if sys.platform == "win32" and not text.startswith("\\\\"):
        return "\\\\?\\" + os.path.abspath(text)
    return text


def is_placeholder(st: Stat) -> bool:
    return bool(st.attributes & PLACEHOLDER_BITS)


def _error(exc: BaseException) -> str:
    errno = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    code = f" (errno {errno})" if errno is not None else ""
    if winerror is not None:
        code += f" (winerror {winerror})"
    return f"{type(exc).__name__}{code}"[:300]


# ----- walking -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Walked:
    rel: str  # the real names, "/"-separated (unescaped)
    stat: Stat | None
    vcs: bool = False  # a version-control folder: summarised, never entered
    error: str | None = None  # the folder could not be listed, or the entry not statted
    pruned: str | None = None  # "excluded" or "placeholder": a folder that is not entered


Prune = Callable[[str, Stat], str | None]


def walk(
    root: Path, stat_of: StatProvider = entry_stat, prune: Prune | None = None
) -> Iterator[Walked]:
    """Every entry under ``root``, depth first, sorted by name, links not followed. A VCS
    folder, a placeholder folder and a folder ``prune`` names are yielded once and not
    entered. A folder that cannot be listed and an entry that cannot be statted are yielded
    with ``error`` and the walk goes on (review cp17-1-a F3, F5, F10)."""

    def visit(folder: str, prefix: str) -> Iterator[Walked]:
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            yield Walked(prefix.rstrip("/"), None, error=_error(exc), pruned="unlisted")
            return
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            try:
                st = stat_of(entry)
            except OSError as exc:
                yield Walked(rel, None, error=_error(exc))
                continue
            if st.kind == "dir":
                if entry.name.casefold() in VCS_FOLDERS:
                    yield Walked(rel, st, vcs=True)
                    continue
                if is_placeholder(st):
                    yield Walked(rel, st, pruned="placeholder")
                    continue
                reason = prune(rel, st) if prune is not None else None
                if reason is not None:
                    yield Walked(rel, st, pruned=reason)
                    continue
                yield Walked(rel, st)
                yield from visit(os.path.join(folder, entry.name), rel + "/")
            else:
                yield Walked(rel, st)

    if prefix_error := _list_error(os_path(root)):
        raise OSError(prefix_error)
    yield from visit(os_path(root), "")


def _list_error(folder: str) -> str | None:
    try:
        with os.scandir(folder):
            return None
    except OSError as exc:
        return _error(exc)


def root_digest(
    root: Path, stat_of: StatProvider = entry_stat, prune: Prune | None = None
) -> tuple[str, int]:
    """SHA-256 over one line per entry the walk sees: ``path NUL kind NUL size NUL mtime_ns NUL
    attributes`` for files and links, ``path NUL dir`` for folders, and a token for a folder
    that is not entered (version control, placeholder, excluded) or cannot be listed. It
    **detects** an entry created, deleted, renamed, resized, re-timed or with changed
    attributes; it is not a content proof (a rewrite that restores size and time is not seen;
    tests add a full content digest). Folder times are left out: NTFS updates a folder's time in
    its parent's listing lazily (decision 0039 section 4). Returns ``(digest, entries)``."""
    h = hashlib.sha256()
    count = 0
    for walked in walk(root, stat_of, prune):
        st = walked.stat
        if walked.error is not None:
            line = f"{walked.rel}\0unreadable\0{walked.error}\n"
        elif st is None:  # pragma: no cover - walk yields a stat or an error
            continue
        elif walked.vcs or walked.pruned is not None:
            label = "vcs" if walked.vcs else walked.pruned
            line = f"{walked.rel}\0{label}\0{st.attributes}\n"
        elif st.kind == "dir":
            line = f"{walked.rel}\0dir\n"
        else:
            line = f"{walked.rel}\0{st.kind}\0{st.size}\0{st.mtime_ns}\0{st.attributes}\n"
        h.update(line.encode("utf-8", "surrogatepass"))
        count += 1
    return h.hexdigest(), count


def folder_totals(path: Path) -> tuple[int, int]:
    """File count and bytes under a folder from directory entries only (nothing opened; no
    placeholder folder entered; names not kept)."""
    files = size = 0
    stack = [os_path(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry_stat(entry)
                    except OSError:
                        continue
                    if st.kind == "dir" and not is_placeholder(st):
                        stack.append(entry.path)
                    elif st.kind == "file":
                        files += 1
                        size += st.size
        except OSError:
            continue
    return files, size


# ----- sniffing and digests ------------------------------------------------------------------

_OPC_TYPES = {
    "application/vnd.mathworks.simulink.model": "opc_slx",
    "application/vnd.mathworks.simulink.library": "opc_slx",
    "application/vnd.mathworks.matlab.code": "opc_mlx",
    "application/vnd.mathworks.simulink.simulinkcache": "opc_slxc",
}
_MW_CONTENT_TYPE = re.compile(rb"<contentType>([^<]{1,200})</contentType>")


def decode_text(data: bytes) -> str | None:
    """The text of ``data`` if it is text (UTF-8 with or without BOM, UTF-16 with BOM, or
    Windows-1252 without control characters), else ``None``."""
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data[3:].decode("utf-8")
        except UnicodeDecodeError:
            return None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("cp1252")
    except UnicodeDecodeError:
        return None
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r\f")
    return text if controls <= len(text) // 100 else None


def normalised_text_digest(text: str) -> str:
    """``text-v1``: line endings to LF, trailing spaces and tabs removed per line, trailing
    empty lines removed; SHA-256 of the UTF-8 bytes (decision 0039 section 4)."""
    lines = [
        line.rstrip(" \t") for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    while lines and lines[-1] == "":
        lines.pop()
    return hashlib.sha256("\n".join(lines).encode("utf-8", "surrogatepass")).hexdigest()


def _looks_mat_v4(head: bytes) -> bool:
    if len(head) < 20:
        return False
    for fmt in ("<5i", ">5i"):
        mopt, rows, cols, imag, namelen = struct.unpack(fmt, head[:20])
        m, rest = divmod(mopt, 1000)
        o, rest = divmod(rest, 100)
        p, t = divmod(rest, 10)
        if (
            0 <= mopt < 5000
            and m in (0, 1)
            and o == 0
            and p <= 5
            and t <= 2
            and rows >= 0
            and cols >= 0
            and imag in (0, 1)
            and 0 < namelen < 64
        ):
            return True
    return False


def sniff(head: bytes, name: str, size: int) -> str:
    """The container type from the first bytes (and the name only where a format has no
    magic: MAT v4)."""
    if size == 0:
        return "empty"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "zip"  # refined to an OPC type once the central directory is read
    if head.startswith(b"MATLAB 7.3 MAT-file"):
        return "mat_v73"
    if head.startswith(b"MATLAB 5.0 MAT-file") or head.startswith(b"MATLAB 7.0 MAT-file"):
        return "mat_v5"
    if head.startswith(b"\x89HDF\r\n\x1a\n"):
        return "hdf5"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"MZ", b"\x7fELF")):
        return "executable"
    if name.lower().endswith(".mat") and _looks_mat_v4(head):
        return "mat_v4"
    return (
        "text"
        if decode_text(head[:SNIFF_BYTES] if size > SNIFF_BYTES else head) is not None
        else "binary"
    )


def _probe_text(head: bytes, size: int) -> bool:
    """Text sniffing on a prefix can cut a multi-byte character; retry without the tail."""
    if decode_text(head) is not None:
        return True
    return size > len(head) and decode_text(head[:-3]) is not None


def opc_kind(archive: zipfile.ZipFile) -> str | None:
    """The OPC subtype of a ZIP (``[Content_Types].xml`` present), else ``None``."""
    names = set(archive.namelist())
    if "[Content_Types].xml" not in names:
        return None
    if "metadata/mwcoreProperties.xml" in names:
        info = archive.getinfo("metadata/mwcoreProperties.xml")
        if info.file_size <= 64 * 1024:
            found = _MW_CONTENT_TYPE.search(archive.read(info))
            if found:
                return _OPC_TYPES.get(
                    found.group(1).decode("ascii", "replace").strip(), "opc_other"
                )
    if any(n.startswith(("word/", "xl/", "ppt/")) for n in names):
        return "opc_office"
    return "opc_other"


# ----- zones ---------------------------------------------------------------------------------

_ZONE_BY_EXT = {
    **dict.fromkeys(
        (
            ".m",
            ".py",
            ".r",
            ".jl",
            ".c",
            ".h",
            ".cpp",
            ".hpp",
            ".java",
            ".js",
            ".ts",
            ".f90",
            ".sh",
            ".ps1",
            ".bat",
        ),
        "source_code",
    ),
    **dict.fromkeys((".mlx", ".ipynb", ".rmd", ".qmd"), "notebooks"),
    **dict.fromkeys((".slx", ".mdl", ".sldd", ".prj", ".mo", ".ssc"), "models_sim"),
    **dict.fromkeys(
        (".mat", ".csv", ".tsv", ".h5", ".hdf5", ".nc", ".parquet", ".xlsx", ".npz"), "data_derived"
    ),
    **dict.fromkeys((".tex", ".bib", ".cls", ".sty"), "manuscript"),
    **dict.fromkeys((".md", ".txt", ".rst", ".org"), "notes"),
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".svg", ".fig", ".eps"), "results"),
    **dict.fromkeys((".zip", ".7z", ".tar", ".gz", ".tgz", ".rar"), "archive"),
    **dict.fromkeys((".asv", ".bak", ".orig"), "backup_copy"),
    ".slxc": "env_cache_noise",
    ".pdf": "literature",
    ".exe": "vendored",
    ".dll": "vendored",
}
_AGENT_FOLDERS = frozenset({".claude", ".kiro", ".cursor", ".codex", ".github", "graphify-out"})
_AGENT_FILES = frozenset({"agents.md", "claude.md", "gemini.md", "llms.txt"})
_BACKUP_NAME = re.compile(
    r"(?:^|[ _.\-])(?:backup|bak|copy)(?:[ _.\-]|\d|$)|\(\d+\)|\.r20\d\d[ab]$", re.IGNORECASE
)
_BACKUP_FOLDERS = frozenset({"backup", "backups", "bak", "old", "legacy", "archive", "archived"})


def zone_of(rel: str, container: str | None, noise: bool) -> str:
    """A first, generic zone from the path (profiles refine it in CP17.2)."""
    if noise:
        return "env_cache_noise"
    parts = rel.split(MEMBER_SEPARATOR)[0].split("/")
    name = parts[-1].lower()
    folders = [p.lower() for p in parts[:-1]]
    if any(p in _AGENT_FOLDERS for p in folders) or name in _AGENT_FILES:
        return "agent_memory"
    if (
        folders[:2] == ["resources", "project"]
        or "/resources/project/" in "/" + "/".join(folders) + "/"
    ):
        return "tool_output"
    if any(p in _BACKUP_FOLDERS for p in folders):
        return "backup_copy"
    if _BACKUP_NAME.search(name):
        return "backup_copy"
    if container == "executable":
        return "vendored"
    ext = os.path.splitext(name)[1]
    return _ZONE_BY_EXT.get(ext, "unknown")


# ----- the inventory -------------------------------------------------------------------------


@dataclass
class Result:
    rows: list[AdoptionFile] = field(default_factory=list)
    summary: ScanSummary | None = None


class ChangedWhileRead(OSError):
    """The file changed identity or size while it was being read."""


def _read_and_hash(path: Path, size_hint: int) -> tuple[str, bytes, bytes | None, int]:
    """``(sha256, head, whole, bytes read)`` from a read-only handle: the content digest, the
    first bytes, and the whole content when it may be text and fits ``TEXT_LIMIT``. The opened
    file must be the entry that was listed: a file swapped for a link (or another file)
    between the walk and the read is refused (review cp17-1-a F13)."""
    target = os_path(path)
    before = os.lstat(target)
    attributes = int(getattr(before, "st_file_attributes", 0))
    surrogate = int(getattr(before, "st_reparse_tag", 0)) & NAME_SURROGATE_BIT
    if stat_mod.S_ISLNK(before.st_mode) or (
        attributes & FILE_ATTRIBUTE_REPARSE_POINT and surrogate
    ):
        raise ChangedWhileRead("the entry became a link before it was read")
    h = hashlib.sha256()
    head = b""
    count = 0
    keep: list[bytes] | None = [] if size_hint <= TEXT_LIMIT else None
    with open(target, "rb") as handle:
        after = os.fstat(handle.fileno())
        if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev):
            raise ChangedWhileRead("the file changed identity while it was opened")
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            count += len(chunk)
            if len(head) < SNIFF_BYTES:
                head += chunk[: SNIFF_BYTES - len(head)]
            if keep is not None:
                keep.append(chunk)
                if count > TEXT_LIMIT:
                    keep = None
    return h.hexdigest(), head, (b"".join(keep) if keep is not None else None), count


def escaped(rel: str) -> tuple[str, str | None]:
    """The row path of a real relative name (``!`` doubled per segment) and, when the name
    cannot be a row path as it is (invalid Unicode or a control character), a sanitised path
    with a digest of the raw name and the problem (review cp17-1-a F1)."""
    try:
        rel.encode("utf-8")
        valid = not _CONTROL_CHARS.search(rel)
    except UnicodeEncodeError:
        valid = False
    if valid:
        return "/".join(escape_segment(p) for p in rel.split("/")), None
    raw = rel.encode("utf-16-le", "surrogatepass")
    digest = hashlib.sha256(raw).hexdigest()[:8]
    clean = rel.encode("utf-8", "replace").decode("utf-8")
    clean = _CONTROL_CHARS.sub("?", clean.replace("\\", "?"))
    parts = [escape_segment(p) or "_" for p in clean.split("/")]
    parts[-1] = f"{parts[-1]}~{digest}"
    return "/".join(parts), "name is not valid Unicode or holds control characters"


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_NESTED = (".zip", ".slx", ".mlx", ".docx", ".xlsx", ".pptx", ".slxc")


@dataclass
class _Counts:
    status: Counter[str] = field(default_factory=Counter)
    container: Counter[str] = field(default_factory=Counter)
    zone: Counter[str] = field(default_factory=Counter)


class ListingFailed(Exception):
    """An archive's listing failed part-way: none of its members are kept."""


def central_directory(handle: BinaryIO) -> tuple[int, int] | None:
    """``(entries, central directory bytes)`` from a ZIP's end records, read from the tail
    only, so limits apply before Python builds one object per member (review cp17-1-a F7)."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    tail_size = min(size, 65_557 + 20 + 56)
    handle.seek(size - tail_size)
    tail = handle.read(tail_size)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or at + 22 > len(tail):
        return None
    entries, cd_size = (
        struct.unpack("<H", tail[at + 10 : at + 12])[0],
        struct.unpack("<I", tail[at + 12 : at + 16])[0],
    )
    if entries == 0xFFFF or cd_size == 0xFFFFFFFF:
        locator = tail.rfind(b"PK\x06\x07", 0, at)
        if locator >= 0:
            offset = struct.unpack("<Q", tail[locator + 8 : locator + 16])[0]
            handle.seek(offset)
            record = handle.read(56)
            if record.startswith(b"PK\x06\x06") and len(record) >= 56:
                entries = struct.unpack("<Q", record[32:40])[0]
                cd_size = struct.unpack("<Q", record[40:48])[0]
    handle.seek(0)
    return int(entries), int(cd_size)


class Inventory:
    """One inventory pass over one root. ``progress`` is called with (done, total) and may
    raise to stop (a run's cancellation)."""

    def __init__(
        self,
        root: Path,
        scan_gen: int,
        config: ScanConfig,
        *,
        stat_of: StatProvider = entry_stat,
        noise: tuple[str, ...] = GENERIC_NOISE,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        self.root = root
        self.scan_gen = scan_gen
        self.config = config
        self.limits = config.limits
        self.stat_of = stat_of
        self.exclude = Globs.of(config.exclusions)
        self.noise = Globs.of(noise)
        self.progress = progress
        self.rows: list[AdoptionFile] = []

    # rows -------------------------------------------------------------------------------

    def _make(self, path: str, **values: Any) -> AdoptionFile:
        defaults: dict[str, Any] = {
            "scan_gen": self.scan_gen,
            "path": path,
            "parent": None,
            "depth": 0,
            "entry": "file",
            "size": None,
            "mtime_us": None,
            "attributes": None,
            "sha256": None,
            "text_sha256": None,
            "container": None,
            "status": "read",
            "reason": None,
            "zone": "unknown",
            "change": None,
            "detail": None,
        }
        defaults.update(values)
        return AdoptionFile.model_validate(defaults)

    def _row(self, path: str, **values: Any) -> None:
        self.rows.append(self._make(path, **values))

    # walk -------------------------------------------------------------------------------

    def prune(self, rel: str, st: Stat) -> str | None:
        """A folder the owner excluded is summarised, not entered (review cp17-1-a F10)."""
        return "excluded" if self.exclude.match(rel, folder=True) else None

    def run(self) -> Result:
        started = time.monotonic()
        walked = list(walk(self.root, self.stat_of, self.prune))
        items = [
            w
            for w in walked
            if w.error is not None
            or w.vcs
            or w.pruned is not None
            or (w.stat is not None and w.stat.kind != "dir")
        ]
        for done, item in enumerate(items, start=1):
            if self.progress is not None and done % 50 == 0:
                self.progress(done, len(items))
            try:
                self._one(item)
            except Exception as exc:  # an untrusted name or stat never stops a scan (F1-F3)
                self._fallback(item, exc)
        if self.progress is not None:
            self.progress(len(items), len(items))
        return Result(self.rows, self._summary(time.monotonic() - started))

    def _fallback(self, item: Walked, exc: Exception) -> None:
        path, _ = escaped(item.rel)
        folder = item.stat is not None and item.stat.kind == "dir"
        entry = "folder" if folder else "file"
        problem = f"could not be recorded ({type(exc).__name__})"
        try:
            self._row(path, entry=entry, status="unreadable", reason=problem)
        except ValueError:
            digest = hashlib.sha256(item.rel.encode("utf-8", "surrogatepass")).hexdigest()[:16]
            self._row(f"unrecordable~{digest}", entry=entry, status="unreadable", reason=problem)

    def _is_noise(self, rel: str) -> bool:
        parts = rel.split("/")
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts))]
        return self.noise.match(rel) or any(self.noise.match(p, folder=True) for p in prefixes)

    def _one(self, item: Walked) -> None:
        rel, st = item.rel, item.stat
        path, problem = escaped(rel)
        if st is None:  # a folder that could not be listed, or an entry that vanished
            entry = "folder" if item.pruned == "unlisted" else "file"
            self._row(path, entry=entry, status="unreadable", reason=item.error or "unreadable")
            return
        base = {
            "size": st.size if st.kind != "dir" else None,
            "mtime_us": st.mtime_ns // 1000,
            "attributes": st.attributes & 0xFFFFFFFF,
        }
        if item.vcs:
            files, total = folder_totals(self.root / rel)
            self._row(
                path,
                entry="folder",
                status="noise",
                zone="tool_output",
                size=total,
                mtime_us=base["mtime_us"],
                attributes=base["attributes"],
                detail={"vcs": rel.rsplit("/", 1)[-1].lower(), "files": files},
            )
            return
        if item.pruned == "placeholder":
            self._row(
                path,
                entry="folder",
                status="placeholder",
                zone=zone_of(rel, None, False),
                mtime_us=base["mtime_us"],
                attributes=base["attributes"],
                detail={"attributes": f"0x{st.attributes:08x}"},
            )
            return
        if item.pruned == "excluded":
            files, total = folder_totals(self.root / rel)
            self._row(
                path,
                entry="folder",
                status="excluded",
                size=total,
                mtime_us=base["mtime_us"],
                attributes=base["attributes"],
                detail={"files": files},
            )
            return
        if problem is not None:
            self._row(path, status="unreadable", reason=problem, **base)
            return
        if self.exclude.match(rel):
            self._row(path, status="excluded", **base)
            return
        noise = self._is_noise(rel)
        if st.kind == "link":
            self._row(path, entry="link", status="link", zone=zone_of(rel, None, noise), **base)
            return
        if is_placeholder(st):
            self._row(
                path,
                status="placeholder",
                zone=zone_of(rel, None, noise),
                detail={"attributes": f"0x{st.attributes:08x}"},
                **base,
            )
            return
        if st.kind == "other":
            self._row(path, status="unreadable", reason="not a regular file", **base)
            return
        if noise:
            self._row(path, status="noise", zone="env_cache_noise", **base)
            return
        self._read_file(rel, path, st, base)

    def _read_file(self, rel: str, path: str, st: Stat, base: dict[str, Any]) -> None:
        try:
            sha, head, whole, count = _read_and_hash(self.root / rel, st.size)
        except OSError as exc:
            failure = (
                f"changed while it was read: {exc}"
                if isinstance(exc, ChangedWhileRead)
                else _error(exc)
            )
            self._row(
                path,
                status="unreadable",
                reason=failure[:300],
                zone=zone_of(rel, None, False),
                **base,
            )
            return
        if count != st.size:  # review cp17-1-a F6: the listing and the bytes disagree
            self._row(
                path,
                status="unreadable",
                zone=zone_of(rel, None, False),
                reason=f"changed while it was read (listed {st.size} bytes, read {count})",
                **base,
            )
            return
        container = sniff(head, rel, st.size)
        text_sha = None
        if container == "text" or (
            container == "binary" and whole is not None and _probe_text(head, st.size)
        ):
            text = decode_text(whole) if whole is not None else None
            if text is not None:
                container = "text"
                text_sha = normalised_text_digest(text)
            elif whole is not None:
                container = "binary"
        detail: dict[str, Any] | None = None
        status: str = "read"
        reason: str | None = None
        members: list[AdoptionFile] = []
        if container == "zip":
            container, detail, status, reason, members = self._archive(rel, path)
        self._row(
            path,
            sha256=sha,
            text_sha256=text_sha,
            container=container,
            status=status,
            reason=reason,
            zone=zone_of(rel, container, False),
            detail=detail,
            **base,
        )
        self.rows.extend(members)

    # archives ---------------------------------------------------------------------------

    def _archive(
        self, rel: str, path: str
    ) -> tuple[str, dict[str, Any] | None, str, str | None, list[AdoptionFile]]:
        """List a ZIP on disk: the archive row's ``(container, detail, status, reason)`` and
        its member rows. A listing that fails part-way keeps no member (F2)."""
        try:
            with open(os_path(self.root / rel), "rb") as handle:
                return self._list_zip(handle, path, depth=1)
        except Exception as exc:  # corrupt or hostile archives: zlib, lzma, struct, ...
            return "zip", None, "unreadable", f"zip listing failed: {_error(exc)}", []

    def _limit(self, infos: list[zipfile.ZipInfo]) -> str | None:
        lim: ArchiveLimits = self.limits
        if len(infos) > lim.max_members:
            return f"max_members: {len(infos)} > {lim.max_members}"
        total = sum(i.file_size for i in infos)
        if total > lim.max_total_uncompressed:
            return f"max_total_uncompressed: {total} > {lim.max_total_uncompressed}"
        packed = sum(i.compress_size for i in infos)
        if total > 0 and total > lim.max_ratio * max(packed, 1):
            return f"max_ratio: {total // max(packed, 1)} > {lim.max_ratio}"
        for info in infos:
            ratio = info.file_size // max(info.compress_size, 1)
            if ratio > lim.max_ratio and info.file_size > 1024:
                return f"max_ratio: member ratio {ratio} > {lim.max_ratio}"
        return None

    def _list_zip(
        self, handle: BinaryIO, path: str, *, depth: int
    ) -> tuple[str, dict[str, Any] | None, str, str | None, list[AdoptionFile]]:
        directory = central_directory(handle)
        if directory is not None:
            entries, cd_size = directory
            if entries > self.limits.max_members:
                return (
                    "zip",
                    {"members": entries},
                    "limited",
                    (f"max_members: {entries} > {self.limits.max_members}"),
                    [],
                )
            if cd_size > max(self.limits.max_members, 1) * 4096:
                return (
                    "zip",
                    {"members": entries},
                    "limited",
                    (f"max_members: central directory of {cd_size} bytes"),
                    [],
                )
        with zipfile.ZipFile(handle) as archive:
            infos = archive.infolist()
            limited = self._limit(infos)
            kind = None
            if limited is None or "[Content_Types].xml" in archive.namelist():
                try:
                    kind = opc_kind(archive) if limited is None else None
                except Exception as exc:  # a damaged package part
                    return (
                        "zip",
                        {"members": len(infos)},
                        "unreadable",
                        (f"package metadata unreadable: {_error(exc)}"),
                        [],
                    )
            if limited is not None:
                return "zip", {"members": len(infos)}, "limited", limited, []
            if kind is not None:
                return kind, {"parts": len(infos)}, "read", None, []
            rows: list[AdoptionFile] = []
            seen: dict[str, str] = {}
            for info in sorted(infos, key=lambda i: (i.filename, i.header_offset)):
                if info.is_dir():
                    continue
                rows += self._member(archive, info, path, depth, seen)
            members = sum(1 for i in infos if not i.is_dir())
            return "zip", {"members": members}, "read", None, rows

    @staticmethod
    def member_label(stored: str) -> str:
        """A member's path inside its archive: separators normalised, empty, ``.`` and ``..``
        segments and leading slashes dropped, control characters replaced."""
        name = _CONTROL_CHARS.sub("?", stored.replace("\\", "/"))
        parts = [p for p in name.split("/") if p not in ("", ".", "..")]
        return "/".join(parts) or "unnamed"

    def _member(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        parent: str,
        depth: int,
        seen: dict[str, str],
    ) -> list[AdoptionFile]:
        stored = info.filename
        label = self.member_label(stored)
        member = member_path(parent, label)
        detail: dict[str, Any] = {"crc32": f"{info.CRC:08x}", "compressed": info.compress_size}
        if member in seen:  # duplicate or colliding names stay distinct rows (F2)
            first, n = member, 2
            while f"{first}#{n}" in seen:
                n += 1
            member = f"{first}#{n}"
            detail["duplicate_of"] = first
        seen[member] = stored
        if label != stored:
            detail["name_as_stored"] = stored[:300]
        values: dict[str, Any] = {
            "parent": parent,
            "depth": depth,
            "entry": "member",
            "size": info.file_size,
            "detail": detail,
            "zone": zone_of(label, None, False),
        }
        if not label.lower().endswith(_NESTED):
            return [self._make(member, status="listed", **values)]
        if depth >= self.limits.max_depth:
            return [
                self._make(
                    member,
                    status="limited",
                    reason=f"max_depth: {depth} >= {self.limits.max_depth}",
                    **values,
                )
            ]
        if info.file_size > self.limits.max_member_bytes:
            return [
                self._make(
                    member,
                    status="limited",
                    reason=f"max_member_bytes: {info.file_size} > {self.limits.max_member_bytes}",
                    **values,
                )
            ]
        ratio = info.file_size // max(info.compress_size, 1)
        if ratio > self.limits.max_ratio and info.file_size > 1024:
            return [
                self._make(
                    member,
                    status="limited",
                    reason=f"max_ratio: {ratio} > {self.limits.max_ratio}",
                    **values,
                )
            ]
        try:
            with archive.open(info) as src:
                data = src.read(self.limits.max_member_bytes + 1)
        except Exception as exc:  # corrupt member stream
            return [
                self._make(
                    member,
                    status="unreadable",
                    reason=f"member read failed: {_error(exc)}",
                    **values,
                )
            ]
        if len(data) > self.limits.max_member_bytes:
            return [
                self._make(
                    member,
                    status="limited",
                    reason="max_member_bytes: actual size over the limit",
                    **values,
                )
            ]
        sha = hashlib.sha256(data).hexdigest()
        if not data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
            return [
                self._make(
                    member,
                    status="read",
                    sha256=sha,
                    container=sniff(data[:SNIFF_BYTES], label, len(data)),
                    **values,
                )
            ]
        try:
            container, inner, status, reason, rows = self._list_zip(
                io.BytesIO(data), member, depth=depth + 1
            )
        except Exception as exc:
            container, inner, status, reason, rows = (
                "zip",
                None,
                "unreadable",
                f"zip listing failed: {_error(exc)}",
                [],
            )
        values["detail"] = {**detail, **(inner or {})}
        values["zone"] = zone_of(label, container, False)
        head = self._make(
            member, status=status, reason=reason, sha256=sha, container=container, **values
        )
        return [head, *rows]

    # summary ----------------------------------------------------------------------------

    def _summary(self, seconds: float) -> ScanSummary:
        c = _Counts()
        on_disk = members = size = 0
        for row in self.rows:
            c.status[row.status] += 1
            c.zone[row.zone] += 1
            if row.container is not None:
                c.container[row.container] += 1
            if row.entry == "member":
                members += 1
            elif row.entry == "file":
                on_disk += 1
                size += row.size or 0
        return ScanSummary(
            entries=len(self.rows),
            files_on_disk=on_disk,
            members=members,
            bytes_on_disk=size,
            by_status=dict(sorted(c.status.items())),  # type: ignore[arg-type]
            by_container=dict(sorted(c.container.items())),  # type: ignore[arg-type]
            by_zone=dict(sorted(c.zone.items())),  # type: ignore[arg-type]
            unreadable=c.status["unreadable"],
            limited=c.status["limited"],
            seconds=round(seconds, 3),
        )


def exclusion_prune(config: ScanConfig) -> Prune:
    """The same folder pruning a scan's inventory uses, for its root digests."""
    globs = Globs.of(config.exclusions)
    return lambda rel, st: "excluded" if globs.match(rel, folder=True) else None


def inventory(
    root: Path,
    scan_gen: int,
    config: ScanConfig | None = None,
    *,
    stat_of: StatProvider = entry_stat,
    progress: Callable[[int, int], None] | None = None,
) -> Result:
    return Inventory(
        root, scan_gen, config or ScanConfig(), stat_of=stat_of, progress=progress
    ).run()
