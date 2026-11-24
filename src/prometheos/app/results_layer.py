"""The intelligence layer on results waiting for interpretation (decision 0110, L2).

The case: a finished run or a bare receipt waits for interpretation, and one of its outputs is a
newer version of a source whose older version holds a claim's bound quantity. PrometheOS drafts
the comparison beside the item: the expected value (the binding), the measured value (read by code
from the new version), and the absolute and relative difference. It never invents a tolerance;
the owner or the agent chooses one when recording the check, through the existing assessment
path. A draft writes nothing.

System 1 first:

- **Rules.** The binding's label is the text before its number on the old line (it must hold a
  letter, and name that line alone in the old version). The new version's one line that starts
  with the same label, followed by a number, is read with the quantities tokenizer, and the unit
  must be the binding's own (``check_token``; for a unitless binding, the same word after the
  number). A number that is not a plain decimal (``1,240``) is not guessed at.
- **The layer** (``results.draft_interpretation``) only when no line, or more than one, carries
  the label. The bundle offers the new version's lines that hold a readable number in the
  binding's unit; two different evaluated models must agree on one line id; code then reads the
  number on that line (a line with several such numbers is left to the reader).

Text versions only, at most ``MAX_BYTES``; a JSON or PDF binding is shown as not drafted. A
subject's sensitivity includes the file's (A07.4), so restricted inputs never reach a remote
executor (the run denies them), and a caller sees no draft over records it cannot see.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from prometheos.adoption import assist
from prometheos.app import quantities
from prometheos.app.apply import read_bytes, text_lines
from prometheos.app.claim_status import _rows
from prometheos.app.paper_rules import is_text_version
from prometheos.app.reads import Caller, current_record
from prometheos.domain.canonical import canonical_sha256, loads_internal
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.intelligence import results as ai_results
from prometheos.intelligence import tasks
from prometheos.intelligence.run import Subject
from prometheos.store.project import ProjectStore

TASK_KIND = tasks.DRAFT_INTERPRETATION.kind
MAX_LINES = 60
MAX_BYTES = 2_000_000  # a larger output is compared by hand (``not_text``)
LINE_CHARS = 200
OUT_OF_USE = frozenset({"superseded", "withdrawn"})
DECIMAL = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$")  # QuantityBinding.decimal
NEEDS_LAYER = frozenset({"label_not_found", "label_ambiguous"})


@dataclass(frozen=True)
class Case:
    """One bound quantity of one claim, against one newer output version of its source."""

    target_id: str
    claim: dict[str, Any]
    index: int
    binding: dict[str, Any]
    old: dict[str, Any]
    new: dict[str, Any]

    @property
    def subject(self) -> str:
        return f"{self.target_id}:{self.claim['id']}:{self.index}"

    def sensitivity(
        self, conn: sqlite3.Connection, store_dir: Path, roots: list[tuple[str, Path, str]] | None
    ) -> str:
        """The highest of the claim and of the file's sensitivity for both versions (A07.4,
        ``file_sensitivity``: its root, its versions and sources, every record citing them;
        review map-ai-spread-b R1-F1). Without the roots, or for a source bound to no root, the
        file reads as restricted."""
        from prometheos.app.intelligence import file_sensitivity

        levels = [str(self.claim["sensitivity"]), "restricted" if roots is None else "normal"]
        if roots is not None:
            source = current_record(conn, str(self.new["body"]["source_id"]))
            binding = ((source or {}).get("body") or {}).get("root_binding") or {}
            folder = next((f for alias, f, _ in roots if alias == binding.get("alias")), None)
            path = binding.get("path")
            absolute = folder / str(path) if folder is not None and path else store_dir / "unbound"
            levels += [file_sensitivity(conn, roots, absolute, str(v["body"]["sha256"]))
                       for v in (self.old, self.new)]  # fmt: skip
        return max(levels, key=lambda s: SENSITIVITY_ORDER[s])

    @property
    def digest(self) -> str:
        return canonical_sha256(
            {"target": self.target_id, "claim": [self.claim["id"], self.claim["rev"]],
             "index": self.index, "old": self.old["id"], "new": self.new["id"]}
        )  # fmt: skip


def _created_seq(conn: sqlite3.Connection, record_id: str) -> int:
    row = conn.execute("SELECT created_seq FROM records WHERE id = ?", (record_id,)).fetchone()
    return int(row[0]) if row else 0


def _visible(conn: sqlite3.Connection, caller: Caller, record_id: str) -> dict[str, Any] | None:
    record = current_record(conn, record_id)
    if record is None or record["lifecycle"] == "deleted" or record["body"].get("purged"):
        return None
    return record if caller.may_see(record["sensitivity"]) else None


def _outputs(
    conn: sqlite3.Connection, caller: Caller, item: dict[str, Any]
) -> list[dict[str, Any]]:
    """The output versions an awaiting item's receipts name, the latest per source."""
    by_source: dict[str, dict[str, Any]] = {}
    for receipt in item.get("receipts", []):
        version = _visible(conn, caller, receipt["version_id"])
        if version is None:
            continue
        row = conn.execute(
            "SELECT data FROM payloads WHERE sha256 = ?", (version["body"]["sha256"],)
        ).fetchone()
        payload = loads_internal(bytes(row["data"])) if row else {}
        for output in payload.get("outputs", []):
            found = _visible(conn, caller, str(output.get("version_id")))
            if found is not None and found["body"].get("source_id") == output.get("source_id"):
                by_source[str(output["source_id"])] = found
    return list(by_source.values())


