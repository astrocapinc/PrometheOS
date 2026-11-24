"""Where every number in a manuscript comes from (decision 0111, slice A of the paper pass).

A pure read over recorded versions. The manuscript is the paper's newest visible manuscript
version; the files it ``\\input``s, ``\\include``s or ``\\subfile``s are the newest recorded,
visible versions of the same research-root paths. Every number written in the body gets one
kind:

- ``macro``: a use of a project macro (any defining form: ``\\newcommand`` and its variants,
  ``\\def`` and its variants, the ``DocumentCommand`` forms, ``\\let``) defined in a *generated*
  file: a file a recorded run wrote, whose header says it is generated, or that sits
  in a ``generated/`` folder (the KERS convention: ``make_report_data.m`` writes
  ``report/generated/macros.tex`` and the ``.tex`` holds no typed result). Its trace is the
  defining file and line, the execution receipts that report writing that exact file version,
  whether any input of those runs has a newer recorded version since, and the claims that quote
  the definition line with their derived status;
- ``generated``: a number in a generated file that is not a definition (a generated table);
- ``hand_macro``: a use of a numeric macro defined anywhere else (the preamble, a hand-written
  definitions file). A ``\\newcommand`` does not make a typed number traced (review R1-F1);
- ``claimed``: a number in prose that is exactly one of a visible claim's bound quantities: same
  value, and the unit next as a whole word (a unit-less quantity only when no unit follows);
- ``typed``: any other number in prose, which nothing traces;
- not checked, but counted: ``formula`` (mathematics), ``reference`` (after a cross-reference or
  name word, the far end of such a range, an ordinal, the 0--100 metric), ``unit`` (inside a unit
  such as kWh/100 km) and ``year``.

Nothing here decides that a number is right: a traced number is one whose origin is recorded.
Macros whose value holds no number (``\\ArmKgWinner`` = "C1 third-pack (naive)") are text.
"""

from __future__ import annotations

import bisect
import posixpath
import re
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

from prometheos.app import quantities
from prometheos.app.apply import read_bytes, text_lines
from prometheos.app.papers import _paper, latest_version
from prometheos.app.reads import Caller, current_record, project_seq

MAX_FILES = 40
MAX_ROWS = 300
CONTEXT = 70
RULE = (
    "Every number in the manuscript body is a macro from a generated file traced to its file, run "
    "and claims; a number in a generated file; a claim's bound quantity; or typed by hand, "
    "directly or through a hand-written macro. Numbers in formulas, names, cross-references, "
    "units and years are counted but not checked."
)
LIMITATIONS = [
    "Reads recorded versions only: a file changed on disk since its last import is not seen "
    "until it is imported again.",
    "A traced number has a recorded origin; this read does not decide that it is correct.",
    "Numbers in formulas, names, cross-references, units and years are counted but not checked.",
    "A file included more than once is read once.",
]

