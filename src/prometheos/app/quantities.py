"""Exact structured quantities (spec/03 "Source checks and scientific quantities").

A binding ``{decimal, unit, source_ref, source_pointer}`` must reproduce the
retained bytes exactly. ``source_pointer`` is ``text:<line>:<start>:<end>``
(1-based line inside the evidence locator's line range; 0-based character
offsets, end exclusive) or ``json:<RFC 6901 pointer>`` for retained JSON. The
addressed characters must be exactly ``<decimal> <unit>``, ``<decimal><unit>`` or,
for a unitless value, ``<decimal>``. There is no tolerance: 2.4 does not match
2.40, and a locator copied onto a different version fails. Conversions require
a separately recorded transformation, never a silent reformat.
"""

from __future__ import annotations

import re
from typing import Any

from prometheos.domain.ids import RECORD_ID_PATTERN
from prometheos.domain.models import PrometheosError

OPENING = set("([{\"'=:")  # qualifiers such as < > ~ change the meaning: not allowed
# Separators that continue a written number when a digit precedes them: 3:4, 10:30,
# 1'240, 1,240 and digit groups separated by (thin / no-break / ordinary) spaces.
GROUP_SEPARATORS = {":", "'", ",", ".", " ", chr(0x2009), chr(0x202F), chr(0xA0)}
CLOSING = set(")]}\"',;:!?")
UNIT_CONTINUATION = {".", "/", "^", "_", "%", chr(0xB7), chr(0xB2), chr(0xB3)}
NBSP = "\u00a0"
TEXT_POINTER = re.compile(r"^text:(\d+):(\d+):(\d+)$")
NUMBER_START = re.compile(r"(?<![\w.])[-+\u2212\u00b1]?(?:\d+(?:[.,]\d+)?|\.\d+)")
DATE_OR_TIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?)?")
# Record identifiers (kind_<uuid4>) name records; their hex digits are not quantities.
RECORD_ID = re.compile(RECORD_ID_PATTERN.strip("^$"))
MINUS = chr(0x2212)

# ----- one number tokenizer (CP11 S11.4 review F1; decision 0065) ------------------------------
#
# A written number is one token: an optional sign (only where a sign can stand: not after a
# letter, a digit, '.' or ')'), digit groups joined by separators that continue a number
# (1,240  1'240  1 240 with thin or no-break spaces  2,40  3:4  10:30), an optional decimal part
# (.5 alone too) and an optional exponent (4.2e-3). A date, or a date and time, is one token; a
# word that glues letters to digits (v0.042, CP2, r5000) is one token; a record ID (kind_<uuid4>)
# is one token. Two tokens are the same number only when their texts are equal (a typographic
# minus U+2212 reads as '-'): 240 is not 1,240, 3.2 is not -3.2, 3 is not 4.2e-3.
_SEPARATORS = ",:'" + chr(0x2009) + chr(0x202F) + NBSP
# Digit groups joined by a plain space ("1 240", SI style) are one number too, when every group
# after the first has exactly three digits (review round 2, R2-F3): "240 W" is then not a
# token of "1 240 W", for text cells as for quantities.
_SPACED = r"\d{1,3}(?: \d{3})+(?![\d.,])"
_CORE = rf"(?:{_SPACED}|\d+(?:[.{_SEPARATORS}]\d+)*|\.\d+)"
_NUMBER = rf"(?<![\w.])(?:[-+{MINUS}±](?=[\d.]))?{_CORE}(?:[eE][-+{MINUS}]?\d+)?"
# A word glued to digits, directly or through a full stop (v0.042, CP2, loss.5, approx.5;
# review round 2, R2-F2: a decimal after "word." is not hidden). A record ID is an ID only
# when no word character or glued decimal follows it.
_WORD = r"(?<!\w)[^\W\d]+\.?\d\w*(?:[.,]\d+\w*)*"
TOKENS = re.compile(
    rf"(?P<id>{RECORD_ID.pattern}(?!\w|\.\d))|(?P<datetime>(?<![\w.]){DATE_OR_TIME.pattern})"
    rf"|(?P<word>{_WORD})|(?P<number>{_NUMBER})"
)


class Token:
    __slots__ = ("end", "kind", "start", "text")

    def __init__(self, kind: str, text: str, start: int, end: int) -> None:
        self.kind, self.text, self.start, self.end = kind, text, start, end

    @property
    def value(self) -> str:
        """The text compared with another token: a typographic minus reads as '-', and a
        number's exponent letter is read without case (4.2E-3 is 4.2e-3)."""
        if self.kind == "number":
            return number_value(self.text)
        return self.text.replace(MINUS, "-")

    def __repr__(self) -> str:
        return f"Token({self.kind}, {self.text!r}, {self.start}, {self.end})"