def cases(
    conn: sqlite3.Connection, caller: Caller, awaiting: list[dict[str, Any]] | None = None
) -> list[Case]:
    """Every (awaiting item, claim quantity) pair whose item outputs a newer version of the
    quantity's source, among the records the caller can see (``awaiting``: the queue, when the
    caller already read it)."""
    from prometheos.app.continuity import awaiting_queue

    bound: dict[str, list[tuple[dict[str, Any], int, dict[str, Any]]]] = {}
    for claim in _rows(conn, "claim", caller):
        if claim["body"].get("state") in OUT_OF_USE:
            continue
        for index, binding in enumerate(claim["body"].get("quantities") or []):
            source_id = str(binding["source_ref"]["source_id"])
            bound.setdefault(source_id, []).append((claim, index, binding))
    if not bound:
        return []
    out = []
    for item in awaiting if awaiting is not None else awaiting_queue(conn, caller):
        for new in _outputs(conn, caller, item):
            for claim, index, binding in bound.get(str(new["body"]["source_id"]), []):
                old = _visible(conn, caller, str(binding["source_ref"]["version_id"]))
                if old is None or _created_seq(conn, old["id"]) >= _created_seq(conn, new["id"]):
                    continue
                out.append(Case(item["target_id"], claim, index, binding, old, new))
    return out


Lines = dict[str, list[str] | None]


def _lines(
    conn: sqlite3.Connection, store_dir: Path, version: dict[str, Any], cache: Lines
) -> list[str] | None:
    """A text version's lines, once per read (``cache``), and none above ``MAX_BYTES`` (every Now
    read drafts again; review map-ai-spread-b R1-F7)."""
    if version["id"] not in cache:
        body = version["body"]
        size = body.get("byte_length")
        data = (
            read_bytes(conn, store_dir, body)
            if is_text_version(body) and isinstance(size, int) and size <= MAX_BYTES
            else None
        )
        cache[version["id"]] = text_lines(data) if data is not None else None
    return cache[version["id"]]


def read_value(line: str, start: int, unit: str) -> tuple[str, str | None, int]:
    """(status, decimal, end) of the number token starting at ``start`` with the binding's unit:
    ``drafted``; ``unit_differs`` when another unit (or none) follows; ``unreadable_number`` for
    a written number that is not a plain decimal (``1,240``, ``3:4``); ``no_value``."""
    token = quantities.number_at(line, start)
    if token is None:
        return "no_value", None, start
    decimal = quantities.number_value(token.text)
    if not DECIMAL.fullmatch(decimal):
        return "unreadable_number", None, token.end
    for tail in quantities.unit_forms(unit):
        stop = token.end + len(tail)
        if line[token.end : stop] != tail:
            continue
        try:
            quantities.check_token(line, start, stop, {"decimal": decimal, "unit": unit}, "")
        except PrometheosError:
            continue
        return "drafted", decimal, stop
    return "unit_differs", None, token.end


