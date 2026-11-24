"""Deterministic rule extraction for evidence matrices (A02, CP11 S11.4; decision 0064 §2).

Rules are data, never code. A rule set is JSON::

    {"rules": [
      {"id": "method-experiment", "column": "method", "keywords": ["was evacuated"],
       "value": "experiment"},
      {"id": "pressure", "column": "pressure", "pattern": "evacuated to {value}", "unit": "Pa"},
      {"id": "finding", "column": "finding", "pattern": "reduces windage loss by {value} %"}
    ]}

The pattern language is a safe subset, not a regular expression: literal words (matched without
regard to case, any run of white space between them), and at most one ``{value}`` placeholder
that matches one written decimal number. A quantity column's rule names its ``unit``, which must
follow the number (a space, a no-break space or nothing between). Patterns compile to
expressions built only from escaped literals, ``\\s+`` between words and one fixed number class,
so matching is linear in the text: there is nothing to backtrack into. Sizes are bounded (rules,
keywords, pattern length) and a run stops at a time limit.

Rules run over each work's extracted page text (basis ``full_text``) or, without full text, its
abstract (basis ``abstract``). For each column the first rule that matches wins (pages in order,
then position); each hit becomes a proposed cell with ``method: rule``, the matched span as its
evidence (page and code-point offsets, or lines, and the quote digest) and a transformation
naming the rule and its version. A column no rule matches is ``unsupported`` ("not found: no
match"). Cells that are reviewed, or proposed by a person or a model, are never replaced. Every
row is written through the apply path, so rule cells are grounded exactly like any other.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from prometheos.app import quantities
from prometheos.app.grounding import unsupported_text
from prometheos.domain.canonical import canonical_sha256, dumps_canonical
from prometheos.domain.models import PrometheosError, StrictModel

ENGINE = "prometheos-matrix-rule"
MAX_RULES = 100
MAX_KEYWORDS = 20
MAX_PATTERN = 200
MAX_RULES_BYTES = 64 * 1024
TIME_LIMIT_SECONDS = 60.0
PLACEHOLDER = "{value}"
NUMBER = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?"

RuleId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,39}$")]
Phrase = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class Rule(StrictModel):
    id: RuleId
    version: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,20}$")] = "1"
    column: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
    keywords: list[Phrase] | None = Field(default=None, min_length=1, max_length=MAX_KEYWORDS)
    pattern: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATTERN)] | None = None
    unit: Annotated[str, StringConstraints(max_length=64)] | None = None
    value: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def _shape(self) -> Rule:
        if (self.keywords is None) == (self.pattern is None):
            raise ValueError("a rule has keywords or a pattern, not both")
        texts = self.keywords or [self.pattern or ""]
        for text in texts:
            if not text.split():
                raise ValueError("a keyword or pattern has at least one word")
            if ("{" in text.replace(PLACEHOLDER, "")) or ("}" in text.replace(PLACEHOLDER, "")):
                raise ValueError("the only placeholder is {value}")
        if self.keywords is not None and any(PLACEHOLDER in k for k in self.keywords):
            raise ValueError("keywords have no placeholder; use a pattern")
        if self.pattern is not None and self.pattern.count(PLACEHOLDER) > 1:
            raise ValueError("a pattern has at most one {value}")
        return self


class RuleSet(StrictModel):
    rules: list[Rule] = Field(min_length=1, max_length=MAX_RULES)

    @model_validator(mode="after")
    def _unique(self) -> RuleSet:
        ids = [r.id for r in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate rule id")
        return self


def parse(raw: Any) -> RuleSet:
    if len(dumps_canonical(raw)) > MAX_RULES_BYTES:
        raise PrometheosError("budget_exceeded", "a rule set is at most 64 KiB", {})
    try:
        return RuleSet.model_validate(raw, strict=True)
    except ValidationError as exc:
        errors = [
            {"loc": "/".join(str(p) for p in e["loc"]), "msg": e["msg"]} for e in exc.errors()[:20]
        ]
        raise PrometheosError("validation_error", "invalid rule set", {"errors": errors}) from exc


def check_against(rules: RuleSet, columns: list[dict[str, Any]]) -> None:
    """Each rule names a column of the matrix and fits its kind."""
    by_id = {str(c["id"]): c for c in columns}
    for rule in rules.rules:
        column = by_id.get(rule.column)
        if column is None:
            raise PrometheosError(
                "validation_error", f"rule {rule.id}: no column {rule.column} in the matrix", {}
            )
        kind = column["value_kind"]
        if kind == "quantity":
            if rule.pattern is None or PLACEHOLDER not in rule.pattern:
                raise PrometheosError(
                    "validation_error",
                    f"rule {rule.id}: a quantity rule is a pattern with {PLACEHOLDER}",
                    {},
                )
            if rule.value is not None:
                raise PrometheosError(
                    "validation_error", f"rule {rule.id}: a quantity rule sets no value", {}
                )
        elif rule.unit is not None:
            raise PrometheosError(
                "validation_error", f"rule {rule.id}: only quantity rules name a unit", {}
            )
        if kind == "choice" and rule.value not in (column.get("choices") or []):
            raise PrometheosError(
                "validation_error",
                f"rule {rule.id}: value must be one of the column's choices",
                {"choices": column.get("choices")},
            )
        if kind == "text" and rule.value is not None:
            raise PrometheosError(
                "validation_error", f"rule {rule.id}: a text rule takes the matched text", {}
            )


# ----- compiling the safe patterns ---------------------------------------------------------------


def _literal(part: str) -> str:
    out = r"\s+" if part[:1].isspace() else ""
    words = part.split()
    out += r"\s+".join(re.escape(w) for w in words)
    if words and part[-1:].isspace():
        out += r"\s+"
    return out


def _bounded(body: str, starts_word: bool, ends_word: bool) -> str:
    return (r"(?<!\w)" if starts_word else "") + body + (r"(?!\w)" if ends_word else "")


def _edges(text: str) -> tuple[bool, bool]:
    stripped = text.strip()
    return stripped[:1].isalnum(), stripped[-1:].isalnum()


def compile_rule(rule: Rule, value_kind: str) -> re.Pattern[str]:
    """The rule as an expression of escaped literals, white space runs and one number class."""
    # Leading and trailing white space is dropped (review F2): a run of white space at the edge
    # of an expression would be tried at every position of a long white-space run (quadratic);
    # between words it always follows a literal, so each attempt is anchored.
    if rule.keywords is not None:
        alternatives = [_bounded(_literal(k.strip()), *_edges(k)) for k in rule.keywords]
        return re.compile("(?:" + "|".join(alternatives) + ")", re.IGNORECASE)
    pattern = (rule.pattern or "").strip()
    if PLACEHOLDER not in pattern:
        return re.compile(_bounded(_literal(pattern), *_edges(pattern)), re.IGNORECASE)
    before, after = pattern.split(PLACEHOLDER)
    value = f"(?P<value>{NUMBER})"
    tail = ""
    if value_kind == "quantity" and rule.unit:
        value += "(?P<sep>[ \u00a0]?)(?P<unit>" + re.escape(rule.unit) + ")"
        tail = rule.unit
    starts = _edges(before)[0] if before.strip() else True
    ends = _edges(after)[1] if after.strip() else (_edges(tail)[1] if tail else True)
    body = _literal(before) + value + _literal(after)
    return re.compile(_bounded(body, starts, ends), re.IGNORECASE)


# ----- one hit -----------------------------------------------------------------------------------


class Hit:
    """A rule's match in one located text: the span ``[start, end)`` of ``text`` and, for a
    quantity, the written value's span."""

    def __init__(self, rule: Rule, text: str, match: re.Match[str], value_kind: str) -> None:
        self.rule = rule
        self.start, self.end = match.start(), match.end()
        self.matched = text[self.start : self.end]
        self.quantity: dict[str, Any] | None = None
        if value_kind == "quantity":
            decimal = match.group("value")
            unit = rule.unit or ""
            q_end = match.end("unit") if rule.unit else match.end("value")
            self.quantity = {
                "decimal": decimal,
                "unit": unit,
                "start": match.start("value"),
                "end": q_end,
            }


