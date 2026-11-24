"""Local filesystem paths in diagnostic free text and path-valued fields (decision 0012 items
43 and 47; decision 0019, CP10.3 review R2-L1 and integration review INT-CP10-M1/L1).

Shared by the hub's JSON redaction (:mod:`prometheos.hub.redaction`, which applies the
free-text rule to error bodies and ``error`` fields) and by operation runs
(:mod:`prometheos.app.runs`, whose text and views agents also read through MCP, where no hub
middleware runs).

Free text (:class:`FreeText`): each path that starts at a known local root becomes the
root's label (``<PrometheOS home>``, ``<user home>``, ...), and any other Windows drive or UNC
path becomes ``<local path>``. The whole path is consumed, spaces included: up to the closing
quote when the path is quoted (exception messages quote paths), otherwise to the end of the
line (pre-check P2-L2). With ``posix=True`` (run text) an absolute POSIX-style path of at
least two segments is a path too: MSYS (``/c/Users/...``), WSL (``/mnt/c/...``) and other
absolute paths, except API route text (``/api/...``) and URLs. The hub's error bodies keep
the rule without it (their roots include the research roots, and routes appear in them).

Path-valued fields (:func:`is_path_key`): a field whose name is path-like holds a path, so an
absolute value there is never shown as it is.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path

ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
LOCAL_LABEL = "<local path>"
# Where a Windows drive or UNC path starts inside free text.
_WINDOWS_START = r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\(?=[^\\\s]))"
# Where an absolute POSIX-style path of two or more segments starts (posix=True only): not
# inside a word, a URL (``://``, ``host/x``), a relative path (``./``, ``~/``) or after a
# backslash, and not an API route.
_POSIX_START = r"(?<![\w:/.~\\])/(?!api/)(?=[^\s/'\"]+/)"
# The rest of a path: up to its closing quote when quoted, otherwise to the end of the line.
_TAIL = r"(?(q)[^\n]*?(?=(?P=q)|\n|$)|[^\n]*)"
CASE_INSENSITIVE = os.name == "nt"

Roots = list[tuple[str, str]]  # (absolute folder, label)

# Field names that hold a path. ``archive`` (a local release zip) and ``paths`` were added
# for run parameters (integration review INT-CP10-M1).
PATH_KEYS = frozenset(
    {
        "path", "paths", "lock_path", "store", "previous_store", "home", "work",
        "research_root", "location", "destination", "target", "into", "created", "adapted",
        "file", "folder", "directory", "root", "written", "archive",
    }
)  # fmt: skip
PATH_SUFFIXES = ("_path", "_dir", "_root", "_store", "_file", "_folder", "_directory")


def is_path_key(key: str) -> bool:
    return key in PATH_KEYS or key.endswith(PATH_SUFFIXES)


def _variants(folder: str) -> set[str]:
    """Spellings of one folder that can appear in text: as given, with either separator, and
    with doubled backslashes (``repr`` of a path inside an exception message)."""
    out = {folder, folder.replace("\\", "/"), folder.replace("/", "\\")}
    out |= {v.replace("\\", "\\\\") for v in list(out) if "\\" in v}
    return {v.rstrip("\\/") for v in out if v.rstrip("\\/")}


def normalise_roots(pairs: Iterable[tuple[Path | str | None, str]]) -> Roots:
    """Absolute, de-duplicated roots, longest first (so the most specific label wins)."""
    seen: dict[str, str] = {}
    for folder, label in pairs:
        if folder is None:
            continue
        for candidate in {str(folder), os.path.realpath(folder)}:
            text = candidate.rstrip("\\/")
            if text and ABSOLUTE.match(candidate) and text not in seen:
                seen[text] = label
    return sorted(seen.items(), key=lambda item: len(item[0]), reverse=True)


class FreeText:
    """The free-text rule over the roots ``roots()`` gives. ``roots`` is called at most once,
    and only when a non-empty text is scrubbed."""

    def __init__(self, roots: Callable[[], Roots], *, posix: bool = False) -> None:
        self._roots = roots
        self._posix = posix
        self._pattern: re.Pattern[str] | None = None
        self._labels: dict[str, str] = {}

    def pattern(self) -> re.Pattern[str]:
        """One alternative per spelling of each root, longest first, then any Windows drive
        or UNC start (group ``w``) and, with ``posix``, any absolute POSIX-style start (group
        ``p``). An optional opening quote (group ``q``) decides where the path ends: at the
        matching quote, otherwise at the end of the line."""
        if self._pattern is None:
            spelled: dict[str, str] = {}
            for folder, label in normalise_roots(self._roots()):
                for variant in _variants(folder):
                    spelled.setdefault(variant.casefold() if CASE_INSENSITIVE else variant, label)
            parts = []
            for index, variant in enumerate(sorted(spelled, key=len, reverse=True)):
                self._labels[f"r{index}"] = spelled[variant]
                parts.append(f"(?P<r{index}>{re.escape(variant)})")
            roots_alt = "|".join(parts) or r"(?!x)x"
            posix_alt = f"|(?P<p>{_POSIX_START})" if self._posix else ""
            flags = re.IGNORECASE if CASE_INSENSITIVE else 0
            self._pattern = re.compile(
                rf"(?P<q>['\"])?(?:(?:{roots_alt})(?=$|[\\/'\"\s:;,)\]>])"
                rf"|(?P<w>{_WINDOWS_START}){posix_alt}){_TAIL}",
                flags,
            )
        return self._pattern

    def scrub(self, text: str) -> str:
        if not text:
            return text

        def label(match: re.Match[str]) -> str:
            shown = self._labels.get(match.lastgroup or "", LOCAL_LABEL)
            return (match.group("q") or "") + shown

        return self.pattern().sub(label, text)

    def path(self, value: str) -> str:
        """A value known to be a path (a path-like field): its label, never the value. An
        absolute path the free-text rule does not recognise is ``<local path>``."""
        if not ABSOLUTE.match(value):
            return value
        shown = self.scrub(value)
        return shown if shown != value else LOCAL_LABEL