UNIT_WORD = re.compile(r"[ \u00a0]?([^\W\d_][\w/^.\u00b7\u00b2\u00b3-]*|%)")


def unit_word(line: str, end: int) -> str:
    """The word right after a number (``kJ``, ``%``, ``runs``), or nothing: for a unitless
    binding, a value now followed by another word is another unit (review R1-F4)."""
    found = UNIT_WORD.match(line, end)
    return found.group(1) if found else ""


def label_of(line: str, start: int) -> str | None:
    """The text before the bound number, when it names something (holds a letter)."""
    label = line[:start].rstrip()
    return label if any(ch.isalpha() for ch in label) else None


def label_matches(label: str, lines: list[str]) -> list[tuple[int, int]]:
    """(1-based line, number start) of every line that starts with ``label`` and then, after
    optional spaces, a number token."""
    found = []
    for number, line in enumerate(lines, 1):
        if not line.startswith(label):
            continue
        rest = line[len(label) :]
        start = len(label) + len(rest) - len(rest.lstrip())
        if quantities.number_at(line, start) is not None:
            found.append((number, start))
    return found


def readings_on(line: str, unit: str) -> list[tuple[int, str, int]]:
    """(start, decimal, end) of every number on ``line`` that reads in the binding's unit."""
    out = []
    for token in quantities.tokens(line):
        if token.kind != "number":
            continue
        status, decimal, end = read_value(line, token.start, unit)
        if status == "drafted" and decimal is not None:
            out.append((token.start, decimal, end))
    return out


def offered_lines(lines: list[str], unit: str) -> list[dict[str, str]]:
    """The new version's lines that hold a readable number in the unit, the first ``MAX_LINES``."""
    out = []
    for number, line in enumerate(lines, 1):
        if readings_on(line, unit):
            out.append({"id": f"L{number}", "text": line[:LINE_CHARS]})
            if len(out) == MAX_LINES:
                break
    return out


def bundle(case: Case, old_line: str, offered: list[dict[str, str]]) -> assist.Bundle:
    task = tasks.DRAFT_INTERPRETATION
    binding = case.binding
    data = {
        "quantity": {
            "name": str(binding.get("name") or ""),
            "definition": str(binding.get("definition") or ""),
            "unit": str(binding["unit"]),
            "earlier_line": old_line[:LINE_CHARS],
        },
        "lines": offered,
        "line_ids": [line["id"] for line in offered],
    }
    return assist.Bundle(
        kind="draft_interpretation",
        subject=case.subject,
        subject_sha256=case.digest,
        input=data,
        input_sha256=canonical_sha256(data),
        template_sha256=task.template_sha256,
    )


def _difference(expected: str, measured: str) -> tuple[str, str | None]:
    """(measured - expected, exact) and the relative difference in percent of |expected| (four
    significant digits), or ``None`` when expected is zero. Never a verdict: no tolerance."""
    diff = Decimal(measured) - Decimal(expected)
    if Decimal(expected) == 0:
        return format(diff, "f"), None
    exact = diff * 100 / abs(Decimal(expected))
    with localcontext() as ctx:
        ctx.prec = 4
        percent = +exact  # rounded once, to four significant digits (review R1-F3)
    return format(diff, "f"), format(percent, "f")


def _pointer(binding: dict[str, Any]) -> tuple[int, int, int] | None:
    match = quantities.TEXT_POINTER.fullmatch(str(binding.get("source_pointer", "")))
    if match is None or binding["source_ref"]["locator"].get("type") != "text":
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


@dataclass
class _Read:
    status: str
    line: int | None = None
    start: int = 0
    end: int = 0
    decimal: str | None = None
    old_line: str = ""
    old_end: int = 0
    offered: tuple[dict[str, str], ...] = ()
    new_lines: tuple[str, ...] = ()


