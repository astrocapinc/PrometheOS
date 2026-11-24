"""Local filesystem paths never reach the browser (decision 0006 item 19; integration
reviews CP5 INT1-M3 and INT2-M2, decision 0012 items 43, 46 and 47).

The hub applies :class:`Redactor` to every JSON body it sends, success or error, in one
place (the hub middleware). Three rules, from structural to textual:

1. A field whose *name* is path-like (:data:`PATH_KEYS`, or a name ending in one of
   :data:`PATH_SUFFIXES`) is dropped when its value is an absolute path; a list under such a
   name loses its absolute entries. Relative paths are kept: they are how research files and
   workspace files are cited. A field whose name is URL-like (``url`` or ``*_url``) and
   whose value is a local path or a ``file:`` URL (a Git remote on this computer) is shown
   as ``<local path>``; network URLs are kept (CP9 review INT2-F5).
2. Researcher content is never rewritten in a success reply. Note bodies, workspace file
   text, LaTeX, transcripts and every other field whose name is not path- or URL-like pass
   unchanged, even when a value happens to be a local path, because the hub writes such
   values back (the editor saves the text it received; revises send whole bodies). Pre-check
   P2-L1 (decision 0012 item 46) withdrew an earlier whole-value rule for this reason.
3. Free text is rewritten only where it is diagnostic: every string of an error body (status
   400 or more) and every ``error`` field. A value that is wholly a path under a folder
   PrometheOS manages (its home, the project store, an overridden engine or models folder)
   becomes a label such as ``<PrometheOS home>/tools/x``. Otherwise each path that starts at
   a known local root (those folders, the temporary folder, the user's profile folder, the
   project's research roots) becomes the root's label, and any other Windows drive or UNC
   path becomes ``<local path>``. The whole path is consumed, spaces included: up to the
   closing quote when the path is quoted (exception messages quote paths), otherwise to the
   end of the line (pre-check P2-L2). Error bodies are never written back as content. The
   free-text part of this rule is :mod:`prometheos.domain.redact`, shared with run text
   (decision 0019, CP10.3 review R2-L1).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from prometheos.domain.redact import (
    ABSOLUTE,
    CASE_INSENSITIVE,
    LOCAL_LABEL,
    PATH_KEYS,
    PATH_SUFFIXES,
    FreeText,
    Roots,
    is_path_key,
    normalise_roots,
)

__all__ = [
    "ABSOLUTE",
    "DIAGNOSTIC_KEYS",
    "LOCAL_LABEL",
    "PATH_KEYS",
    "PATH_SUFFIXES",
    "Redactor",
    "Roots",
    "is_local_url",
    "is_path_key",
    "normalise_roots",
]

DIAGNOSTIC_KEYS = frozenset({"error"})


def is_local_url(key: str, value: Any) -> bool:
    """A URL-like field naming something on this computer: an absolute path (a local Git
    remote is configured as one) or a ``file:`` URL."""
    return (
        (key == "url" or key.endswith("_url"))
        and isinstance(value, str)
        and (bool(ABSOLUTE.match(value)) or value[:5].lower() == "file:")
    )


class Redactor:
    """``managed`` gives the PrometheOS-managed roots; ``extra`` gives the further roots of
    rule 3. Both are called only when a value actually needs them."""

    def __init__(self, managed: Callable[[], Roots], extra: Callable[[], Roots]) -> None:
        self._managed = managed
        self._extra = extra

    def body(self, value: Any, *, envelope: bool, relabel: bool = False) -> Any:
        """``relabel`` keeps the shape of a typed reply (MCP, decision 0022): an absolute path
        in a path-like field becomes its label instead of leaving the field out."""
        return _Pass(self._managed, self._extra, relabel).walk(value, scrub=envelope)


class _Pass:
    def __init__(
        self, managed: Callable[[], Roots], extra: Callable[[], Roots], relabel: bool = False
    ) -> None:
        self._relabel = relabel
        self._managed_source = managed
        self._managed_roots: Roots | None = None
        self._text = FreeText(lambda: [*self.managed(), *extra()])

    # ----- roots ------------------------------------------------------------------------

    def managed(self) -> Roots:
        if self._managed_roots is None:
            self._managed_roots = self._managed_source()
        return self._managed_roots

    # ----- rules ------------------------------------------------------------------------

    def _managed_label(self, text: str) -> str | None:
        """``text`` is wholly a path at or under a managed root (rule 3, local URLs)."""
        if not ABSOLUTE.match(text) or "\n" in text:
            return None
        probe = text.replace("\\", "/")
        for folder, label in self.managed():
            base = folder.replace("\\", "/")
            a, b = (probe.casefold(), base.casefold()) if CASE_INSENSITIVE else (probe, base)
            if a == b:
                return label
            if a.startswith(b + "/"):
                return f"{label}/{probe[len(base) + 1 :]}"
        return None

    def _label(self, text: str) -> str:
        """A value known to be a path: its managed label, a root label, or ``<local path>``."""
        return self._managed_label(text) or self._text.path(text)

    def scrub(self, text: str) -> str:
        """Rule 3, for diagnostic free text (:class:`prometheos.domain.redact.FreeText`)."""
        return self._text.scrub(text)

    def walk(self, value: Any, *, scrub: bool) -> Any:
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for key, item in value.items():
                name = str(key)
                if is_local_url(name, item):
                    out[key] = self._managed_label(item) or LOCAL_LABEL
                    continue
                if is_path_key(name):
                    if isinstance(item, str) and ABSOLUTE.match(item):
                        if self._relabel:
                            out[key] = self._label(item)
                        continue  # rule 1
                    if isinstance(item, list):
                        item = [
                            self._label(v)
                            if self._relabel and isinstance(v, str) and ABSOLUTE.match(v)
                            else v
                            for v in item
                            if self._relabel or not (isinstance(v, str) and ABSOLUTE.match(v))
                        ]
                out[key] = self.walk(item, scrub=scrub or name in DIAGNOSTIC_KEYS)
            return out
        if isinstance(value, list):
            return [self.walk(item, scrub=scrub) for item in value]
        if isinstance(value, str) and scrub:  # rule 3; success content is never rewritten
            return self._managed_label(value) or self.scrub(value)
        return value
