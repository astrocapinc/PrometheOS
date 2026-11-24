"""What it means for a record to cite an execution result (A02, CP10.4; decision 0018 round 2).

One definition serves both sides, so they can never disagree:

- the awaiting-interpretation queue (``continuity.citers``) asks which results an assessment or
  reviewed entry cites, to decide whether an item is still waiting;
- the protected effect (``apply``) asks which results a reviewed entry cites, to decide whether
  creating, revising or marking it takes an item out of the queue and so needs the owner.

A record cites a result when its body mentions the result's ID anywhere the reference index
sees it (a typed reference, evidence, a quantity's source, or any text that is exactly the ID),
or when it has a live ``cites`` relation to it. A *result* is an execution record, or the source
or version of an execution receipt.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from prometheos.store.derived import body_refs

Lookup = Callable[[str], tuple[str, dict[str, Any]] | None]
"""``id -> (kind, body)`` for an existing record, else ``None``. Not rank-filtered: whether a
record is a result is a fact about the project, not about who is asking."""


def is_execution_result(kind: str, body: dict[str, Any]) -> bool:
    if kind == "execution":
        return True
    if kind == "version":
        return bool(body.get("payload_schema") == "execution_receipt_v1")
    return bool(kind == "source" and body.get("origin") == "agent_receipt")


def cited_result_ids(
    lookup: Lookup, body: dict[str, Any], cites_targets: Iterable[str]
) -> set[str]:
    """The execution results a record cites: every ID its body mentions (as the reference index
    reads it) plus the targets of its live ``cites`` relations, that is a result."""
    candidates = {to_id for to_id, _rev, _field in body_refs(body)} | set(cites_targets)
    cited: set[str] = set()
    for candidate in candidates:
        found = lookup(candidate)
        if found is not None and is_execution_result(*found):
            cited.add(candidate)
    return cited
