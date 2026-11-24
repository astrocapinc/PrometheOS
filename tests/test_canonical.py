from __future__ import annotations

import pytest

from prometheos.domain.canonical import (
    CanonicalJSONError,
    canonical_sha256,
    dumps_canonical,
    loads_strict,
)


def test_sorted_compact_and_order_preserving() -> None:
    assert dumps_canonical({"b": [3, 1, 2], "a": {"z": 1, "y": None}}) == (
        b'{"a":{"y":null,"z":1},"b":[3,1,2]}'
    )


def test_unicode_is_preserved_without_normalization() -> None:
    composed, decomposed = "café", "café"
    assert dumps_canonical(composed) == '"café"'.encode()
    assert canonical_sha256({"t": composed}) != canonical_sha256({"t": decomposed})


@pytest.mark.parametrize(
    "text",
    [
        '{"a":1,"a":2}',
        '{"outer":{"k":1,"k":1}}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":-Infinity}',
        '{"x":1e999}',
        '﻿{"x":1}',
        '{"x":1',
    ],
)
def test_strict_loader_rejects(text: str) -> None:
    with pytest.raises(CanonicalJSONError):
        loads_strict(text)


def test_invalid_utf8_rejected() -> None:
    with pytest.raises(CanonicalJSONError):
        loads_strict(b'{"x":"\xff"}')


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"a": [float("-inf")]}, chr(0xD800)])
def test_dump_rejects_unrepresentable(value: object) -> None:
    with pytest.raises(CanonicalJSONError):
        dumps_canonical(value)


def test_roundtrip_hash_stable_across_key_order() -> None:
    a = loads_strict('{"b":1,"a":[{"d":2,"c":"x"}]}')
    b = loads_strict('{"a":[{"c":"x","d":2}],"b":1}')
    assert canonical_sha256(a) == canonical_sha256(b)


def test_escaped_lone_surrogates_rejected_on_parse() -> None:
    backslash = chr(92)
    lone_value = '{"x": "' + backslash + 'ud800"}'
    lone_key = '{"' + backslash + 'udfff": 1}'
    pair = '{"x": "' + backslash + "ud83d" + backslash + 'ude00"}'
    with pytest.raises(CanonicalJSONError):
        loads_strict(lone_value)
    with pytest.raises(CanonicalJSONError):
        loads_strict(lone_key)
    assert loads_strict(pair) == {"x": chr(0x1F600)}
