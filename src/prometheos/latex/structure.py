"""Structure of a LaTeX workspace from its text: labels, references, citation keys,
BibTeX keys, outline and file inclusions, plus the static refusal scan run before every
compile (decision 0012 items 6 and 11).

Line-based and deliberately simple: a command and its arguments are read on one line,
comments are removed (an unescaped ``%`` to the end of the line), verbatim material is
not special-cased. The result is completion and navigation data, not a TeX parser; the
compile itself is the authority on what the document means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_ITEMS = 2000  # per category, so a pathological file cannot produce an unbounded reply

_COMMAND = re.compile(r"\\([A-Za-z@]+)(\*?)")
_BIB_ENTRY = re.compile(r"@\s*([A-Za-z]+)\s*[{(]\s*([^,\s{}()]+)\s*,")

REF_COMMANDS = frozenset(
    {"ref", "eqref", "pageref", "autoref", "cref", "Cref", "nameref", "vref", "labelcref"}
)
CITE_COMMANDS = frozenset(
    {
        "cite",
        "citep",
        "citet",
        "citealp",
        "citealt",
        "citeauthor",
        "citeyear",
        "citeyearpar",
        "parencite",
        "textcite",
        "autocite",
        "footcite",
        "fullcite",
        "smartcite",
        "nocite",
        "Cite",
        "Citep",
        "Citet",
    }
)
SECTION_LEVELS = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
# Commands whose (last) braced argument names a file the engine will read.
FILE_COMMANDS = frozenset(
    {
        "input",
        "include",
        "includeonly",
        "subfile",
        "includegraphics",
        "bibliography",
        "addbibresource",
        "InputIfFileExists",
        "IfFileExists",
        "lstinputlisting",
        "verbatiminput",
        "VerbatimInput",
        "includepdf",
        "import",
        "subimport",
        "inputfrom",
        "includefrom",
        "subinputfrom",
        "subincludefrom",
        "includesvg",
        "inputminted",
        "externaldocument",
        "usepackage",
        "RequirePackage",
        "documentclass",
        "LoadClass",
        "bibliographystyle",
        "openin",
        "openout",
        "pdffiledump",
        "filemodCmp",
    }
)
# Commands that run programs. Tectonic's --untrusted mode already disables \write18;
# the workspace refuses the source before the engine starts (defence in depth).
SHELL_COMMANDS = frozenset({"ShellEscape", "shellescape", "directlua", "luaexec"})
# \write18 is the control word \write followed by the stream number 18.
_WRITE18 = re.compile(r"\\write\s*18(?![0-9])")
# In TeX source a backslash always starts a control sequence, so a literal path outside
# the workspace starts with a drive letter, a slash or ~ (a macro argument is not a path).
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:|/|~)")


@dataclass(frozen=True)
class Located:
    key: str
    file: str
    line: int


def strip_comment(line: str) -> str:
    """Remove an unescaped ``%`` comment (``\\%`` is a literal percent sign)."""
    index = 0
    while True:
        index = line.find("%", index)
        if index < 0:
            return line
        backslashes = 0
        probe = index - 1
        while probe >= 0 and line[probe] == "\\":
            backslashes += 1
            probe -= 1
        if backslashes % 2 == 0:
            return line[:index]
        index += 1


def _group(text: str, start: int, open_char: str, close_char: str) -> tuple[str, int] | None:
    """The balanced group starting at ``start`` (which must hold ``open_char``)."""
    if start >= len(text) or text[start] != open_char:
        return None
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2  # an escaped character never opens or closes a group
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
        index += 1
    return None


def command_arguments(text: str, end: int, limit: int = 3) -> tuple[list[str], list[str]]:
    """Optional ``[...]`` and mandatory ``{...}`` arguments following a command."""
    optional: list[str] = []
    mandatory: list[str] = []
    position = end
    while len(optional) + len(mandatory) < limit + 2:
        while position < len(text) and text[position] in " \t":
            position += 1
        if position < len(text) and text[position] == "[":
            found = _group(text, position, "[", "]")
            if found is None:
                break
            optional.append(found[0])
            position = found[1]
        elif position < len(text) and text[position] == "{":
            found = _group(text, position, "{", "}")
            if found is None:
                break
            mandatory.append(found[0])
            position = found[1]
            if len(mandatory) >= limit:
                break
        else:
            break
    return optional, mandatory


def _keys(argument: str) -> list[str]:
    return [k.strip() for k in argument.split(",") if k.strip()]


def is_path_escape(argument: str) -> bool:
    """An absolute, drive, home or parent-directory path (outside the workspace)."""
    value = argument.strip().strip('"').strip()
    if not value:
        return False
    if _ABSOLUTE.match(value):
        return True
    parts = re.split(r"[\\/]", value)
    return ".." in parts


def analyze(files: dict[str, str]) -> dict[str, Any]:
    """Labels, references, citations, BibTeX keys, outline and inclusions.

    ``files`` maps workspace-relative paths to text. ``.bib`` files contribute BibTeX
    keys; every other file is read as LaTeX.
    """
    labels: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    cites: list[dict[str, Any]] = []
    bib: list[dict[str, Any]] = []
    outline: list[dict[str, Any]] = []
    includes: list[dict[str, Any]] = []
    for path in sorted(files):
        text = files[path]
        lines = text.split("\n")
        if path.lower().endswith(".bib"):
            for number, raw in enumerate(lines, start=1):
                for match in _BIB_ENTRY.finditer(raw):
                    entry_type = match.group(1).lower()
                    if entry_type in ("string", "comment", "preamble"):
                        continue
                    bib.append(
                        {"key": match.group(2), "type": entry_type, "file": path, "line": number}
                    )
            continue
        for number, raw in enumerate(lines, start=1):
            line = strip_comment(raw.rstrip("\r"))
            for match in _COMMAND.finditer(line):
                name = match.group(1)
                if (
                    name not in REF_COMMANDS
                    and name not in CITE_COMMANDS
                    and name not in SECTION_LEVELS
                    and name != "label"
                    and name not in FILE_COMMANDS
                ):
                    continue
                _optional, mandatory = command_arguments(line, match.end())
                if not mandatory:
                    continue
                where = {"file": path, "line": number}
                if name == "label":
                    labels.append({"key": mandatory[0].strip(), **where})
                elif name in REF_COMMANDS:
                    for key in _keys(mandatory[0]):
                        refs.append({"key": key, "command": name, **where})
                elif name in CITE_COMMANDS:
                    for key in _keys(mandatory[-1] if name != "nocite" else mandatory[0]):
                        cites.append({"key": key, "command": name, **where})
                elif name in SECTION_LEVELS:
                    outline.append(
                        {
                            "level": SECTION_LEVELS[name],
                            "command": name + match.group(2),
                            "title": " ".join(mandatory[0].split())[:300],
                            **where,
                        }
                    )
                elif name in (
                    "input",
                    "include",
                    "subfile",
                    "includegraphics",
                    "bibliography",
                    "addbibresource",
                ):
                    for target in _keys(mandatory[0]) if name == "bibliography" else mandatory[:1]:
                        includes.append({"command": name, "target": target.strip(), **where})
    defined = {item["key"] for item in labels}
    bib_keys = {item["key"] for item in bib}
    return {
        "labels": labels[:MAX_ITEMS],
        "refs": refs[:MAX_ITEMS],
        "cites": cites[:MAX_ITEMS],
        "bib_keys": bib[:MAX_ITEMS],
        "outline": outline[:MAX_ITEMS],
        "includes": includes[:MAX_ITEMS],
        "undefined_refs": sorted({r["key"] for r in refs} - defined)[:MAX_ITEMS],
        "undefined_cites": sorted({c["key"] for c in cites} - bib_keys)[:MAX_ITEMS],
        "truncated": any(len(x) > MAX_ITEMS for x in (labels, refs, cites, bib, outline, includes)),
    }


def scan_hostile(files: dict[str, str]) -> list[dict[str, Any]]:
    """Refusals found before compiling: shell escape and file paths outside the workspace.

    Only literal text can be checked here; a path assembled by macros is caught (or not)
    by the post-run audit (decision 0012 item 11). Each finding names file and line.
    """
    findings: list[dict[str, Any]] = []
    for path in sorted(files):
        if path.lower().endswith(".bib"):
            continue
        for number, raw in enumerate(files[path].split("\n"), start=1):
            line = strip_comment(raw.rstrip("\r"))
            if "\\" not in line:
                continue
            for _hit in _WRITE18.finditer(line):
                findings.append(
                    {
                        "code": "shell_escape",
                        "file": path,
                        "line": number,
                        "message": "\\write18 would run a program; shell escape is disabled "
                        "and this source is refused",
                    }
                )
            for match in _COMMAND.finditer(line):
                name = match.group(1)
                if name in SHELL_COMMANDS:
                    findings.append(
                        {
                            "code": "shell_escape",
                            "file": path,
                            "line": number,
                            "message": f"\\{name} would run a program; shell escape is disabled "
                            "and this source is refused",
                        }
                    )
                    continue
                if name not in FILE_COMMANDS:
                    continue
                _optional, mandatory = command_arguments(line, match.end())
                candidates = list(mandatory)
                if name in ("input", "include", "openin", "openout") and not mandatory:
                    # Primitive forms: \input file, \openin\handle=file
                    rest = line[match.end() :]
                    bare = re.match(r"\s*(?:\\[A-Za-z@]+\s*=?\s*)?([^\s{}\\%]+)", rest)
                    if bare:
                        candidates.append(bare.group(1))
                for argument in candidates:
                    for piece in _keys(argument) or [argument]:
                        if is_path_escape(piece):
                            findings.append(
                                {
                                    "code": "path_escape",
                                    "file": path,
                                    "line": number,
                                    "message": f"\\{name} names a file outside the workspace "
                                    f"({piece.strip()[:120]}); only workspace files can be read",
                                }
                            )
    return findings[:MAX_ITEMS]
