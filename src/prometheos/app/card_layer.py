"""The intelligence layer on decision cards (decision 0110, L3: ``cards.word``).

A card is written by the agent that asked for the decision. The layer proposes plain wording for
its ``what``, ``why`` and ``recommendation`` from the card's facts: what approving it does (the
effects PrometheOS records), the claims the plan names (and a correction's replacements), each
with its bound quantities offered as ids, and the agent's own card text. Guardrails (D16; spec/00
A07):

- a number may appear only as a placeholder ``{q:<id>}`` of an offered quantity, and code fills
  it with the bound value (``unbound_number`` otherwise);
- the output has no default fields, so ``default_text`` and ``default_at`` are never touched (a
  reply that sets them fails the shape check), and the card itself is never rewritten;
- the wording is a labelled hint shown beside the agent's words; the plan stays the
  recommendation, and approving still goes through the same preview.

A card the caller may not see is never worded for it, and a restricted card or claim never
reaches a remote executor (the run denies it).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from prometheos.adoption import assist
from prometheos.app import cards
from prometheos.app.reads import Caller, current_record
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import SENSITIVITY_ORDER
from prometheos.intelligence import results as ai_results
from prometheos.intelligence import tasks
from prometheos.intelligence.run import Subject
from prometheos.intelligence.validate import QUANTITY
from prometheos.store.project import ProjectStore

TASK_KIND = tasks.CARD_WORDING.kind
RANK_NAME = {rank: name for name, rank in SENSITIVITY_ORDER.items()}
MAX_CLAIMS = 10
CARD_FIELDS = ("what", "why", "recommendation")


def _claims(conn: sqlite3.Connection, caller: Caller, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The visible claims the plan names, then those a visible named correction targets and
    replaces (a correction the caller may not see is not followed; review R1-F8)."""
    named: list[str] = []
    cards._ids_named(plan.get("operations", []), named)
    ids: list[str] = []
    for rid in named:
        if not rid.startswith("correction_"):
            ids.append(rid)
            continue
        correction = current_record(conn, rid)
        if correction is None or not caller.may_see(correction["sensitivity"]):
            continue
        body = correction["body"]
        for ref in [*body.get("target_refs", []), *body.get("replacement_refs", [])]:
            if str(ref.get("id", "")).startswith("claim_"):
                ids.append(str(ref["id"]))
    ids = list(dict.fromkeys(ids))
    out = []
    for cid in ids[:MAX_CLAIMS]:
        claim = current_record(conn, cid)
        if claim is not None and caller.may_see(claim["sensitivity"]):
            out.append(claim)
    return out


def facts(
    conn: sqlite3.Connection, caller: Caller, row: Any, plan: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """(the bundle input, quantity id -> its written form, the claims used)."""
    from prometheos.app.research_map import _line

    claims = _claims(conn, caller, plan)
    written: dict[str, str] = {}
    listed = []
    for claim in claims:
        offered = []
        for binding in claim["body"].get("quantities") or []:
            qid = f"q{len(written) + 1}"
            unit = str(binding["unit"])
            written[qid] = f"{binding['decimal']} {unit}" if unit else str(binding["decimal"])
            offered.append({"id": qid, "name": binding.get("name") or "", "unit": unit})
        listed.append({"claim": _line(claim["body"]), "quantities": offered})
    card = plan["card"]
    data = {
        "effects": cards._effects(row[3]),
        "claims": listed,
        "agent_card": {field: str(card.get(field) or "") for field in CARD_FIELDS},
        "quantities": list(written),
    }
    return data, written, claims


def _digest(row: Any, claims: list[dict[str, Any]]) -> str:
    return canonical_sha256(
        {"card": row[0], "plan": row[2], "claims": [[c["id"], c["rev"]] for c in claims]}
    )


def bundle(row: Any, data: dict[str, Any], claims: list[dict[str, Any]]) -> assist.Bundle:
    task = tasks.CARD_WORDING
    return assist.Bundle(
        kind="card_wording",
        subject=str(row[0]),
        subject_sha256=_digest(row, claims),
        input=data,
        input_sha256=canonical_sha256(data),
        template_sha256=task.template_sha256,
    )


def _sensitivity(row: Any, claims: list[dict[str, Any]]) -> str:
    rank = max([int(row[7]), *(SENSITIVITY_ORDER[c["sensitivity"]] for c in claims)])
    return RANK_NAME[rank]


def _wanted(conn: sqlite3.Connection, plan: dict[str, Any], claim_ids: list[str] | None) -> bool:
    return claim_ids is None or bool(set(cards._claims_named(conn, plan)) & set(claim_ids))


def subjects(store: ProjectStore, caller: Caller, claim_ids: list[str] | None) -> list[Subject]:
    conn = store.connect(readonly=True)
    try:
        found = []
        for row, plan in cards.pending(conn, caller.rank):
            if not _wanted(conn, plan, claim_ids):
                continue
            data, _written, claims = facts(conn, caller, row, plan)
            found.append((row, bundle(row, data, claims), _sensitivity(row, claims), claims))
    finally:
        conn.close()

    def reread_for(approval_id: str) -> Any:
        def reread() -> str | None:
            again = store.connect(readonly=True)
            try:
                for row, plan in cards.pending(again, caller.rank):
                    if row[0] == approval_id:
                        return _digest(row, facts(again, caller, row, plan)[2])
            finally:
                again.close()
            return None

        return reread

    return [
        Subject(
            bundle=built,
            current_digest=built.subject_sha256,
            sensitivity=level,
            refs=[str(row[0]), *(c["id"] for c in claims)],
            reread=reread_for(str(row[0])),
        )
        for row, built, level, claims in found
    ]


def fill(text: str, written: dict[str, str]) -> str:
    """Replace each placeholder with the bound value it names (validated: only offered ids)."""
    return QUANTITY.sub(lambda m: written.get(m.group(1), m.group(0)), text)


def wordings(conn: sqlite3.Connection, caller: Caller) -> dict[str, dict[str, Any]]:
    """Card -> the layer's wording for its current facts, placeholders filled by code, or
    nothing. Read-only: the card, its default and its date are returned untouched elsewhere."""
    out: dict[str, dict[str, Any]] = {}
    for row, plan in cards.pending(conn, caller.rank):
        data, written, claims = facts(conn, caller, row, plan)
        judged = ai_results.latest_judged(
            conn, tasks.CARD_WORDING, bundle(row, data, claims).input_sha256
        )
        if judged is None or not judged["agree"]:
            continue
        result = judged["results"][0]
        out[str(row[0])] = {
            **{field: fill(str(result[field]), written) for field in CARD_FIELDS},
            "models": judged["models"],
            "executors": judged["executors"],
            "hint_only": True,  # wording is never a decision (A07.7): always a hint
        }
    return out


__all__ = ["TASK_KIND", "bundle", "facts", "fill", "subjects", "wordings"]
