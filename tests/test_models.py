from __future__ import annotations

import json
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from prometheos.domain import ids
from prometheos.domain.canonical import CanonicalJSONError
from prometheos.domain.models import (
    EXIT_CODES,
    MAX_INT,
    ErrorCode,
    EvidenceRef,
    PrometheosError,
    RecordEnvelope,
    Request,
    parse_model,
)

P = "project_00000000-0000-4000-8000-000000000001"
R = "00000000-0000-4000-8000-000000000002"
E = "entry_00000000-0000-4000-8000-000000000003"


def request(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "project_id": P,
        "request_id": R,
        "expected_seq": 0,
        "operations": [{"op": "archive", "id": E, "base_rev": 1}],
    }
    base.update(overrides)
    return base


def parse(value: dict[str, Any]) -> Request:
    return Request.model_validate_json(json.dumps(value), strict=True)


def test_valid_request_parses() -> None:
    assert parse(request()).operations[0].op == "archive"


@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": 2},
        {"project_id": "project_not-a-uuid"},
        {"project_id": "entry_00000000-0000-4000-8000-000000000001"},
        {"request_id": "00000000-0000-1000-8000-000000000002"},  # not UUID4
        {"expected_seq": -1},
        {"expected_seq": 1.0},
        {"expected_seq": True},
        {"operations": [{"op": "archive", "id": E, "base_rev": 0}]},
        {"operations": [{"op": "archive", "id": E, "base_rev": 1, "confirmed": True}]},
        {"operations": [{"op": "archive", "id": E, "base_rev": 1}] * 201},
        {"extra": 1},
    ],
)
def test_invalid_requests_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(request(**bad))


def test_nested_unknown_fields_rejected_in_evidence_locator() -> None:
    op = {
        "op": "transition",
        "id": E,
        "base_rev": 1,
        "to": "ready",
        "reason": None,
        "evidence": [
            {
                "source_id": "source_00000000-0000-4000-8000-000000000005",
                "version_id": "version_00000000-0000-4000-8000-000000000006",
                "locator": {"type": "text", "start_line": 1, "end_line": 2, "col": 3},
                "quote_sha256": None,
            }
        ],
    }
    with pytest.raises(ValidationError):
        parse(request(operations=[op]))


def test_nonfinite_bbox_rejected() -> None:
    raw = (
        '{"source_id":"source_00000000-0000-4000-8000-000000000005",'
        '"version_id":"version_00000000-0000-4000-8000-000000000006",'
        '"locator":{"type":"pdf","page":1,"bbox":[0,0,1,Infinity]},"quote_sha256":null}'
    )
    with pytest.raises(ValidationError):
        EvidenceRef.model_validate_json(raw, strict=True)


def test_id_helpers() -> None:
    new = ids.new_id("claim")
    assert ids.is_record_id(new, "claim")
    assert not ids.is_record_id(new, "entry")
    assert ids.kind_of(new) == "claim"
    assert not ids.is_record_id(new.upper())
    assert not ids.is_record_id(new + "\n")
    with pytest.raises(ValueError):
        ids.new_id("widget")
    assert ids.is_request_id(ids.new_request_id())


def test_every_error_code_has_exit_code_and_unknown_codes_fail() -> None:
    assert set(get_args(ErrorCode)) == set(EXIT_CODES)
    assert PrometheosError("busy", "x").exit_code == 6
    response = PrometheosError("revision_conflict", "stale", {"project_seq": 4}).to_response(4)
    assert response.error.retryable is True
    with pytest.raises(ValueError):
        PrometheosError("nope", "x")


@pytest.mark.parametrize(
    "raw",
    [
        '{"x": NaN}',
        '{"x": [1, Infinity]}',
        '{"x": 1e999}',
        '{"nested": {"x": -Infinity}}',
    ],
)
def test_nonfinite_values_inside_bodies_rejected_by_model_itself(raw: str) -> None:
    envelope = (
        '{"id":"entry_00000000-0000-4000-8000-000000000003","kind":"entry","rev":1,'
        '"recorded_at":"2026-10-04T12:00:00Z","occurred_at":null,"sensitivity":"normal",'
        f'"lifecycle":"active","body":{raw}}}'
    )
    with pytest.raises(ValidationError):
        RecordEnvelope.model_validate_json(envelope, strict=True)
    with pytest.raises((ValidationError, CanonicalJSONError)):
        parse_model(RecordEnvelope, envelope)


def test_duplicate_keys_and_lone_surrogates_rejected_at_entry_point() -> None:
    dup = json.dumps(request())[:-1] + ', "expected_seq": 5}'
    with pytest.raises(CanonicalJSONError):
        parse_model(Request, dup)
    escaped_lone_surrogate = '"x": "' + chr(92) + 'ud800"'
    surrogate = json.dumps(request()).replace(
        '"archive"', '"archive", ' + escaped_lone_surrogate, 1
    )
    with pytest.raises(CanonicalJSONError):
        parse_model(Request, surrogate)


def test_integers_are_bounded_for_json_clients() -> None:
    with pytest.raises(ValidationError):
        parse(request(expected_seq=MAX_INT + 1))
    assert parse(request(expected_seq=MAX_INT)).expected_seq == MAX_INT


def test_nested_integers_in_bodies_are_bounded() -> None:
    envelope = {
        "id": E,
        "kind": "entry",
        "rev": 1,
        "recorded_at": "2026-10-04T12:00:00Z",
        "occurred_at": None,
        "sensitivity": "normal",
        "lifecycle": "active",
        "body": {"deep": [{"n": MAX_INT + 1}]},
    }
    with pytest.raises(ValidationError):
        parse_model(RecordEnvelope, json.dumps(envelope))
    envelope["body"] = {"deep": [{"n": -MAX_INT}], "flag": True}
    assert parse_model(RecordEnvelope, json.dumps(envelope)).body["flag"] is True