# Every TeX/LaTeX form that (re)defines a macro (review R3-G1): each pattern names the macro in
# group "n" and its value in group "v" (for \let, the macro it copies).
_VALUE = r"\{(?P<v>(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}"
_DEFINES = [
    re.compile(
        r"\\(?:(?:re)?newcommand|providecommand|DeclareRobustCommand)\*?\s*\{?\\(?P<n>[A-Za-z]+)\}?"
        r"\s*(?:\[\d\])?\s*(?:\[[^\]]*\])?\s*" + _VALUE
    ),
    re.compile(r"\\[gex]?def\s*\\(?P<n>[A-Za-z]+)\s*(?:#\d)*\s*" + _VALUE),
    re.compile(
        r"\\(?:New|Renew|Declare|Provide)DocumentCommand\s*\{?\\(?P<n>[A-Za-z]+)\}?\s*"
        r"\{[^{}]*\}\s*" + _VALUE
    ),
    re.compile(r"\\let\s*\\(?P<n>[A-Za-z]+)\s*=?\s*(?P<v>\\[A-Za-z]+)"),
]
_INPUT = re.compile(r"\\(?:input|include|subfile)\s*\{([^{}]+)\}")
# A comment starts at a % preceded by an even number of backslashes (\\% is a line break, then a
# comment; \% is a percent sign).
_COMMENT = re.compile(r"(?<!\\)((?:\\\\)*)(%.*)")
_USE = re.compile(r"\\([A-Za-z]+)")
# Arguments that hold labels, keys, paths, lengths or layout, never results.
_MASKED = re.compile(
    r"\\(?:label|ref|eqref|cref|Cref|autoref|pageref|nameref|cite[A-Za-z]*|input|include|"
    r"includegraphics|subfile|usepackage|documentclass|bibliography|bibliographystyle|url|href|"
    r"[hv]space|setlength|addtolength|setcounter|addtocounter|definecolor|color|textcolor|"
    r"rule|scalebox|resizebox|raisebox|graphicspath|vskip|hskip|linespread|multicolumn|"
    r"multirow|cline|cmidrule|begin|end)\*?(?:\s*\[[^\]]*\])*(?:\s*\{(?:[^{}]|\{[^{}]*\})*\})?"
    r"(?:\s*\{(?:[^{}]|\{[^{}]*\})*\})?"
)
_OPTIONAL = re.compile(r"(?:\\[A-Za-z]+|\\\\)\*?\s*\[[^\]]*\]")
_LENGTH = re.compile(
    r"\d*\.?\d+\s*(?:\\(?:text|line|column|paper)(?:width|height)|pt|em|ex|cm|mm|bp)\b"
)
_MATH_ENVS = "equation|align|gather|multline|eqnarray|math|displaymath|flalign|alignat"
_MATH_OPEN = re.compile(rf"\$\$|(?<!\\)\$|\\\(|\\\[|\\begin\{{({_MATH_ENVS})(\*?)\}}")
# A word before the number that makes it a name or a cross-reference (Section 3, Track-10,
# Model 3, \S7). The word must start a word: "temp." is not "p.", "three-phase" is not "phase".
_REFERENCE_WORD = re.compile(
    r"(?<![\w-])(?:section|sections|sec\.|secs\.|fig\.|figs\.|figure|figures|table|tables|tab\.|"
    r"eq\.|eqs\.|equation|equations|chapter|appendix|algorithm|step|phase|track|tracks|"
    r"item|stage|ref\.|refs\.|p\.|pp\.|page|pages|no\.|version|study|adr|paper|model)"
    r"(?:[\s~-]|\\,)*(?:\\[A-Za-z]+\s*\{?|\(|\{)?$|(?-i:\\S)(?:[\s~]|\\,)*$"
    # A name glued by a hyphen (plan-Phase-6, Track-10) even after a hyphen.
    r"|(?<![A-Za-z])(?:phase|track|stage|step|model|version)-$",
    re.IGNORECASE,
)
# A file a generator wrote: its numbers are traced to the file and the run that wrote it.
_GENERATED = re.compile(r"auto-?generated|generated by|do not edit", re.IGNORECASE)
# A digit group joined in LaTeX (200{,}000, 1\,240) is one number.
_GROUP_JOIN = re.compile(r"\{,\}|\\,")
# Inside a unit: a letter, then "/" (kWh/100 km); 1/2 and 25/75 are numbers.
_UNIT_BEFORE = re.compile(r"[A-Za-z]/(?:\s|~|\\,)*$")
_METRIC = re.compile(r"(?<![\d.])0\s*-{1,3}\s*100\b")
_SPACING = re.compile(r"(?:\s|~|\\[,;:! ]|\\thinspace|\{\})*")
# A unit after a four-digit number makes it a quantity, not a year (2000 rpm).
_UNIT_AFTER = re.compile(
    r"(?:\\,|~|\s)*(?:rpm|r/min|[kMG]?W|[kMG]?Wh|[kM]?J|N|Nm|[kM]?V|m?A|Ah|[kM]?Hz|s|ms|min|h|"
    r"km|m|mm|kg|t|g|K|bar|[kM]?Pa|rad|\\%|%)(?![A-Za-z])"
)


class _Number(NamedTuple):
    start: int
    end: int
    text: str
    value: str


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _blank_text(part: str) -> str:
    return re.sub(r"[^\n]", " ", part)


def _strip_comments(text: str) -> str:
    def cut(m: re.Match[str]) -> str:
        return m.group(1) + " " * len(m.group(2))

    return "\n".join(_COMMENT.sub(cut, line, count=1) for line in text.split("\n"))


