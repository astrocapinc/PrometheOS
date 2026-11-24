"""Compile diagnostics mapped to workspace file and line (decision 0012 item 8).

The stored compile log has two sections: Tectonic's own messages (``error: file:line:
message`` and ``warning: ...``, written by the engine's diagnostic channel) and the TeX
``.log`` file. Errors come from the engine section; LaTeX and package warnings ("on input
line N"), undefined references/citations and errors the engine section did not report
come from the TeX log, whose current file is tracked with the usual parenthesis stack.
Paths are workspace-relative; a message about a bundle file (a package) has no file.
"""

from __future__ import annotations

import re
from typing import Any

from prometheos.domain.canonical import loads_bounded

INPUTS_PREFIX = "inputs: "
ENGINE_HEADER = "== engine messages =="
LOG_HEADER = "== TeX log =="
MAX_DIAGNOSTICS = 500
MAX_MESSAGE = 500

_ENGINE = re.compile(r"^(error|warning|note): (.*)$")
_FILE_LINE = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+?):(?P<line>\d+): (?P<msg>.*)$")
_INPUT_LINE = re.compile(r"on input line (\d+)")
_LINES_RANGE = re.compile(r"at lines? (\d+)(?:--\d+)?")
_OPEN_FILE = re.compile(r"\(((?:[A-Za-z]:)?[^\s()\"{}<>\[\]]+)")
# Tectonic wraps a fatal TeX error in these generic messages; the located error says more.
_WRAPPERS = (
    "something bad happened inside XeTeX",
    "the XeTeX engine had an unrecoverable error",
    "halted on potentially-recoverable error",
)
_WARNING = re.compile(r"^(?:LaTeX|Package [A-Za-z0-9_.-]+|Class [A-Za-z0-9_.-]+) Warning: (.*)")


def workspace_path(name: str, staging_root: str | None) -> str | None:
    """Map an engine-reported file name to a workspace-relative path (or None)."""
    value = name.strip().strip('"').replace("\\", "/")
    if staging_root:
        root = staging_root.replace("\\", "/").rstrip("/") + "/"
        if value.lower().startswith(root.lower()):
            value = value[len(root) :]
    while value.startswith("./"):
        value = value[2:]
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return None
    if ".." in value.split("/"):
        return None
    if "." not in value.rsplit("/", 1)[-1]:
        value += ".tex"  # \input{sections/intro} reads sections/intro.tex
    return value


def _looks_like_file(name: str) -> bool:
    return "." in name or "/" in name or "\\" in name


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def split_log(text: str) -> tuple[str, str]:
    """(engine section, TeX log section) of a stored compile log."""
    _, _, rest = text.partition(ENGINE_HEADER)
    if not rest:
        return "", text
    engine_part, _, log_part = rest.partition(LOG_HEADER)
    return engine_part, log_part.split("\n", 1)[1] if "\n" in log_part else log_part


