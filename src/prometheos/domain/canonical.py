"""Canonical JSON encoding and strict decoding shared by tools, tests and transports.

Canonical form (spec/04-interfaces.md): UTF-8, object keys sorted, compact
separators, no NaN/Infinity, strings preserved exactly (no Unicode
normalization), array order preserved. Duplicate object keys are errors.

Nesting is bounded by two limits (decision 0029); every array or object adds one level and a
scalar adds none. Input from outside (a request body, a file the researcher supplies, a
caller's parameter) may be ``MAX_INPUT_DEPTH`` = 64 levels deep: about three times the deepest
generated schema (22) and six times the deepest committed document (11). Everything the
application writes, wraps or re-reads (a stored row, a response envelope, a plan) is held only
to ``MAX_INTERNAL_DEPTH`` = 96, so an admitted value stays readable after any envelope the
application puts around it (32 levels of margin; pydantic's own validation and serialization
of a JSON value fail from about 99 levels, so the ceiling cannot be higher). A deeper document
is refused with a ``JSONDepthError``
(a ``CanonicalJSONError``), never an untyped ``RecursionError``: text is scanned in linear time,
without recursion, before the interpreter's recursive parser sees it, and values are walked
iteratively.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

MAX_INPUT_DEPTH = 64
"""Deepest nesting admitted from outside input (decision 0029)."""

MAX_INTERNAL_DEPTH = 96
"""Deepest nesting the application encodes or re-reads: admitted input plus any envelope."""


class CanonicalJSONError(ValueError):
    """Raised for input that cannot be represented or parsed canonically."""


class JSONDepthError(CanonicalJSONError):
    """Nesting deeper than ``limit`` levels. ``internal`` marks the application's own ceiling:
    something it wrote or wrapped is too deep, which is no fault of the caller and does not
    go away on retry."""

    def __init__(self, limit: int, message: str | None = None) -> None:
        super().__init__(message or f"JSON nesting is deeper than {limit} levels")
        self.limit = limit

    @property
    def internal(self) -> bool:
        return self.limit > MAX_INPUT_DEPTH


def _reject_constant(name: str) -> Any:
    raise CanonicalJSONError(f"non-finite number {name} is not allowed")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalJSONError(f"duplicate object key: {key!r}")
        result[key] = value
    return result


def _check_string(text: str) -> None:
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        raise CanonicalJSONError("lone surrogate code points are not valid Unicode text")


# A whole string literal, or one bracket. The string pattern can never fail to match once it
# has seen its opening quote: it ends at the closing quote, at the end of the text, or at a
# final lone backslash (an unterminated string runs to the end). Possessive quantifiers keep it
# from backtracking, so the scan is linear whatever the input. Were it able to fail at the
# end, the substitution would restart at every later quote and become quadratic.
_STRING = re.compile(r'"(?:[^"\\]++|\\.)*+(?:"|\\?\Z)', re.DOTALL)
_BRACKET = re.compile(r"[\[\]{}]")


def check_text_depth(data: str | bytes, max_depth: int = MAX_INPUT_DEPTH) -> None:
    """Refuse JSON text that nests arrays or objects deeper than ``max_depth``.

    A linear, non-recursive scan that counts brackets outside string literals (an escaped
    quote does not end a string). It judges structure only: text that is not valid JSON may
    pass here and is refused by the parser. Bytes are scanned as UTF-8 (undecodable bytes
    cannot hide a bracket, which is ASCII); a UTF-16 or UTF-32 body is not understood.
    """
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    # No document nests deeper than it has openers; most documents stop here.
    if text.count("[") + text.count("{") <= max_depth:
        return
    depth = 0
    for bracket in _BRACKET.finditer(_STRING.sub("", text)):
        if bracket.group() in "[{":
            depth += 1
            if depth > max_depth:
                raise JSONDepthError(max_depth)
        else:
            depth -= 1


def _check_value(value: Any, max_depth: int) -> None:
    """Reject nesting beyond ``max_depth``, non-finite numbers and lone surrogates anywhere
    in a JSON value. Iterative: a very deep or self-referencing value cannot exhaust the
    interpreter's stack."""

    def scalar(node: Any) -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise CanonicalJSONError("non-finite number is not allowed")
        if isinstance(node, str):
            _check_string(node)

    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if isinstance(node, dict):
            for key in node:
                _check_string(key)
            children: Any = node.values()
        elif isinstance(node, list | tuple):
            children = node
        else:
            scalar(node)
            continue
        depth += 1
        if depth > max_depth:
            raise JSONDepthError(max_depth)
        for child in children:
            if isinstance(child, dict | list | tuple):
                pending.append((child, depth))
            else:
                scalar(child)


