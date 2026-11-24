"""Check exported public contracts against committed schemas and examples.

Usage:
    uv run python tools/check_contracts.py          # verify
    uv run python tools/check_contracts.py --write  # regenerate committed schemas

Rules:
- Every registered schema is regenerated and must equal ``contracts/schemas/<name>.schema.json``;
  no stray committed schema files are allowed. Each schema must be valid Draft 2020-12.
- ``contracts/examples/<name>/valid-*.json`` must parse strictly and validate with both
  the Pydantic model and the exported JSON Schema.
- ``invalid-parse-*.json`` must be rejected by the strict parser (duplicate keys, NaN...),
  which is the first step of ``parse_model``, the only entry point for untrusted JSON.
- ``invalid-semantic-*.json`` must be rejected by the Pydantic model (cross-field rules
  JSON Schema cannot express). JSON Schema acceptance is reported, not required.
- Other ``invalid-*.json`` must be rejected by both the model and the JSON Schema.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from prometheos.domain.canonical import CanonicalJSONError, loads_strict
from prometheos.domain.models import parse_model
from prometheos.schemas import REGISTRY, schema_for, schema_text

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def check(write: bool) -> list[str]:
    failures: list[str] = []
    schema_dir = CONTRACTS / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    expected_files = {f"{name}.schema.json" for name in REGISTRY}
    for stray in sorted(p.name for p in schema_dir.glob("*.json")):
        if stray not in expected_files:
            failures.append(f"stray committed schema: contracts/schemas/{stray}")
    counts = {"schemas": 0, "valid": 0, "invalid": 0}
    for name, model in sorted(REGISTRY.items()):
        text = schema_text(name)
        path = schema_dir / f"{name}.schema.json"
        if write:
            path.write_text(text, encoding="utf-8", newline="\n")
        elif not path.exists() or path.read_text(encoding="utf-8") != text:
            failures.append(f"generated schema differs from committed copy: {name}")
        schema = schema_for(name)
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            failures.append(f"schema {name} is not valid Draft 2020-12: {exc}")
            continue
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        counts["schemas"] += 1
        example_dir = CONTRACTS / "examples" / name
        examples = sorted(example_dir.glob("*.json")) if example_dir.exists() else []
        if not any(p.name.startswith("valid-") for p in examples):
            failures.append(f"no valid example for schema {name}")
        if not any(p.name.startswith("invalid-") for p in examples):
            failures.append(f"no invalid example for schema {name}")
        for example in examples:
            label = f"{name}/{example.name}"
            raw = example.read_bytes()
            try:
                value = loads_strict(raw)
                parse_error = None
            except CanonicalJSONError as exc:
                value, parse_error = None, str(exc)
            if example.name.startswith("invalid-parse-"):
                counts["invalid"] += 1
                if parse_error is None:
                    failures.append(f"{label}: strict parser accepted invalid JSON")
                continue
            if parse_error is not None:
                failures.append(f"{label}: strict parse failed: {parse_error}")
                continue
            try:
                parse_model(model, raw)
                model_ok = True
            except (ValidationError, CanonicalJSONError):
                model_ok = False
            schema_ok = not list(validator.iter_errors(value))
            if example.name.startswith("valid-"):
                counts["valid"] += 1
                if not model_ok:
                    failures.append(f"{label}: model rejected valid example")
                if not schema_ok:
                    failures.append(f"{label}: JSON Schema rejected valid example")
            elif example.name.startswith("invalid-semantic-"):
                counts["invalid"] += 1
                if model_ok:
                    failures.append(f"{label}: model accepted semantically invalid example")
            elif example.name.startswith("invalid-"):
                counts["invalid"] += 1
                if model_ok:
                    failures.append(f"{label}: model accepted invalid example")
                if schema_ok:
                    failures.append(f"{label}: JSON Schema accepted invalid example")
            else:
                failures.append(f"{label}: example name must start with valid- or invalid-")
    if not failures:
        print(
            f"PASS: {counts['schemas']} schemas match committed copies; "
            f"{counts['valid']} valid and {counts['invalid']} invalid examples behave as declared."
        )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="regenerate committed schemas")
    args = parser.parse_args(argv)
    failures = check(write=args.write)
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