def _definitions(text: str) -> tuple[dict[str, tuple[str, int]], list[tuple[int, int]]]:
    """Macro name -> (value, 1-based line of its last definition in this file), and the
    definitions' spans. A later definition of a name replaces an earlier one, as in TeX."""
    stripped = _strip_comments(text)
    found = sorted(
        (m for pattern in _DEFINES for m in pattern.finditer(stripped)), key=lambda m: m.start()
    )
    out: dict[str, tuple[str, int]] = {}
    spans: list[tuple[int, int]] = []
    newlines = [i for i, ch in enumerate(text) if ch == "\n"]
    for m in found:
        if spans and m.start() < spans[-1][1]:
            continue  # inside an earlier definition's value
        out[m.group("n")] = (m.group("v").strip(), bisect.bisect_left(newlines, m.start()) + 1)
        spans.append((m.start(), m.end()))
    return out, spans


def _numeric(value: str) -> bool:
    """A macro value that writes a number (``-8.5``, ``1{,}325``, ``\\SI{425}{kJ}``, ``$4.2$``);
    a value of names only (``C1 third-pack (naive)``) is text (review R1-F2). Parameters
    (``#1``) are not numbers (review R2-F3)."""
    # Lengths (0.97\textwidth) are layout, not results.
    written = re.sub(r"#\d", "  ", _USE.sub(_blank, _LENGTH.sub(_blank, value)))
    return any(t.kind == "number" for t in quantities.tokens(written))


def _math_spans(text: str) -> list[tuple[int, int]]:
    """Mathematics spans, found left to right. An opener with no closer ends the search for that
    closer (review R1-A1: no rescan of the rest of the text per unmatched opener)."""
    spans: list[tuple[int, int]] = []
    no_closer: set[str] = set()
    pos = 0
    while True:
        m = _MATH_OPEN.search(text, pos)
        if m is None:
            return spans
        opener = m.group(0)
        if m.group(1):
            closer = f"\\end{{{m.group(1)}{m.group(2)}}}"
        else:
            closer = {"$$": "$$", "$": "$", "\\(": "\\)", "\\[": "\\]"}[opener]
        end = -1 if closer in no_closer else text.find(closer, m.end())
        while closer == "$" and end > 0 and text[end - 1] == "\\":
            end = text.find(closer, end + 1)
        if end < 0:
            no_closer.add(closer)
            pos = m.end()
            continue
        spans.append((m.start(), end + len(closer)))
        pos = end + len(closer)


def _body(text: str, main: bool) -> str:
    """The scanned part of a file (blanked elsewhere). The main file's preamble and everything
    after ``\\end{document}`` are not scanned."""
    if not main:
        return text
    start = text.find("\\begin{document}")
    end = text.find("\\end{document}")
    begin = start + len("\\begin{document}") if start >= 0 else 0
    finish = end if end >= begin else len(text)
    return _blank_text(text[:begin]) + text[begin:finish] + _blank_text(text[finish:])