def _same_unit_word(unit: str, old_line: str, old_end: int, line: str, end: int) -> bool:
    return bool(unit) or unit_word(old_line, old_end) == unit_word(line, end)


def rule_reading(
    conn: sqlite3.Connection, store_dir: Path, case: Case, cache: Lines | None = None
) -> _Read:
    """The rules' reading of the case (no model). The label must name the bound line alone in
    the old version, and one line in the new (review map-ai-spread-b R1-F2)."""
    cache = {} if cache is None else cache
    where = _pointer(case.binding)
    if where is None:
        return _Read("not_text")
    old_lines = _lines(conn, store_dir, case.old, cache)
    new_lines = _lines(conn, store_dir, case.new, cache)
    if old_lines is None or new_lines is None or where[0] > len(old_lines):
        return _Read("not_text")
    old_line, old_end = old_lines[where[0] - 1], where[2]
    unit = str(case.binding["unit"])
    label = label_of(old_line, where[1])
    unique = label is not None and label_matches(label, old_lines) == [(where[0], where[1])]
    matches = label_matches(label, new_lines) if label is not None else []
    if unique and len(matches) == 1:
        number, start = matches[0]
        line = new_lines[number - 1]
        status, decimal, end = read_value(line, start, unit)
        if status == "drafted" and not _same_unit_word(unit, old_line, old_end, line, end):
            status, decimal = "unit_differs", None
        return _Read(status, number, start, end, decimal, old_line, old_end)
    status = (
        "label_ambiguous" if matches or (label is not None and not unique) else ("label_not_found")
    )
    offered = tuple(offered_lines(new_lines, unit))
    return _Read(status, old_line=old_line, old_end=old_end, offered=offered,
                 new_lines=tuple(new_lines))  # fmt: skip


def subjects(
    store: ProjectStore,
    caller: Caller,
    claim_ids: list[str] | None,
    roots: list[tuple[str, Path, str]] | None = None,
) -> list[Subject]:
    """The cases the rules could not read, each a layer subject (lines to choose from). Its
    sensitivity includes the file's (``roots``: ``app.intelligence.root_levels``; without them
    every subject reads as restricted and is never sent)."""
    wanted = None if claim_ids is None else set(claim_ids)
    conn = store.connect(readonly=True)
    try:
        found = []
        cache: Lines = {}
        for case in cases(conn, caller):
            if wanted is not None and case.claim["id"] not in wanted:
                continue
            reading = rule_reading(conn, store.directory, case, cache)
            if reading.status in NEEDS_LAYER and reading.offered:
                level = case.sensitivity(conn, store.directory, roots)
                found.append((case, bundle(case, reading.old_line, list(reading.offered)), level))
    finally:
        conn.close()

    def reread_for(subject: str) -> Any:
        def reread() -> str | None:
            again = store.connect(readonly=True)
            try:
                now = [c for c in cases(again, caller) if c.subject == subject]
            finally:
                again.close()
            return now[0].digest if now else None

        return reread

    return [
        Subject(
            bundle=built,
            current_digest=case.digest,
            sensitivity=level,
            refs=[case.claim["id"], case.old["id"], case.new["id"]],
            reread=reread_for(case.subject),
        )
        for case, built, level in found
    ]


def _layer_reading(conn: sqlite3.Connection, case: Case, reading: _Read) -> dict[str, Any] | None:
    """The layer's line for this case's current bundle, read by code, else ``None``."""
    judged = ai_results.latest_judged(
        conn, tasks.DRAFT_INTERPRETATION,
        bundle(case, reading.old_line, list(reading.offered)).input_sha256,
    )  # fmt: skip
    if judged is None:
        return None
    lines = {str(r.get("line_id")) for r in judged.get("results", [])}
    layer = {"agree": judged["agree"] and len(lines) == 1, "models": judged["models"]}
    if not layer["agree"]:
        return {"layer": layer}
    layer |= {"executors": judged["executors"], "hint_only": judged["hint_only"]}
    number = int(lines.pop()[1:])
    line = reading.new_lines[number - 1] if number <= len(reading.new_lines) else ""
    unit = str(case.binding["unit"])
    found = readings_on(line, unit)
    if len(found) != 1:
        return {"layer": layer, "status": "several_values" if found else "no_value",
                "line": number}  # fmt: skip
    start, decimal, end = found[0]
    if not _same_unit_word(unit, reading.old_line, reading.old_end, line, end):
        return {"layer": layer, "status": "unit_differs", "line": number}
    return {"layer": layer, "status": "drafted", "line": number, "start": start, "end": end,
            "decimal": decimal}  # fmt: skip


