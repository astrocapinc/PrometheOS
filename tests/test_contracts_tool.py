from __future__ import annotations

import shutil
from pathlib import Path

import check_contracts
import pytest
from conftest import REPO


def test_committed_contracts_pass() -> None:
    assert check_contracts.check(write=False) == []


@pytest.fixture
def contracts_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    copy = tmp_path / "contracts"
    shutil.copytree(REPO / "contracts", copy)
    monkeypatch.setattr(check_contracts, "CONTRACTS", copy)
    return copy


def test_drifted_schema_detected(contracts_copy: Path) -> None:
    path = contracts_copy / "schemas" / "request.schema.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert any("differs" in f for f in check_contracts.check(write=False))


def test_invalid_example_that_validates_is_reported(contracts_copy: Path) -> None:
    good = contracts_copy / "examples" / "request" / "valid-create-entry.json"
    shutil.copy(good, contracts_copy / "examples" / "request" / "invalid-sneaky.json")
    failures = check_contracts.check(write=False)
    assert any("invalid-sneaky.json: model accepted" in f for f in failures)


def test_stray_schema_and_missing_examples_detected(contracts_copy: Path) -> None:
    (contracts_copy / "schemas" / "ghost.schema.json").write_text("{}")
    shutil.rmtree(contracts_copy / "examples" / "record_ref")
    failures = check_contracts.check(write=False)
    assert any("stray" in f for f in failures)
    assert any("no valid example for schema record_ref" in f for f in failures)
