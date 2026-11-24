"""The intelligence layer on the research map (decision 0109).

L1, ``map.link_claim_question``: for a claim in use that answers no question yet, the layer
proposes which of the project's questions it answers. Each subject is one claim; its bundle holds
the claim's statement and the visible questions (ids and titles), pinned by the claim's and the
questions' revisions. Two models of different companies must agree on the question, the id must
be one offered and the quote must be the claim's own words. A proposal changes nothing: the owner
places the claim with one act (an ``answers`` link, as from the hub), and nothing is written by a
model. Restricted claims never reach a remote executor (the run denies them), and questions the
caller may not see are never offered.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from prometheos.adoption import assist
from prometheos.app.claim_status import _rows
from prometheos.app.reads import Caller
from prometheos.domain.canonical import canonical_sha256
from prometheos.intelligence import results as ai_results
from prometheos.intelligence import tasks
from prometheos.intelligence.run import Subject
from prometheos.store.project import ProjectStore

MAX_QUESTIONS = 60
OUT_OF_USE = frozenset({"withdrawn", "superseded"})
TASK_KIND = tasks.LINK_CLAIM_QUESTION.kind


def _questions(conn: sqlite3.Connection, caller: Caller) -> list[dict[str, Any]]:
    """The visible questions offered to the model: normal sensitivity only, so one restricted
    question never makes every claim's bundle restricted. Ordered by title, then id."""
    found = [
        e
        for e in _rows(conn, "entry", caller)
        if e["body"].get("category") == "question" and e["sensitivity"] == "normal"
    ]
    found.sort(key=lambda e: (str(e["body"].get("title")), e["id"]))
    return found[:MAX_QUESTIONS]


def _answering(conn: sqlite3.Connection, claims: dict[str, dict[str, Any]]) -> set[str]:
    """Claims that answer a question: directly, or as the replacement (``superseded_by``, followed
    as far as it goes) of one that does, as the research map lists them (review cp20-2-a F3;
    review map-ai-spread-a R1-F3)."""
    placed = {
        str(r[0])
        for r in conn.execute(
            "SELECT source_id FROM relations WHERE relation = 'answers' AND retired_seq IS NULL"
        )
    }
    for start in list(placed):
        current = start
        while True:
            nxt = ((claims.get(current) or {}).get("body", {}).get("superseded_by") or {}).get("id")
            if not nxt or nxt in placed:
                break
            placed.add(str(nxt))
            current = str(nxt)
    return placed


def _subject_sha256(claim: dict[str, Any], questions: list[dict[str, Any]]) -> str:
    return canonical_sha256(
        {
            "claim": [claim["id"], claim["rev"]],
            "questions": [[q["id"], q["rev"]] for q in questions],
        }
    )


def bundle(claim: dict[str, Any], questions: list[dict[str, Any]]) -> assist.Bundle:
    task = tasks.LINK_CLAIM_QUESTION
    data = {
        "claim": {"statement": str(claim["body"].get("statement") or "")},
        "questions": [
            {"id": q["id"], "title": str(q["body"].get("title") or "")} for q in questions
        ],
        "question_ids": [q["id"] for q in questions],
    }
    return assist.Bundle(
        kind="link_claim_question",
        subject=claim["id"],
        subject_sha256=_subject_sha256(claim, questions),
        input=data,
        input_sha256=canonical_sha256(data),
        template_sha256=task.template_sha256,
    )


def candidates(
    conn: sqlite3.Connection, caller: Caller, statuses: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(claims in use that answer no question, the questions to offer)."""
    visible = {c["id"]: c for c in _rows(conn, "claim", caller)}
    answering = _answering(conn, visible)
    claims = [
        c
        for c in visible.values()
        if c["id"] not in answering and statuses.get(c["id"]) not in OUT_OF_USE
    ]
    claims.sort(key=lambda c: (str(c["body"].get("statement")), c["id"]))
    return claims, _questions(conn, caller)


def subjects(store: ProjectStore, caller: Caller, claim_ids: list[str] | None) -> list[Subject]:
    from prometheos.app import claim_status

    conn = store.connect(readonly=True)
    try:
        statuses = {
            s["claim_id"]: s["status"] for s in claim_status.statuses(conn, store.directory, caller)
        }
        claims, questions = candidates(conn, caller, statuses)
    finally:
        conn.close()
    if claim_ids is not None:
        wanted = set(claim_ids)
        claims = [c for c in claims if c["id"] in wanted]
    if not questions:
        return []

    def reread_for(claim_id: str) -> Any:
        def reread() -> str | None:
            again = store.connect(readonly=True)
            try:
                found = [c for c in _rows(again, "claim", caller) if c["id"] == claim_id]
                now = _questions(again, caller)
            finally:
                again.close()
            return _subject_sha256(found[0], now) if found else None

        return reread

    return [
        Subject(
            bundle=bundle(claim, questions),
            current_digest=_subject_sha256(claim, questions),
            sensitivity=str(claim["sensitivity"]),
            refs=[claim["id"]],
            reread=reread_for(claim["id"]),
        )
        for claim in claims
    ]


def suggestions(
    conn: sqlite3.Connection, caller: Caller, statuses: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Claim -> the layer's proposal for its current bundle: the latest run's own verdict on the
    exact current input, task version, template and wire schema (``ai_verdicts``; review
    map-ai-spread-a R1-F1, R2-F1). Only an ``accepted`` verdict gives a proposal, built from the
    samples that run judged together; ``disagreed`` says the models disagree; any other latest
    verdict gives nothing. A sample from outside the evaluated list makes it a hint (A07.5). A
    changed statement or question list shows no stale proposal."""
    task = tasks.LINK_CLAIM_QUESTION
    claims, questions = candidates(conn, caller, statuses)
    if not claims or not questions:
        return {}
    titles = {q["id"]: str(q["body"].get("title") or "") for q in questions}
    revs = {q["id"]: int(q["rev"]) for q in questions}
    out: dict[str, dict[str, Any]] = {}
    for claim in claims:
        judged = ai_results.latest_judged(conn, task, bundle(claim, questions).input_sha256)
        if judged is None:
            continue
        results: list[dict[str, Any]] = judged.get("results", [])
        if not judged["agree"] or len({str(r["question_id"]) for r in results}) != 1:
            out[claim["id"]] = {"agree": False, "models": judged["models"]}
            continue
        first = results[0]
        out[claim["id"]] = {
            "agree": True,
            "question_id": first["question_id"],
            "question_rev": revs.get(first["question_id"]),
            "question_title": titles.get(first["question_id"]),
            "quote": first["quote"],
            "models": judged["models"],
            "executors": judged["executors"],
            "hint_only": judged["hint_only"],
        }
    return out


__all__ = ["TASK_KIND", "bundle", "candidates", "subjects", "suggestions"]
