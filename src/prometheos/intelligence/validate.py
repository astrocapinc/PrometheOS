"""Deterministic acceptance (A07.3; D16; decision 0092).

An output becomes a proposal only if every check passes; the first failing kind of check is
named by a reason code, and a rejected output is kept only with its reasons (never as evidence):

- ``shape``: it parses as strict JSON and validates against the task's strict model (the wire
  schema only shaped the reply; provider-side strict mode is advisory);
- ``digest_changed``: the subject still has the digest the bundle was built from;
- ``unoffered_option``: every id, path, class and option is one the bundle offered;
- ``quote_not_found`` and ``claim_not_bound``: every quote re-reads verbatim on the line it
  names in the visible text of the exact bytes, and carries what it is cited for (CP17.4);
- ``unbound_number``: generated text has no number of its own, only bound quantity ids;
- ``disagreement`` (``agree``): ``k`` validated samples are equal where the task needs it.

``abstained`` is a valid outcome: it counts toward coverage, never toward precision.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from prometheos.adoption import assist
from prometheos.domain.canonical import canonical_sha256, loads_strict

from .tasks import Task

Outcome = Literal["accepted", "abstained", "rejected"]
QUANTITY = re.compile(r"\{q:([A-Za-z0-9_.-]{1,80})\}")
DIGIT = re.compile(r"\d")
# Number words a model could write instead of digits (review map-ai-spread-b R1-F5, R2-F2): a
# conservative list, not every way to write a number. "one", "first" and "second" are left out
# (a pronoun, an order, a unit of time).
NUMBER_WORDS = re.compile(
    r"(?<![^\W\d_])(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundreds?|thousands?|millions?|billions?|dozens?|half|halves|twice|"
    r"double|triple|percent|percentage|thirds?|quarters?|fourths?|fifths?|sixths?|sevenths?|"
    r"eighths?|ninths?|tenths?|hundredths?|thousandths?)(?![^\W\d_])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    reasons: tuple[str, ...]
    output: dict[str, Any] | None


def _values(node: Any, path: list[str]) -> list[Any]:
    if not path:
        return [node]
    head, rest = path[0], path[1:]
    if head.endswith("[]"):
        items = node.get(head[:-2]) if isinstance(node, dict) else None
        return [v for item in items or [] for v in _values(item, rest)]
    if not isinstance(node, dict) or node.get(head) is None:
        return []
    return _values(node[head], rest)


def values_at(document: dict[str, Any], path: str) -> list[Any]:
    return _values(document, path.split("."))


def unbound_numbers(text: str, quantity_ids: frozenset[str]) -> bool:
    """True when the text holds a number outside a quantity id (any numeral, such as a digit,
    "½" or "Ⅻ", or a number word such as "twelve" or "percent"), or an id the bundle lacks."""
    if any(match.group(1) not in quantity_ids for match in QUANTITY.finditer(text)):
        return True
    rest = QUANTITY.sub(" ", text)
    if any(unicodedata.category(ch).startswith("N") for ch in rest):
        return True
    return NUMBER_WORDS.search(rest) is not None


def quoted_words(quote: str, text: str) -> bool:
    """A quote of a record's own words (``Task.quoted``): at least two whole words, verbatim and
    on word boundaries, so a fragment such as "s 1" or a lone "the" never counts (review
    map-ai-spread-a R1-F12)."""
    if len(quote.split()) < 2:
        return False
    return re.search(rf"(?<!\w){re.escape(quote)}(?!\w)", text) is not None


def _assist_reasons(problems: list[str]) -> list[str]:
    reasons = []
    for problem in problems:
        if "quote is not" in problem:
            reasons.append("quote_not_found")
        elif "not a candidate" in problem or "not in the inventory" in problem:
            reasons.append("unoffered_option")
        else:
            reasons.append("claim_not_bound")
    return reasons


def check(task: Task, bundle: assist.Bundle, text: str, *, current_digest: str | None) -> Verdict:
    try:
        document = loads_strict(text.encode("utf-8"))
        model = task.output.model_validate(document, strict=True)
    except (ValueError, ValidationError, TypeError):
        return Verdict("rejected", ("shape",), None)
    dumped = model.model_dump(mode="json")
    if current_digest != bundle.subject_sha256:
        return Verdict("rejected", ("digest_changed",), None)  # stale, abstained or not
    if dumped.get("abstain") is not None:
        return Verdict("abstained", (), dumped)
    reasons: list[str] = []
    for path, key in task.offered:
        offered = set(bundle.input.get(key) or [])
        if any(value not in offered for value in values_at(dumped, path)):
            reasons.append("unoffered_option")
    if task.assist_kind is not None:
        result = dumped["result"]
        problems = assist.validate(bundle, result, {}).problems
        reasons += _assist_reasons(problems)
    for path, source in task.quoted:
        texts = [t for t in values_at(bundle.input, source) if isinstance(t, str)]
        if any(not isinstance(v, str) or not any(quoted_words(v, t) for t in texts)
               for v in values_at(dumped, path)):  # fmt: skip
            reasons.append("quote_not_found")
    quantities = frozenset(str(q) for q in bundle.input.get("quantities") or [])
    for path in task.generated_text:
        if any(isinstance(v, str) and unbound_numbers(v, quantities)
               for v in values_at(dumped, path)):  # fmt: skip
            reasons.append("unbound_number")
    unique = tuple(dict.fromkeys(reasons))
    if unique:
        return Verdict("rejected", unique, None)
    return Verdict("accepted", (), dumped)


def agree(task: Task, outputs: list[dict[str, Any]]) -> bool:
    """``k`` validated outputs are equal (statements compared as sets, as in CP17.4)."""
    if len(outputs) < task.agreement:
        return False
    results = [o["result"] for o in outputs]
    if task.assist_kind is not None:
        return assist.same(results)
    if task.agree_on:
        keys = [[values_at(o, path) for path in task.agree_on] for o in outputs]
        return len({canonical_sha256(k) for k in keys}) == 1
    return len({canonical_sha256(r) for r in results}) == 1