def _first(
    pattern: re.Pattern[str],
    rule: Rule,
    text: str,
    value_kind: str,
    deadline: float,
    index: Any = None,
) -> Hit | None:
    """The first hit whose value stands as a whole token. ``index`` (``quantities.starts`` of
    the text, or a callable giving it) is computed once per text, not per candidate (review
    round 2, R2-F1)."""
    for match in pattern.finditer(text):
        if time.monotonic() > deadline:
            raise _timeout()
        if match.end() <= match.start():
            continue
        hit = Hit(rule, text, match, value_kind)
        if hit.quantity is not None:
            q = hit.quantity
            if callable(index):
                index = index()
            try:  # the value must stand as a whole token, as grounding will check
                quantities.check_token(text, q["start"], q["end"], q, "", index)
            except PrometheosError:
                continue
        return hit
    return None


def _timeout() -> PrometheosError:
    return PrometheosError(
        "budget_exceeded",
        f"rule extraction stopped at its time limit ({int(TIME_LIMIT_SECONDS)} s); narrow the "
        "rules or the matrix",
        {},
    )


# ----- a work's located texts --------------------------------------------------------------------


class Located:
    """One searchable text of a work: a PDF page, or the lines of a bibliography abstract."""

    def __init__(
        self,
        basis: Literal["full_text", "abstract"],
        text: str,
        cite: Callable[[int, int], dict[str, Any]],
        pointer: Callable[[int, int], str],
        input_version: str,
    ) -> None:
        self.basis = basis
        self.text = text
        self.cite = cite
        self.pointer = pointer
        self.input_version = input_version
        self._index: dict[int, Any] | None = None

    def index(self) -> dict[int, Any]:
        """The text's tokens by start offset, computed once (review round 2, R2-F1)."""
        if self._index is None:
            self._index = quantities.starts(self.text)
        return self._index


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pdf_pages(pages: Any) -> list[Located]:
    """``pages`` is a ``library.views.Pages``: each page with text, cited on the PDF version."""
    out = []
    for number, page in enumerate(pages.payload["pages"], start=1):
        text = str(page.get("text") or "")
        if page.get("state") != "text" or not text:
            continue

        def cite(start: int, end: int, n: int = number, t: str = text) -> dict[str, Any]:
            locator = {"type": "pdf", "page": n, "bbox": None, "start": start, "end": end}
            return {
                "source_id": pages.pdf_source_id,
                "version_id": pages.pdf_version_id,
                "locator": locator,
                "quote_sha256": _sha(t[start:end]),
            }

        out.append(
            Located(
                "full_text", text, cite, lambda s, e: f"pdf:{s}:{e}", str(pages.text_version["id"])
            )
        )
    return out


