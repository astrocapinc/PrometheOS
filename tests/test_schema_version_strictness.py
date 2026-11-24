"""Decision 0014 item 5: version constants are JSON integers only.

``Literal[1]`` admits ``true`` and ``1.0`` (and ``Literal[0]`` admits ``false`` and ``0.0``)
even in strict mode, while the exported JSON contracts say ``const`` plus ``integer`` and
reject them. Every model that carries such a constant must reject the same inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cp1_support import Env, create_entry, request
from cp4_support import write_package
from pydantic import BaseModel, ValidationError

from prometheos import packages
from prometheos.domain.bodies import (
    ExecutionReceiptV1,
    JobBudget,
    ProjectSettings,
    default_settings,
)
from prometheos.domain.models import PrometheosError, Request, SuccessResponse

PROJECT = "project_00000000-0000-4000-8000-000000000001"
REQ = "00000000-0000-4000-8000-000000000002"
ENTRY = "entry_00000000-0000-4000-8000-000000000003"


def _request() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": PROJECT,
        "request_id": REQ,
        "expected_seq": 0,
        "operations": [{"op": "archive", "id": ENTRY, "base_rev": 1}],
    }


def _success() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": 1,
        "project_id": PROJECT,
        "request_id": REQ,
        "project_seq": 1,
        "results": [],
        "replayed": False,
    }


def _settings() -> dict[str, Any]:
    return default_settings("t", "objective", None).model_dump(mode="json")


def _budget() -> dict[str, Any]:
    return {
        "max_inputs": 20,
        "max_output_chars": 2000,
        "max_provider_calls": 0,
        "max_concurrent_runs": 1,
        "spend_cap_estimate_usd": None,
    }


def _receipt() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "observation": "reported",
        "reporter": "agent",
        "command": "run",
        "working_root": None,
        "environment": {"os": "TestOS", "tools": []},
        "started_at": None,
        "ended_at": None,
        "exit_status": 0,
        "outcome": "succeeded",
        "inputs": [],
        "outputs": [],
        "limitations": [],
    }


def _sidecar(tmp_path: Path) -> dict[str, Any]:
    folder = write_package(tmp_path / "pkg")
    data: dict[str, Any] = json.loads((folder / "prometheos-package.json").read_text("utf-8"))
    return data


def _lock() -> dict[str, Any]:
    return {"schema_version": 1, "installs": []}


# (model, field) -> builder of a valid payload. Python-mode validation is what the lock
# loader and the MCP adapter use; JSON-mode is what ``parse_model`` uses for requests.
CASES: list[tuple[str, type[BaseModel], str, Any]] = [
    ("request", Request, "schema_version", _request),
    ("success_response", SuccessResponse, "schema_version", _success),
    ("project_settings", ProjectSettings, "policy_version", _settings),
    ("job_budget", JobBudget, "max_concurrent_runs", _budget),
    ("receipt", ExecutionReceiptV1, "schema_version", _receipt),
    ("lock", packages.Lock, "schema_version", _lock),
]
BAD = [True, 1.0, "1", None, 2, 0]


@pytest.mark.parametrize("bad", BAD, ids=repr)
@pytest.mark.parametrize("name,model,field,build", CASES, ids=[c[0] for c in CASES])
def test_version_constant_rejects_non_integers(
    name: str, model: type[BaseModel], field: str, build: Any, bad: Any
) -> None:
    payload = build()
    assert model.model_validate(payload) is not None  # the valid payload is valid
    payload[field] = bad
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    with pytest.raises(ValidationError):  # the raw-JSON route, as requests arrive
        model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("bad", [True, 1.0, "1", None, 2, 0], ids=repr)
def test_sidecar_version_rejects_non_integers(tmp_path: Path, bad: Any) -> None:
    payload = _sidecar(tmp_path)
    assert packages.Sidecar.model_validate_json(json.dumps(payload)) is not None
    payload["schema_version"] = bad
    with pytest.raises(ValidationError):
        packages.Sidecar.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("bad", [False, 0.0, "0", None, 1], ids=repr)
def test_create_base_rev_rejects_non_integers(bad: Any) -> None:
    op = create_entry(ENTRY)
    assert Request.model_validate_json(json.dumps({**_request(), "operations": [op]}))
    op["base_rev"] = bad
    with pytest.raises(ValidationError):
        Request.model_validate_json(json.dumps({**_request(), "operations": [op]}))


@pytest.mark.parametrize("literal", ["true", "1.0", "1e0"])
def test_apply_rejects_boolean_and_float_schema_versions(tmp_path: Path, literal: str) -> None:
    env = Env(tmp_path)
    good = request(env.project_id, [create_entry(title="Version check")], env.seq()).decode()
    bad = good.replace('"schema_version": 1', f'"schema_version": {literal}')
    assert bad != good
    before = env.seq()
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, bad.encode())
    assert err.value.code == "validation_error"
    assert env.seq() == before
    assert env.svc.apply(env.project_id, good.encode())["replayed"] is False


def test_integer_one_is_still_accepted_everywhere() -> None:
    assert Request.model_validate(_request()).schema_version == 1
    assert SuccessResponse.model_validate(_success()).schema_version == 1
    assert ProjectSettings.model_validate(_settings()).policy_version == 1
    assert JobBudget.model_validate(_budget()).max_concurrent_runs == 1
    assert packages.Lock.model_validate(_lock()).schema_version == 1


def _error() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": "not_found", "message": "m", "retryable": False, "details": {}},
        "project_seq": None,
    }


@pytest.mark.parametrize("bad", [1, 1.0, "true", None, 0], ids=repr)
def test_success_ok_is_the_boolean_true(bad: Any) -> None:
    payload = _success()
    payload["ok"] = bad
    with pytest.raises(ValidationError):
        SuccessResponse.model_validate(payload)
    with pytest.raises(ValidationError):
        SuccessResponse.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("bad", [0, 0.0, "false", None, 1], ids=repr)
def test_error_ok_is_the_boolean_false(bad: Any) -> None:
    from prometheos.domain.models import ErrorResponse

    payload = _error()
    assert ErrorResponse.model_validate(payload) is not None
    payload["ok"] = bad
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate(payload)
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate_json(json.dumps(payload))


def test_ok_booleans_are_still_accepted() -> None:
    assert SuccessResponse.model_validate_json(json.dumps(_success())).ok is True