SUMMARY = {
    "unit_differs": "The new version reports it in another unit, or none; compare it by hand.",
    "unreadable_number": "The new version writes it in a form PrometheOS does not read as a plain "
    "decimal; read it by hand.",
    "no_value": "The line holds no number in this unit; read it by hand.",
    "several_values": "The proposed line holds several numbers in this unit; read it by hand.",
    "label_not_found": "No line of the new version starts with the same label.",
    "label_ambiguous": "The label does not name one line alone, in the old version or the new.",
    "not_text": "Only bindings to lines of retained text files up to 2 MB are drafted; compare "
    "it by hand.",
}


def _row(case: Case, reading: _Read, layer: dict[str, Any] | None) -> dict[str, Any]:
    from prometheos.app.research_map import _line

    binding = case.binding
    unit = str(binding["unit"])
    status, line, decimal = reading.status, reading.line, reading.decimal
    start, end = reading.start, reading.end
    found_by = "label" if status == "drafted" else None
    if layer is not None and "status" in layer:
        status, line = layer["status"], layer["line"]
        decimal, start, end = layer.get("decimal"), layer.get("start", 0), layer.get("end", 0)
        found_by = "layer"
    measured = None
    difference = relative = None
    if status == "drafted" and decimal is not None and line is not None:
        measured = {
            "decimal": decimal,
            "source_id": case.new["body"]["source_id"],
            "version_id": case.new["id"],
            "line": line,
            "source_pointer": f"text:{line}:{start}:{end}",
        }
        difference, relative = _difference(str(binding["decimal"]), decimal)
    spaced = f" {unit}" if unit else ""
    if measured is not None:
        rel = f" ({relative} %)" if relative is not None else ""
        summary = (
            f"Expected {binding['decimal']}{spaced}, measured {decimal}{spaced} on line {line} of "
            f"the new version: a difference of {difference}{spaced}{rel}. No tolerance is set; "
            "choose one when you record the check."
        )
    else:
        summary = SUMMARY.get(status, "Compare it by hand.")
    return {
        "claim_id": case.claim["id"],
        "claim_line": _line(case.claim["body"]),
        "quantity_index": case.index,
        "name": binding.get("name"),
        "unit": unit,
        "status": status,
        "found_by": found_by,
        "expected": {
            "decimal": binding["decimal"],
            "version_id": case.old["id"],
            "source_pointer": binding["source_pointer"],
        },
        "measured": measured,
        "difference": difference,
        "relative_difference_percent": relative,
        "tolerance": None,
        "layer": None if layer is None else layer["layer"],
        "layer_subject": case.subject
        if reading.status in NEEDS_LAYER and reading.offered
        else None,
        "summary": summary,
    }


def drafts(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    awaiting: list[dict[str, Any]] | None = None,
) -> dict[str, list[Any]]:
    """Awaiting item -> its draft rows (rules first, then the layer's agreeing line). Read-only;
    ``awaiting`` is the queue when the caller already read it."""
    out: dict[str, list[dict[str, Any]]] = {}
    cache: Lines = {}
    for case in cases(conn, caller, awaiting):
        reading = rule_reading(conn, store_dir, case, cache)
        layer = None
        if reading.status in NEEDS_LAYER and reading.offered:
            layer = _layer_reading(conn, case, reading)
        out.setdefault(case.target_id, []).append(_row(case, reading, layer))
    return out


__all__ = ["TASK_KIND", "Case", "bundle", "cases", "drafts", "read_value", "subjects"]