def abstract_lines(
    source_id: str, version_id: str, lines: list[str], start_line: int, end_line: int
) -> Located:
    """A bibliography abstract: the cited lines joined by LF; a hit cites the lines it spans."""
    chosen = lines[start_line - 1 : end_line]
    text = "\n".join(chosen)
    starts = []
    position = 0
    for line in chosen:
        starts.append(position)
        position += len(line) + 1

    def line_of(offset: int) -> int:
        index = 0
        for i, begin in enumerate(starts):
            if begin <= offset:
                index = i
        return index

    def cite(start: int, end: int) -> dict[str, Any]:
        first, last = line_of(start), line_of(max(start, end - 1))
        quoted = "\n".join(chosen[first : last + 1])
        return {
            "source_id": source_id,
            "version_id": version_id,
            "locator": {
                "type": "text",
                "start_line": start_line + first,
                "end_line": start_line + last,
            },
            "quote_sha256": _sha(quoted),
        }

    def pointer(start: int, end: int) -> str:
        index = line_of(start)
        begin = starts[index]
        return f"text:{start_line + index}:{start - begin}:{end - begin}"

    return Located("abstract", text, cite, pointer, version_id)


# ----- cells -------------------------------------------------------------------------------------


def _transformation(rules: list[Rule], inputs: list[str]) -> dict[str, Any]:
    return {
        "type": "extraction",
        "engine": f"{ENGINE}:{','.join(r.id for r in rules)}"[:200],
        "engine_version": "+".join(r.version for r in rules)[:100],
        "model": None,
        "language": None,
        "parameters_digest": canonical_sha256([r.model_dump(mode="json") for r in rules]),
        "input_version_refs": sorted(set(inputs))[:200],
        "run_id": None,
    }


def cell_for(
    column: dict[str, Any],
    rules: list[Rule],
    texts: list[Located],
    fallback_basis: str,
    deadline: float,
) -> dict[str, Any]:
    """The cell the first matching rule gives (texts in order), or an unsupported cell."""
    kind = str(column["value_kind"])
    for rule in rules:
        compiled = compile_rule(rule, kind)
        for located in texts:
            if time.monotonic() > deadline:
                raise _timeout()
            hit = _first(compiled, rule, located.text, kind, deadline, located.index)
            if hit is None:
                continue
            evidence = located.cite(hit.start, hit.end)
            bound = []
            if hit.quantity is not None:
                q = hit.quantity
                bound.append(
                    {
                        "decimal": q["decimal"],
                        "unit": q["unit"],
                        "source_ref": evidence,
                        "source_pointer": located.pointer(q["start"], q["end"]),
                    }
                )
                value = located.text[q["start"] : q["end"]]
            elif kind == "choice":
                value = str(rule.value)
            else:
                value = " ".join(hit.matched.split())
            return {
                "column_id": column["id"],
                "value_text": value,
                "quantities": bound,
                "evidence": [evidence],
                "basis": located.basis,
                "state": "proposed",
                "method": "rule",
                "transformation": _transformation([rule], [located.input_version]),
            }
    basis = texts[0].basis if texts else fallback_basis
    searched = "no match" if texts else "no match (no page text or abstract to search)"
    return {
        "column_id": column["id"],
        "value_text": unsupported_text(searched),
        "quantities": [],
        "evidence": [],
        "basis": basis,
        "state": "unsupported",
        "method": "rule",
        "transformation": _transformation(rules, [t.input_version for t in texts]),
    }


def replaceable(cell: dict[str, Any] | None) -> bool:
    """A rule may (re)write a missing cell or an earlier rule's unreviewed cell, nothing else."""
    return cell is None or (cell["method"] == "rule" and cell["state"] != "reviewed")


def merge_row(
    columns: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    produced: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """The row's cells after a run, in column order (cells of removed columns are kept last)."""
    current = {str(c["column_id"]): c for c in existing}
    out = []
    for column in columns:
        cid = str(column["id"])
        new = produced.get(cid)
        old = current.pop(cid, None)
        if new is not None and replaceable(old):
            out.append(new)
        elif old is not None:
            out.append(old)
    out.extend(current.values())
    return out


def deadline() -> float:
    return time.monotonic() + TIME_LIMIT_SECONDS


def rules_digest(rules: RuleSet) -> str:
    return canonical_sha256(rules.model_dump(mode="json"))
