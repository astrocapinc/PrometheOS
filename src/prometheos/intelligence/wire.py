"""The wire schema: what a provider's strict structured output receives (A07.3; decision 0092).

A task's output model is a strict Pydantic model; its JSON schema uses keywords that strict
structured output on some providers rejects or silently ignores (``$ref``/``$defs``, ``oneOf``,
string and number bounds, patterns, formats). The wire schema is derived deterministically into
the accepted subset:

- references are inlined (a recursive model is refused);
- ``oneOf`` becomes ``anyOf``; ``const`` becomes a one-value ``enum``;
- every object lists all its properties as required and forbids others;
- bounds, patterns, formats, defaults, titles and examples are dropped.

The dropped constraints are not lost: the validator always applies the strict model, so the wire
schema only shapes the reply and never decides acceptance. Its digest is part of the cache key,
the receipt and the trust record.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError

KEPT = frozenset({"type", "properties", "required", "items", "enum", "anyOf", "description",
                  "additionalProperties"})  # fmt: skip


def _resolve(node: Any, defs: dict[str, Any], seen: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_resolve(item, defs, seen) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        if name in seen:
            raise PrometheosError("validation_error", f"a recursive output model ({name})", {})
        if name not in defs:
            raise PrometheosError("validation_error", f"an unknown reference ({name})", {})
        return _resolve(defs[name], defs, (*seen, name))
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "oneOf":
            out["anyOf"] = _resolve(value, defs, seen)
        elif key == "const":
            out["enum"] = [value]
        elif key in KEPT:
            out[key] = (
                {k: _resolve(v, defs, seen) for k, v in value.items()}
                if key == "properties"
                else _resolve(value, defs, seen)
            )
    if out.get("type") == "object" or "properties" in out:
        properties = out.get("properties", {})
        out["type"] = "object"
        out["properties"] = properties
        out["required"] = list(properties)
        out["additionalProperties"] = False
    return out


def wire_schema(model: type[BaseModel]) -> dict[str, Any]:
    document = model.model_json_schema()
    defs = document.pop("$defs", {})
    resolved = _resolve(document, defs, ())
    if not isinstance(resolved, dict):
        raise PrometheosError("validation_error", "an output model must be an object", {})
    return resolved


def wire_sha256(model: type[BaseModel]) -> str:
    return canonical_sha256(wire_schema(model))