def check_input_depth(value: Any) -> None:
    """Refuse a caller-supplied value nested deeper than ``MAX_INPUT_DEPTH`` (the type of a
    caller's JSON parameter; see ``JsonObjectInput``)."""
    _check_value(value, MAX_INPUT_DEPTH)


def loads_strict(data: str | bytes, *, max_depth: int = MAX_INPUT_DEPTH) -> Any:
    """Parse JSON rejecting duplicate keys, NaN/Infinity, lone surrogates, bad UTF-8 and
    nesting deeper than ``max_depth`` (outside input: ``MAX_INPUT_DEPTH``)."""
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalJSONError(f"invalid UTF-8: {exc}") from exc
    else:
        text = data
    if text.startswith("﻿"):
        raise CanonicalJSONError("byte order mark is not allowed")
    check_text_depth(text, max_depth)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise CanonicalJSONError(f"invalid JSON: {exc.msg} at {exc.lineno}:{exc.colno}") from exc
    except ValueError as exc:
        # An integer of more than 4300 digits (Python's conversion limit) is a ValueError, not a
        # JSONDecodeError: still invalid JSON for every caller, never a crash (S11.5 review F1).
        raise CanonicalJSONError(f"invalid JSON: {exc}") from exc
    except RecursionError as exc:  # defence in depth: the scan above should make this unreachable
        raise JSONDepthError(max_depth, "JSON nesting is too deep to parse") from exc
    # Large literals such as 1e999 parse to inf without calling parse_constant, and
    # escaped lone surrogates parse without error; both are rejected here.
    _check_value(value, max_depth)
    return value


def loads_internal(data: str | bytes) -> Any:
    """``loads_strict`` for what the application wrote itself (a stored row, a plan, a
    response, the encoder's own output): held to ``MAX_INTERNAL_DEPTH``, the limit it was
    written under, not to the admission limit of outside input."""
    return loads_strict(data, max_depth=MAX_INTERNAL_DEPTH)


def loads_bounded(text: str) -> Any:
    """``json.loads`` for text from outside that the caller reads leniently (duplicate keys,
    NaN and the rest are accepted as before) but must not let recurse without bound: nesting
    deeper than ``MAX_INPUT_DEPTH`` is a ``CanonicalJSONError`` (a ``ValueError``, as a syntax
    error is)."""
    check_text_depth(text)
    try:
        return json.loads(text)
    except RecursionError as exc:  # defence in depth
        raise JSONDepthError(MAX_INPUT_DEPTH, "JSON nesting is too deep to parse") from exc


def dumps_canonical(value: Any) -> bytes:
    """Encode a JSON-compatible value in canonical form (at most ``MAX_INTERNAL_DEPTH``)."""
    _check_value(value, MAX_INTERNAL_DEPTH)
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return text.encode("utf-8")
    except RecursionError as exc:  # defence in depth
        raise JSONDepthError(MAX_INTERNAL_DEPTH, "JSON nesting is too deep to encode") from exc
    except (TypeError, ValueError) as exc:
        raise CanonicalJSONError(str(exc)) from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_hex(dumps_canonical(value))
