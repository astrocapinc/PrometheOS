"""Design-package validator rules added by amendment A02 (decision 0011).

tools/verify_package.py checks the specification package, not the product. Each test
copies the sealed package into a temporary directory, optionally edits a sealed JSON
file and reseals the copy, then runs the validator there as a subprocess.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import PRIVATE_RECORDS, REPO, requires_private_records
from jsonschema import Draft202012Validator


@pytest.fixture
def package(tmp_path: Path) -> Path:
    root = tmp_path / "package"
    manifest = json.loads((REPO / "package-manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        target = root / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / item["path"], target)
    shutil.copyfile(REPO / "package-manifest.json", root / "package-manifest.json")
    if not PRIVATE_RECORDS:
        # A public export (decision 0072): no state or decision records, and its marker.
        (root / "PUBLIC-EXPORT.json").write_text('{"kind": "prometheos-public-export"}', "utf-8")
        return root
    # Sealed documents link decision records; the copy has no receipts, so its state is unstarted.
    shutil.copytree(REPO / "development/decisions", root / "development/decisions")
    state = json.loads((REPO / "development/state.json").read_text(encoding="utf-8"))
    for cp in state["checkpoints"].values():
        cp.update(technical="not_started", review="pending", receipt=None)
    for gate in state["external_gates"].values():
        gate.update(status="not_run", receipt=None, reason=None)
    state["implementation_exists"] = False
    state["public_release_authorized"] = False
    (root / "development/state.json").write_text(json.dumps(state), encoding="utf-8")
    return root


def run_validator(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "tools/verify_package.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def edit_sealed_json(root: Path, rel: str, mutate: Callable[[Any], None]) -> None:
    """Edit a sealed JSON file and reseal the copy so only the edited rule can fail."""
    path = root / rel
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    manifest_path = root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        item["sha256"] = hashlib.sha256((root / item["path"]).read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def test_sealed_package_passes_with_its_declared_counts(package: Path) -> None:
    # The counts follow the sealed version (A02: 17/34; A05: 20/37; A06.1: 21/38; A07: 23/40).
    graph = json.loads((package / "checkpoints.json").read_text(encoding="utf-8"))
    reqs = json.loads((package / "requirements.json").read_text(encoding="utf-8"))
    checkpoints, requirements = len(graph["checkpoints"]), len(reqs["requirements"])
    assert (checkpoints, requirements) == (23, 40)  # spec 1.7.0
    result = run_validator(package)
    assert result.returncode == 0, result.stderr
    assert f"{checkpoints} checkpoints" in result.stdout
    assert f"{requirements} requirements" in result.stdout


def test_checkpoint_from_cp10_on_needs_its_walk_check(package: Path) -> None:
    def drop_walk(graph: dict[str, Any]) -> None:
        cp12 = next(cp for cp in graph["checkpoints"] if cp["id"] == "CP12")
        cp12["required_checks"].remove("C12-WALK")

    edit_sealed_json(package, "checkpoints.json", drop_walk)
    result = run_validator(package)
    assert result.returncode == 1
    assert "Walk check missing: CP12" in result.stderr


def test_receipt_schema_must_admit_every_declared_checkpoint(package: Path) -> None:
    def restore_old_pattern(schema: dict[str, Any]) -> None:
        schema["properties"]["checkpoint_id"]["pattern"] = "^CP[0-8]$"  # the 1.1.0 defect

    edit_sealed_json(package, "schemas/receipt.schema.json", restore_old_pattern)
    result = run_validator(package)
    assert result.returncode == 1
    assert "Receipt schema rejects a declared checkpoint ID" in result.stderr


def test_receipt_schema_checkpoint_pattern_is_exact() -> None:
    schema = json.loads((REPO / "schemas/receipt.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema["properties"]["checkpoint_id"])
    graph = json.loads((REPO / "checkpoints.json").read_text(encoding="utf-8"))
    for checkpoint in graph["checkpoints"]:
        assert validator.is_valid(checkpoint["id"]), checkpoint["id"]
    for undeclared in ("CP23", "CP01", "CP", "cp1", "CP1 ", "CP100"):
        assert not validator.is_valid(undeclared), undeclared


def public_export_copy(package: Path) -> Path:
    """The fixture's package as tools/public_export.py ships it: the marker, the register only."""
    shutil.rmtree(package / "development", ignore_errors=True)
    (package / "development").mkdir()
    (package / "development/dependencies.json").write_text("{}", encoding="utf-8")
    (package / "PUBLIC-EXPORT.json").write_text('{"kind": "prometheos-public-export"}', "utf-8")
    return package


def test_a_public_export_passes_without_the_private_state_and_decisions(package: Path) -> None:
    # Decision 0072: the export has no development/state.json, receipts or decision records.
    result = run_validator(public_export_copy(package))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "state checks skipped" in result.stdout
    assert "link(s) into the private development records not checked" in result.stdout


@requires_private_records  # in the export, the state is legitimately absent
def test_without_the_marker_a_missing_state_or_decision_still_fails(package: Path) -> None:
    public_export_copy(package)
    (package / "PUBLIC-EXPORT.json").unlink()
    result = run_validator(package)
    assert result.returncode != 0
    assert "state.json" in result.stdout + result.stderr


def test_the_marker_never_excuses_a_broken_link_outside_development(package: Path) -> None:
    public_export_copy(package)
    spec = package / "spec/README.md"
    spec.write_text(spec.read_text("utf-8") + "\n[gone](../nowhere.md)\n", "utf-8")
    edit_sealed_json(package, "checkpoints.json", lambda graph: None)
    result = run_validator(package)
    assert result.returncode != 0
    assert "Broken/nonportable link README.md: ../nowhere.md" in result.stdout + result.stderr