def parse_engine(text: str, staging_root: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        match = _ENGINE.match(raw)
        if match is None:
            if current is not None and raw.strip() and len(current["context"]) < 400:
                current["context"] = (current["context"] + " " + raw.strip()).strip()[:400]
            continue
        level, message = match.groups()
        current = None
        if level == "note" or any(w in message for w in _WRAPPERS):
            continue
        located = _FILE_LINE.match(message)
        file_name: str | None = None
        line: int | None = None
        if located:
            file_name = workspace_path(located.group("file"), staging_root)
            line = int(located.group("line"))
            message = located.group("msg")
        while message.startswith("! "):
            message = message[2:]
        current = {
            "severity": level,
            "file": file_name,
            "line": line if file_name is not None else None,
            "message": _clip(message),
            "origin": "engine",
            "context": "",
        }
        out.append(current)
    return out


def parse_tex_log(text: str, staging_root: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    stack: list[str | None] = []
    pending_error: dict[str, Any] | None = None
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        current = next((f for f in reversed(stack) if f is not None), None)
        warning = _WARNING.match(raw)
        if raw.startswith("! "):
            pending_error = {
                "severity": "error",
                "file": current,
                "line": None,
                "message": _clip(raw[2:]),
                "origin": "log",
                "context": "",
            }
            out.append(pending_error)
        elif pending_error is not None and re.match(r"^l\.(\d+)", raw):
            number = re.match(r"^l\.(\d+)", raw)
            assert number is not None
            pending_error["line"] = int(number.group(1)) if pending_error["file"] else None
            pending_error = None
        elif warning:
            message = warning.group(1)
            # Warnings wrap onto following lines until a blank line.
            follow = index + 1
            while follow < len(lines) and lines[follow].strip() and follow - index < 6:
                if lines[follow].startswith(("(", "!", "LaTeX", "Package", "Class")):
                    break
                message += " " + lines[follow].strip()
                follow += 1
            number = _INPUT_LINE.search(message)
            out.append(
                {
                    "severity": "warning",
                    "file": current if number else None,
                    "line": int(number.group(1)) if number and current else None,
                    "message": _clip(message),
                    "origin": "log",
                    "context": "",
                }
            )
        elif raw.startswith(("Overfull \\hbox", "Underfull \\hbox", "Overfull \\vbox")):
            number = _LINES_RANGE.search(raw)
            out.append(
                {
                    "severity": "info",
                    "file": current if number else None,
                    "line": int(number.group(1)) if number and current else None,
                    "message": _clip(raw),
                    "origin": "log",
                    "context": "",
                }
            )
        # Track the file stack after classifying the line (a line opening a file and
        # reporting on it at once is rare and reported against the outer file).
        position = 0
        while position < len(raw):
            char = raw[position]
            if char == "(":
                opened = _OPEN_FILE.match(raw, position)
                if opened and _looks_like_file(opened.group(1)):
                    stack.append(workspace_path(opened.group(1), staging_root))
                    position = opened.end()
                    continue
                stack.append(None)
            elif char == ")" and stack:
                stack.pop()
            position += 1
    return out


def known_inputs(log_text: str) -> set[str] | None:
    """The workspace files a stored compile log names in its header (``inputs: [...]``)."""
    for line in log_text.split("\n", 8)[:8]:
        if line.startswith(INPUTS_PREFIX):
            try:
                value = loads_bounded(line[len(INPUTS_PREFIX) :])
            except ValueError:
                return None
            return {str(v) for v in value} if isinstance(value, list) else None
    return None


def diagnostics(log_text: str, staging_root: str | None = None) -> list[dict[str, Any]]:
    """Structured diagnostics from a stored compile log, errors first.

    A file name that is not one of the compile's workspace inputs (a bundle package such
    as ``article.cls``) is reported without file and line."""
    engine_text, tex_log = split_log(log_text)
    engine = parse_engine(engine_text, staging_root)
    from_log = parse_tex_log(tex_log, staging_root)
    known = known_inputs(log_text)
    if known is not None:
        folded = {k.casefold(): k for k in known}
        for item in (*engine, *from_log):
            if item["file"] is not None:
                match = folded.get(str(item["file"]).casefold())
                item["file"] = match
                if match is None:
                    item["line"] = None
    engine_errors = {(d["file"], d["line"]) for d in engine if d["severity"] == "error"}
    merged = list(engine)
    seen = {(d["severity"], d["file"], d["line"], d["message"]) for d in engine}
    for item in from_log:
        if item["severity"] == "error" and (
            (item["file"], item["line"]) in engine_errors or engine_errors
        ):
            continue  # the engine already reported this run's errors with their location
        key = (item["severity"], item["file"], item["line"], item["message"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    order = {"error": 0, "warning": 1, "info": 2}
    merged.sort(key=lambda d: order.get(d["severity"], 3))
    return merged[:MAX_DIAGNOSTICS]


# ----- diagnostics of an unproven compile (decision 0012 item 11a) ---------------------------

WITHHELD = (
    "details withheld: the files this compile read could not be verified, so only "
    "recognised messages that quote workspace text are kept"
)
UNVERIFIED_LOG_NOTE = (
    "(the engine output and TeX log of this compile are not kept: its file reads could not "
    "be verified to stay inside the workspace)"
)
# Message templates whose fixed text is the engine's or LaTeX's own. A named group ``v``
# is variable text, kept only if it occurs verbatim in a workspace input.
_TEMPLATES = [
    re.compile(p)
    for p in (
        r"Undefined control sequence\.?",
        r"Emergency stop\.?",
        r"Missing \$ inserted\.?",
        r"Missing number, treated as zero\.?",
        r"Missing \{ inserted\.?",
        r"Missing \} inserted\.?",
        r"Extra \}, or forgotten \$\.?",
        r"Extra \}, or forgotten (?P<v>\\[A-Za-z@]+)\.?",
        r"Too many \}'s\.?",
        r"Misplaced alignment tab character &\.?",
        r"Extra alignment tab has been changed to \\cr\.?",
        r"Illegal unit of measure \(pt inserted\)\.?",
        r"There's no line here to end\.?",
        r"Paragraph ended before (?P<v>\\[A-Za-z@]+) was complete\.?",
        r"File ended while scanning use of (?P<v>\\[A-Za-z@]+)\.?",
        r"Use of (?P<v>\\[A-Za-z@]+) doesn't match its definition\.?",
        r"LaTeX Error: File `(?P<v>[^`']{1,200})' not found\.?",
        r"LaTeX Error: Environment (?P<v>[A-Za-z@*]{1,100}) undefined\.?",
        r"LaTeX Error: Missing \\begin\{document\}\.?",
        r"LaTeX Error: There's no line here to end\.?",
        r"LaTeX Error: \\begin\{(?P<v>[A-Za-z@*]{1,100})\} on input line \d+ ended by "
        r"\\end\{[A-Za-z@*]{1,100}\}\.?",
        r"Reference `(?P<v>[^`']{1,200})' on page \d+ undefined on input line \d+\.?",
        r"Citation `(?P<v>[^`']{1,200})' on page \d+ undefined on input line \d+\.?",
        r"There were undefined references\.?",
        r"Label\(s\) may have changed\. Rerun to get cross-references right\.?",
        r"compile stopped after \d+(?:\.\d+)? s \(time limit\)",
        r"compile stopped: output exceeded its size limit",
        r"compile stopped: the engine reached its memory limit",
    )
]


def _quotable(message: str, source_text: str) -> bool:
    for template in _TEMPLATES:
        match = template.fullmatch(message.strip())
        if match is None:
            continue
        value = match.groupdict().get("v")
        if value is None:
            return True
        variable = value.strip()
        stem = variable.rsplit(".", 1)[0] if "." in variable else variable
        return bool(variable) and (variable in source_text or bool(stem and stem in source_text))
    return False


def sanitize(
    found: list[dict[str, Any]], inputs: set[str], source_text: str
) -> list[dict[str, Any]]:
    """Diagnostics safe to keep from a compile whose reads are unproven: file only when it
    is a workspace input, message only when it is a recognised template quoting workspace
    text (otherwise a fixed notice), box/info messages dropped (they print typeset text)."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in found:
        if item["severity"] not in ("error", "warning"):
            continue
        file_name = item["file"] if item["file"] in inputs else None
        line = item["line"] if file_name is not None else None
        message = item["message"] if _quotable(item["message"], source_text) else WITHHELD
        key = (item["severity"], file_name, line, message)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "severity": item["severity"],
                "file": file_name,
                "line": line,
                "message": message,
                "origin": item["origin"],
                "context": "",
            }
        )
    return out[:MAX_DIAGNOSTICS]


def sanitized_log(header: list[str], found: list[dict[str, Any]]) -> str:
    """A stored compile log built only from the header and sanitized diagnostics, in the
    same layout :func:`diagnostics` parses."""
    lines = []
    for item in found:
        where = f"{item['file']}:{item['line']}: " if item["file"] and item["line"] else ""
        lines.append(f"{item['severity']}: {where}{item['message']}")
    return (
        "\n".join(header)
        + f"\n{ENGINE_HEADER}\n"
        + "\n".join(lines)
        + f"\n{LOG_HEADER}\n{UNVERIFIED_LOG_NOTE}\n"
    )