def _without(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with each (sorted, non-overlapping) span blanked, built once."""
    parts, pos = [], 0
    for a, b in spans:
        parts += [text[pos:a], _blank_text(text[a:b])]
        pos = b
    parts.append(text[pos:])
    return "".join(parts)


def _source_at(conn: sqlite3.Connection, caller: Caller, alias: str, path: str) -> str | None:
    for (source_id,) in conn.execute(
        "SELECT r.id FROM records r JOIN revisions v ON v.record_id = r.id AND v.rev = "
        "r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.kind = 'source' AND "
        "r.lifecycle = 'active' AND json_extract(b.encoded, '$.root_binding.alias') = ? AND "
        "json_extract(b.encoded, '$.root_binding.path') = ? ORDER BY r.created_seq DESC",
        (alias, path),
    ):
        record = current_record(conn, source_id)
        if record is not None and caller.may_see(record["sensitivity"]):
            return str(source_id)
    return None


class _Files:
    """The manuscript and the recorded files it pulls in, depth first, each read once."""

    def __init__(self, conn: sqlite3.Connection, store_dir: Path, caller: Caller) -> None:
        self.conn, self.store_dir, self.caller = conn, store_dir, caller
        self.files: list[dict[str, Any]] = []
        self.missing: list[str] = []
        self.skipped: list[str] = []
        self.texts: dict[str, str] = {}

    def add(self, version: dict[str, Any], alias: str | None, path: str | None, role: str) -> None:
        if any(f["version_id"] == version["id"] for f in self.files):
            return
        if len(self.files) >= MAX_FILES:
            if path is not None and path not in self.skipped:
                self.skipped.append(path)
            return
        data = read_bytes(self.conn, self.store_dir, version["body"])
        lines = text_lines(data) if data is not None else None
        entry = {
            "path": path,
            "root_alias": alias,
            "version_id": version["id"],
            "sha256": version["body"]["sha256"],
            "role": role,
            "readable": lines is not None,
        }
        self.files.append(entry)
        if lines is None:
            return
        text = "\n".join(lines)
        self.texts[version["id"]] = text
        if alias is None or path is None:
            return
        folder = posixpath.dirname(path)
        for m in _INPUT.finditer(_strip_comments(text)):
            name = m.group(1).strip()
            if not posixpath.splitext(name)[1]:
                name += ".tex"
            target = posixpath.normpath(posixpath.join(folder, name))
            source_id = _source_at(self.conn, self.caller, alias, target)
            found = latest_version(self.conn, self.caller, source_id) if source_id else None
            if found is None:
                if target not in self.missing:
                    self.missing.append(target)
                continue
            self.add(found, alias, target, "input")


def _runs_of(conn: sqlite3.Connection, store_dir: Path, caller: Caller) -> dict[str, list[Any]]:
    from prometheos.app.research_map import _runs

    return _runs(conn, store_dir, caller)


def _changed_inputs(
    conn: sqlite3.Connection, caller: Caller, receipt_version_id: str, store_dir: Path
) -> list[dict[str, Any]]:
    """A run's inputs that have a newer visible version than the one the run read."""
    from prometheos.app.receipts import parse
    from prometheos.app.research_map import _file

    version = current_record(conn, receipt_version_id)
    data = read_bytes(conn, store_dir, version["body"]) if version else None
    if data is None:
        return []
    out = []
    for artifact in parse(data).inputs:
        newest = latest_version(conn, caller, artifact.source_id)
        if newest is None or newest["id"] == artifact.version_id:
            continue
        seen = _file(conn, caller, newest["id"])
        out.append(
            {
                "path": seen["path"] if seen else None,
                "read_version_id": artifact.version_id,
                "newer_version_id": newest["id"],
            }
        )
    return out


def _claims_quoting(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller
) -> tuple[dict[tuple[str, int], list[str]], list[dict[str, Any]], dict[str, str]]:
    """(version, line) -> claims whose evidence quotes that line; every visible claim's bound
    quantities; claim -> derived status."""
    from prometheos.app import claim_status
    from prometheos.app.claim_status import _rows

    statuses = {s["claim_id"]: s["status"] for s in claim_status.statuses(conn, store_dir, caller)}
    by_line: dict[tuple[str, int], list[str]] = {}
    bound: list[dict[str, Any]] = []
    for claim in _rows(conn, "claim", caller):
        body = claim["body"]
        for ev in body.get("evidence") or []:
            locator = ev.get("locator") or {}
            if locator.get("type") != "text":
                continue
            for line in range(int(locator["start_line"]), int(locator["end_line"]) + 1):
                by_line.setdefault((ev["version_id"], line), []).append(claim["id"])
        for q in body.get("quantities") or []:
            bound.append({**q, "claim_id": claim["id"]})
    return by_line, bound, statuses


def _numbers_in(visible: str, original: str) -> list[_Number]:
    """The numbers of ``visible``. A digit group joined in LaTeX (``200{,}000``, ``1\\,240``) is
    one number, with the merged text, end and value; the value of a joined number is its digits
    without the joins, so ``1\\,240`` compares with a bound 1240 (review R1-F4)."""
    found = quantities.tokens(visible)
    out: list[_Number] = []
    i = 0
    while i < len(found):
        token = found[i]
        i += 1
        if token.kind != "number":
            continue
        text, end, value = token.text, token.end, token.text
        while i < len(found):
            nxt = found[i]
            gap = original[end : nxt.start]
            if not (_GROUP_JOIN.fullmatch(gap) and nxt.kind == "number" and len(nxt.text) == 3):
                break
            text, end, value = text + "," + nxt.text, nxt.end, value + nxt.text
            i += 1
        out.append(_Number(token.start, end, text, quantities.number_value(value)))
    return out


def _trace(
    conn: sqlite3.Connection, caller: Caller, store_dir: Path, runs: list[dict[str, Any]]
) -> dict[str, Any]:
    """A generated file's trace: the runs that report writing this exact version, whether
    their inputs changed since, and the state its numbers get (``run`` when a succeeded run
    still reads current inputs, ``run_inputs_changed`` when every succeeded run's inputs have a
    newer version, ``file_only`` when no succeeded run wrote it)."""
    found = [
        {
            "receipt_version_id": run["receipt_version_id"],
            "receipt_title": run["receipt_title"],
            "command": run["command"],
            "outcome": run["outcome"],
            "ended_at": run["ended_at"],
            "inputs_changed": _changed_inputs(conn, caller, run["receipt_version_id"], store_dir),
        }
        for run in runs
    ]
    good = [r for r in found if r["outcome"] == "succeeded"]
    if not good:
        state = "file_only"
    elif all(r["inputs_changed"] for r in good):
        state = "run_inputs_changed"
    else:
        state = "run"
    return {"state": state, "runs": found}


def _bound_claim(text: str, number: _Number, bound: list[dict[str, Any]]) -> str | None:
    """The claim whose bound quantity this prose number is: the same value, and the claim's unit
    next as a whole word; a unit-less quantity only when no unit follows (review R1-F4)."""
    rest = text[number.end : number.end + 80]  # a window, never the rest of the text (R1-A1)
    rest = rest[_SPACING.match(rest).end() :].replace("\\%", "%")  # type: ignore[union-attr]
    unit_follows = bool(rest) and (rest[0].isalpha() or rest[0] == "%")
    for q in bound:
        if quantities.number_value(q["decimal"]) != number.value:
            continue
        unit = str(q.get("unit") or "")
        if not unit:
            if not unit_follows:
                return str(q["claim_id"])
        elif rest.startswith(unit) and not rest[len(unit) : len(unit) + 1].isalpha():
            return str(q["claim_id"])
    return None


def _is_generated(f: dict[str, Any], text: str, runs: dict[str, list[Any]]) -> bool:
    """A recorded run wrote it, its header says so, or it sits in a generated/ folder (the KERS
    convention: make_report_data.m writes report/generated/)."""
    return bool(
        runs.get(f["version_id"])
        or _GENERATED.search("\n".join(text.split("\n")[:5]))
        or "generated" in str(f["path"] or "").lower().split("/")[:-1]
    )


def audit(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, paper_id: str
) -> dict[str, Any]:
    paper = _paper(conn, caller, paper_id)
    out: dict[str, Any] = {
        "paper_id": paper_id,
        "title": paper["body"]["title"],
        "project_seq": project_seq(conn),
        "generation": "deterministic",
        "rule": RULE,
        "limitations": LIMITATIONS,
    }
    source_id = paper["body"].get("manuscript_source_id")
    manuscript = latest_version(conn, caller, source_id) if source_id else None
    if manuscript is None:
        return out | {
            "manuscript": None,
            "headline": "No manuscript is recorded for this paper yet, so no number is checked.",
            "counts": {},
            "files": [],
            "inputs_not_recorded": [],
            "inputs_skipped": [],
            "macro_files": [],
            "macros": [],
            "numbers": [],
            "truncated": False,
        }
    source = current_record(conn, str(source_id)) or {}
    binding = source.get("body", {}).get("root_binding") or {}
    files = _Files(conn, store_dir, caller)
    files.add(manuscript, binding.get("alias"), binding.get("path"), "manuscript")

    quoting, bound, statuses = _claims_quoting(conn, store_dir, caller)
    runs = _runs_of(conn, store_dir, caller)
    defined: dict[str, dict[str, Any]] = {}
    per_file: dict[str, dict[str, tuple[str, int]]] = {}
    def_spans: dict[str, list[tuple[int, int]]] = {}
    for f in files.files:
        text = files.texts.get(f["version_id"])
        if text is None:
            continue
        found, spans = _definitions(text)
        per_file[f["version_id"]], def_spans[f["version_id"]] = found, spans
        generated = f["role"] != "manuscript" and _is_generated(f, text, runs)
        code_lines = [ln for ln in _strip_comments(text).split("\n") if ln.strip()]
        if generated:
            f["role"] = "macros" if found and len(found) >= len(code_lines) * 0.8 else "generated"
        for name, (written, line) in found.items():
            # Any hand-written definition of a name wins over a generated one: a \renewcommand
            # in the preamble must never read as traced (review R2-F1).
            # Among generated definitions the last one wins, as TeX prints it (review R3-G2).
            prior = defined.get(name)
            if prior is None or not generated or prior["generated"]:
                defined[name] = {"value": written, "version_id": f["version_id"], "line": line,
                                 "path": f["path"], "generated": generated}  # fmt: skip
    traces = {
        f["version_id"]: _trace(conn, caller, store_dir, runs.get(f["version_id"], []))
        for f in files.files
        if f["role"] in ("macros", "generated")
    }
    traced = {"run": 0, "run_inputs_changed": 0, "file_only": 0}

    counts = dict.fromkeys(
        ("macro", "generated", "hand_macro", "claimed", "typed", "formula", "reference", "unit",
         "year", "text_macro"),
        0,
    )  # fmt: skip
    numbers: list[dict[str, Any]] = []
    dropped = False
    used: dict[str, dict[str, Any]] = {}
    for f in files.files:
        text = files.texts.get(f["version_id"])
        if text is None:
            continue
        # Definitions are not prose: their values count where the macro is used (R1-A4).
        scan = _without(_body(_strip_comments(text), f["role"] == "manuscript"),
                        def_spans[f["version_id"]])  # fmt: skip
        original = scan
        newlines = [i for i, ch in enumerate(scan) if ch == "\n"]
        lines = text.split("\n")
        for m in _USE.finditer(scan):
            name = m.group(1)
            if name in defined:
                line = bisect.bisect_left(newlines, m.start()) + 1
                entry = used.setdefault(
                    name, {"uses": 0, "first": {"path": f["path"], "line": line}}
                )
                entry["uses"] += 1
        if f["role"] in ("macros", "generated"):
            # Everything a generator wrote outside its definitions is its output, mathematics
            # included (review R2-F2: a table row in a mostly-definitions file counts too).
            state = traces[f["version_id"]]["state"]
            written = _USE.sub(_blank, _MASKED.sub(_blank, scan))
            n = len(_numbers_in(written, original))
            counts["generated"] += n
            traced[state] += n
            continue
        maths = _math_spans(scan)
        for a, b in maths:
            counts["formula"] += len(_numbers_in(_USE.sub(_blank, scan[a:b]), original[a:b]))
        scan = _without(scan, maths)
        scan = _MASKED.sub(_blank, scan)
        scan = _OPTIONAL.sub(_blank, scan)
        scan = _LENGTH.sub(_blank, scan)
        scan = _USE.sub(_blank, scan)  # macro and command names are not numbers
        # LaTeX dashes (0--100) are ranges, not minus signs.
        visible = re.sub(r"-{2,3}", lambda m: " " * len(m.group(0)), scan)
        metric = {m.start() for m in _METRIC.finditer(original)} | {
            m.end() - 3 for m in _METRIC.finditer(original)
        }
        reference_end = -1
        for number in _numbers_in(visible, original):
            index = bisect.bisect_left(newlines, number.start)
            line = index + 1
            start = newlines[index - 1] + 1 if index else 0
            before = original[max(start, number.start - 40) : number.start]
            after = original[number.end : number.end + 12]
            digits = number.text.lstrip("+-\u2212").replace(",", "")
            ordinal = re.match(r"(?:st|nd|rd|th)\b", after)
            # The far end of a referenced range (Phase 4--5, Sections 2--3) is a reference too.
            ranged = reference_end >= 0 and re.fullmatch(
                r"\s*-{1,3}\s*", original[reference_end : number.start]
            )
            claim = None
            if _REFERENCE_WORD.search(before) or number.start in metric or ordinal or ranged:
                kind = "reference"
                reference_end = number.end
            elif _UNIT_BEFORE.search(before):
                kind = "unit"
            elif (
                len(digits) == 4
                and digits.isdigit()
                and 1900 <= int(digits) <= 2099
                and not _UNIT_AFTER.match(after)
            ):
                kind = "year"
            else:
                claim = _bound_claim(visible, number, bound)
                kind = "claimed" if claim else "typed"
            counts[kind] += 1
            if kind in ("typed", "claimed"):
                if len(numbers) >= MAX_ROWS:
                    dropped = True
                    continue
                row = lines[line - 1]
                col = number.start - start
                numbers.append(
                    {
                        "kind": kind,
                        "path": f["path"],
                        "line": line,
                        "text": number.text,
                        "context": row[max(0, col - CONTEXT) : col + len(number.text) + CONTEXT],
                        "claim_id": claim,
                        "claim_status": statuses.get(claim) if claim else None,
                    }
                )

    macro_files = [
        {
            "path": f["path"],
            "version_id": f["version_id"],
            "sha256": f["sha256"],
            "role": f["role"],
            "defined": len(per_file.get(f["version_id"], {})),
            "used": sum(1 for d in used if defined[d]["version_id"] == f["version_id"]),
            "trace": traces[f["version_id"]]["state"],
            "runs": traces[f["version_id"]]["runs"],
        }
        for f in files.files
        if f["role"] in ("macros", "generated")
    ]

    macros: list[dict[str, Any]] = []
    for name, use in sorted(used.items()):
        d = defined[name]
        # An alias (\newcommand{\B}{\A}, \let\B\A) prints its target: one level is resolved,
        # and it is traced only if both definitions are generated (review R3-G3).
        alias = re.fullmatch(r"\\([A-Za-z]+)", d["value"])
        if alias and alias.group(1) in defined and alias.group(1) != name:
            target = defined[alias.group(1)]
            d = {**target, "generated": d["generated"] and target["generated"]}
        if not _numeric(d["value"]):
            counts["text_macro"] += use["uses"]
            continue
        if d["generated"]:
            counts["macro"] += use["uses"]
            state = traces[d["version_id"]]["state"]
            traced[state] += use["uses"]
        else:
            counts["hand_macro"] += use["uses"]
            state = "hand"
        claims = sorted(set(quoting.get((d["version_id"], d["line"]), [])))
        if len(macros) >= MAX_ROWS:
            dropped = True
            continue
        macros.append(
            {
                "name": name,
                "value": d["value"],
                "uses": use["uses"],
                "first_use": use["first"],
                "defined_at": {
                    "path": d["path"],
                    "version_id": d["version_id"],
                    "line": d["line"],
                },
                "trace": state,
                "claims": [{"claim_id": c, "status": statuses.get(c)} for c in claims],
            }
        )

    checked = sum(counts[k] for k in ("macro", "generated", "hand_macro", "claimed", "typed"))
    unreadable = [str(f["path"]) for f in files.files if not f["readable"]]
    headline = _headline(checked, traced, counts, files.missing, files.skipped, unreadable)
    return out | {
        "manuscript": {
            "version_id": manuscript["id"],
            "path": binding.get("path"),
            "sha256": manuscript["body"]["sha256"],
        },
        "headline": headline,
        "counts": counts | {"checked": checked} | {f"traced_{k}": v for k, v in traced.items()},
        "files": files.files,
        "inputs_not_recorded": files.missing,
        "inputs_skipped": files.skipped,
        "macro_files": macro_files,
        "macros": macros,
        "numbers": numbers,
        "truncated": dropped,
    }


def _headline(
    checked: int,
    traced: dict[str, int],
    counts: dict[str, int],
    missing: list[str],
    skipped: list[str],
    unreadable: list[str],
) -> str:
    if checked == 0:
        text = "No number in the manuscript body to check."
    else:
        parts = [f"{traced['run']} of {checked} numbers traced to a recorded run"]
        if traced["run_inputs_changed"]:
            parts.append(
                f"{traced['run_inputs_changed']} come from a run whose inputs changed since"
            )
        if traced["file_only"]:
            parts.append(f"{traced['file_only']} from a generated file no recorded run wrote")
        if counts["claimed"]:
            parts.append(f"{counts['claimed']} match a claim's bound quantity")
        hand = counts["typed"] + counts["hand_macro"]
        if not hand:
            parts.append("none typed by hand")
        elif counts["hand_macro"]:
            parts.append(
                f"{hand} typed by hand ({counts['hand_macro']} through a hand-written macro)"
            )
        else:
            parts.append(f"{hand} typed by hand")
        text = "; ".join(parts) + "."
    # Review R1-F5: every file whose numbers are unseen is named in the headline.
    if missing:
        text += f" {len(missing)} included file(s) are not recorded, so their numbers are unseen."
    if skipped:
        text += f" {len(skipped)} included file(s) were past the {MAX_FILES}-file limit, unread."
    if unreadable:
        text += f" {len(unreadable)} file(s) are not readable text, so their numbers are unseen."
    return text