def tokens(text: str) -> list[Token]:
    """Every numeric token of ``text`` in order: numbers, dates, digit-bearing words, IDs."""
    out = []
    for match in TOKENS.finditer(text):
        kind = match.lastgroup or "number"
        out.append(Token(kind, match.group(0), match.start(), match.end()))
    return out


def number_value(text: str) -> str:
    """A written number as compared: U+2212 reads as '-', the exponent letter as 'e'."""
    return text.replace(MINUS, "-").replace("E", "e")


def starts(text: str) -> dict[int, Token]:
    """The tokens of ``text`` by start offset: tokenise a text once, then look positions up
    (review round 2, R2-F1: re-tokenising per candidate was quadratic)."""
    return {token.start: token for token in tokens(text)}


def number_at(text: str, start: int, index: dict[int, Token] | None = None) -> Token | None:
    """The number token that starts exactly at ``start``, if any (``index``: ``starts(text)``,
    computed once by a caller that checks many positions of one text)."""
    token = (index if index is not None else starts(text)).get(start)
    return token if token is not None and token.kind == "number" else None


def _fail(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


def accepted_forms(decimal: str, unit: str) -> set[str]:
    if not unit:
        return {decimal}
    return {f"{decimal} {unit}", f"{decimal}{unit}", f"{decimal}{NBSP}{unit}"}


def verify_text(lines: list[str], start_line: int, end_line: int, quantity: dict[str, Any]) -> None:
    """``lines`` are the version's LF-split lines (see app.apply.text_lines)."""
    match = TEXT_POINTER.fullmatch(quantity["source_pointer"])
    if not match:
        raise _fail("text quantities need source_pointer text:<line>:<start>:<end>")
    line_no, start, end = (int(g) for g in match.groups())
    if not start_line <= line_no <= end_line:
        raise _fail(
            "quantity pointer lies outside the evidence locator", pointer=quantity["source_pointer"]
        )
    if line_no > len(lines) or not 0 <= start < end <= len(lines[line_no - 1]):
        raise _fail(
            "quantity pointer is outside the retained text", pointer=quantity["source_pointer"]
        )
    check_token(lines[line_no - 1], start, end, quantity, quantity["source_pointer"])


PDF_POINTER = re.compile(r"^pdf:(\d+):(\d+)$")


def verify_pdf(page_text: str, span_start: int, span_end: int, quantity: dict[str, Any]) -> None:
    """A quantity bound in extracted PDF page text (CP11 S11.4, decision 0065): ``source_pointer``
    is ``pdf:<start>:<end>``, Unicode code-point offsets into the page text that lie inside the
    evidence span ``[span_start, span_end)``. The same exact-characters and whole-token rules as
    a text pointer apply: no tolerance, numbers below ten included."""
    match = PDF_POINTER.fullmatch(quantity["source_pointer"])
    if not match:
        raise _fail("PDF quantities need source_pointer pdf:<start>:<end> (code points)")
    start, end = (int(g) for g in match.groups())
    if not span_start <= start < end <= span_end:
        raise _fail(
            "quantity pointer lies outside the quoted evidence span",
            pointer=quantity["source_pointer"],
        )
    check_token(page_text, start, end, quantity, quantity["source_pointer"])


def unit_forms(unit: str) -> list[str]:
    """What may follow the number: nothing for a unitless value, else the unit after a space, a
    no-break space or nothing (longest first)."""
    return [""] if not unit else [f" {unit}", f"{NBSP}{unit}", unit]


def check_token(
    line: str,
    start: int,
    end: int,
    quantity: dict[str, Any],
    pointer: str,
    index: dict[int, Token] | None = None,
) -> None:
    """``line[start:end]`` is exactly one written form of the quantity, as a whole token: the
    number token that starts there (the shared tokenizer) is the decimal, sign, separators and
    exponent included, and the unit follows it. ``index`` (``starts(line)``) lets a caller that
    checks many positions of one text tokenise it once."""
    token = number_at(line, start, index)
    if token is None:
        raise _fail("quantity span starts inside a longer token", pointer=start)
    if token.value != number_value(quantity["decimal"]) or token.end > end:
        raise _fail(
            "bound quantity does not match the retained bytes exactly",
            pointer=pointer,
            written=token.text,
        )
    if line[token.end : end] not in unit_forms(quantity["unit"]):
        raise _fail(
            "bound quantity does not match the retained bytes exactly",
            pointer=pointer,
            expected_forms=sorted(accepted_forms(quantity["decimal"], quantity["unit"])),
        )
    # The span must be a whole token. Boundaries are an allowlist: start of line,
    # whitespace or simple opening punctuation before; end of line, whitespace or simple
    # closing punctuation after (a full stop only when it ends a sentence). Anything else
    # (1,240  3/4  -0.5  +-0.5  2,40  12.40  kJ/s  k inside kJ) is rejected; ranges such as
    # 1.5-2.0 kJ are therefore not bindable as single values.
    before = line[start - 1] if start > 0 else ""
    after = line[end] if end < len(line) else ""
    following = line[end + 1] if end + 1 < len(line) else ""
    preceding = line[start - 2] if start > 1 else ""
    if before in GROUP_SEPARATORS and preceding.isdigit():
        raise _fail("quantity span continues an earlier number", pointer=start)
    if before and not (before.isspace() or before in OPENING):
        raise _fail("quantity span starts inside a longer token", pointer=start)
    if after == "." and (not following or following.isspace() or following in CLOSING):
        after = ""  # a sentence-ending full stop
    if (after and not (after.isspace() or after in CLOSING)) or (
        after in {",", ":", ";"} and following.isdigit()  # 2,40 or 3:4 continue the number
    ):
        raise _fail("quantity span ends inside a longer token", pointer=end)


def find_token(
    text: str, decimal: str, unit: str, lo: int = 0, hi: int | None = None
) -> tuple[int, int] | None:
    """The first span ``(start, end)`` inside ``text[lo:hi]`` where a written form of the
    quantity stands as a whole token (the span check above), else ``None``. The text is
    tokenised once; a typographic minus and an upper-case exponent are found too (review round
    2, R2-F4)."""
    hi = len(text) if hi is None else hi
    index = starts(text)
    wanted = number_value(decimal)
    quantity = {"decimal": decimal, "unit": unit}
    for position in sorted(index):
        token = index[position]
        if position < lo or token.kind != "number" or token.value != wanted:
            continue
        for tail in unit_forms(unit):
            stop = token.end + len(tail)
            if stop > hi or text[token.end : stop] != tail:
                continue
            try:
                check_token(text, position, stop, quantity, "", index)
            except PrometheosError:
                continue
            return position, stop
    return None


def verify_json(document: Any, quantity: dict[str, Any]) -> None:
    from prometheos.app.apply import resolve_pointer

    pointer = quantity["source_pointer"]
    if not pointer.startswith("json:"):
        raise _fail("JSON quantities need source_pointer json:<RFC 6901 pointer>")
    try:
        value = resolve_pointer(document, pointer[5:])
    except (KeyError, IndexError, ValueError) as exc:
        raise _fail("quantity JSON pointer does not resolve") from exc
    if not isinstance(value, str):
        raise _fail(
            "JSON numbers lose their written form; bind a string value or record a transformation",
            pointer=pointer,
        )
    if value not in accepted_forms(quantity["decimal"], quantity["unit"]):
        raise _fail(
            "bound quantity does not match the retained JSON value exactly", pointer=pointer
        )


def unbound_numbers(text: str, quantities: list[dict[str, Any]]) -> list[str]:
    """Numbers in generated prose not written exactly as a bound quantity.

    Each number token (the shared tokenizer; dates and times, record IDs and words that glue
    letters to digits such as CP2 are not numbers here) must be one of the bound decimals and
    start one of its accepted written forms, e.g. "2.40 kJ", and that form must end at a token
    boundary. "2.41kJ", "3x", a U+2212 minus before a bound 2.40 and a bound decimal with a
    different unit ("2.40 MJ") are reported.
    """
    found = []
    for token in tokens(text):
        if token.kind != "number":
            continue
        ok = False
        for q in quantities:
            if token.value != number_value(q["decimal"]):
                continue
            for tail in unit_forms(q["unit"]):
                if text[token.end : token.end + len(tail)] != tail:
                    continue
                stop = token.end + len(tail)
                after = text[stop : stop + 1]
                following = text[stop + 1 : stop + 2]
                if after == "." and (not following or following.isspace() or following in CLOSING):
                    after = ""
                continues = after in {",", ":", ";"} and following.isdigit()
                if (not after or after.isspace() or after in CLOSING) and not continues:
                    ok = True
                    break
            if ok:
                break
        if not ok:
            found.append(token.text)
    return found


def unquoted_tokens(
    text: str,
    quoted: list[str],
    *,
    numbers: bool = True,
    known_id: Any = None,
) -> list[str]:
    """Tokens of ``text`` (numbers when ``numbers``, dates, digit-bearing words, record IDs) that
    are not, whole and exactly, a token of a quoted slice. A record ID is excepted only when
    ``known_id(id)`` says it names a record the caller can see (review round 2, R2-F2: a forged
    ID is a token like any other)."""
    present = {(t.kind, t.value) for slice_ in quoted for t in tokens(slice_)}
    out = []
    for t in tokens(text):
        if t.kind == "number" and not numbers:
            continue
        if t.kind == "id" and known_id is not None and known_id(t.text):
            continue
        if (t.kind, t.value) not in present:
            out.append(t.text)
    return out
