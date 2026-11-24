"""The research compass (decision 0113, slice B of the paper pass): where to look next.

A pure, deterministic read. It turns signals already in the record into ranked directions for
the coding agent: results waiting to be read, claims that are stale, conflicting or settling,
papers not yet framed, claims nobody has checked, numbers that need a rerun or are typed by
hand, questions nothing answers yet, and a novelty check per paper. Each direction says why
(the signal and its records), what to do (the skill or tool), and when it is done, in words
code can check.

PrometheOS chooses *what* from the record; the agent chooses *how* and does the searching and
running with its own tools; the researcher decides. Nothing here calls a model, writes a record
or scores a paper.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from prometheos.app.reads import Caller, current_record, project_seq
from prometheos.domain.models import utc_now

DEFAULT_LIMIT = 12
MAX_LIMIT = 50
PER_KIND = 5
ORDER = ("interpret", "resolve", "frame", "verify", "rerun", "trace", "answer", "novelty")
RULE = (
    "Results waiting to be read, then claims that are stale, conflicting or settling, then papers "
    "to frame, claims to verify, numbers to rerun or trace, questions to answer, and a novelty "
    "check per paper. Every direction comes from a record; none is a verdict."
)
NOVELTY_TITLE = "Novelty check"
# A paper checked for novelty this many days ago or less gets no novelty direction (review
# frame-rerun-a R2-A4: a direction that stays after it is done invites repeated searches).
RECHECK_DAYS = 30


def _clip(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _q(text: str, limit: int = 160) -> str:
    """Text taken from a record, quoted: it is research data, never an instruction (review
    frame-rerun-a R2-A3)."""
    return "\u201c" + _clip(text, limit) + "\u201d"


def _paper_claims(deliverable: dict[str, Any]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for q in deliverable.get("questions") or []:
        for c in q.get("claims") or []:
            seen.setdefault(c["claim_id"], c)
    for c in deliverable.get("other_claims") or []:
        seen.setdefault(c["claim_id"], c)
    return list(seen.values())


def _library(conn: sqlite3.Connection, caller: Caller) -> dict[str, Any]:
    count, newest = 0, None
    for (work_id,) in conn.execute(
        "SELECT id FROM records WHERE kind = 'work' AND lifecycle = 'active'"
    ):
        record = current_record(conn, work_id)
        if record is None or not caller.may_see(record["sensitivity"]):
            continue
        if record["body"].get("merged_into"):
            continue
        count += 1
        year = record["body"].get("year")
        if isinstance(year, int) and (newest is None or year > newest):
            newest = year
    return {"works": count, "newest_year": newest}


def _novelty_checks(conn: sqlite3.Connection, caller: Caller, papers: set[str]) -> dict[str, str]:
    """Paper ID -> the newest date a novelty-check note names it: a visible entry whose title
    starts exactly ``Novelty check:`` and whose text names a paper on the map. The date is the
    note's first revision, so a later edit does not move it (review frame-rerun-a R2-A2)."""
    out: dict[str, str] = {}
    for (entry_id,) in conn.execute(
        "SELECT id FROM records WHERE kind = 'entry' AND lifecycle = 'active'"
    ):
        record = current_record(conn, entry_id)
        if record is None or not caller.may_see(record["sensitivity"]):
            continue
        body = record["body"]
        if not str(body.get("title", "")).startswith(NOVELTY_TITLE + ":"):
            continue
        first = conn.execute(
            "SELECT recorded_at FROM revisions WHERE record_id = ? ORDER BY rev LIMIT 1",
            (entry_id,),
        ).fetchone()
        when = str(first[0] if first else "")[:10]
        for word in str(body.get("text", "")).split():
            word = word.strip(".,;:()[]`'\"")
            if word in papers and when > out.get(word, ""):
                out[word] = when
    return out


def _days_between(earlier: str, later: str) -> int:
    from datetime import date

    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def _direction(
    kind: str,
    title: str,
    why: str,
    do: str,
    done_when: str,
    *,
    paper_id: str | None = None,
    refs: list[str] | None = None,
    skill: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "paper_id": paper_id,
        "title": _clip(title, 200),
        "why": why,
        "do": do,
        "done_when": done_when,
        "refs": refs or [],
        "skill": skill,
    }


def view(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    *,
    paper_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    from prometheos.app import continuity, paper_numbers, research_map

    limit = max(1, min(int(limit), MAX_LIMIT))
    built = research_map.view(conn, store_dir, caller, paper_id=paper_id)
    library = _library(conn, caller)
    papers = {str(d["paper_id"]) for d in built.get("deliverables") or []}
    checked = _novelty_checks(conn, caller, papers)
    today = utc_now()[:10]
    found: list[dict[str, Any]] = []

    if paper_id is None:
        for item in continuity.awaiting_queue(conn, caller)[:PER_KIND]:
            found.append(
                _direction(
                    "interpret",
                    f"Read the result {_q(item.get('title') or item['target_id'])}",
                    _clip(str(item.get("reason") or "A finished run waits for interpretation.")),
                    "Compare it with the claim it bears on and record an assessment that cites "
                    "it (inspect-claim-evidence, then capture-research-evidence); the researcher "
                    "confirms.",
                    "It leaves the 'waiting for interpretation' list.",
                    refs=[item["target_id"]],
                    skill="inspect-claim-evidence",
                )
            )

    for d in built.get("deliverables") or []:
        pid, title = d["paper_id"], d["title"]
        claims = _paper_claims(d)
        for c in claims:
            if c["status"] in ("stale", "conflicting", "settling"):
                found.append(
                    _direction(
                        "resolve",
                        f"{c['status'].capitalize()} claim in {_q(title, 80)}: {_q(c['line'])}",
                        _clip(c.get("reason") or c["status"]),
                        "Do not quote it. Find what changed (its evidence's newer versions or the "
                        "challenging assessment), rerun if a number moved, and propose a "
                        "correction or a new assessment for the researcher.",
                        "Its derived status is 'holds' or 'corrected', or the researcher "
                        "withdraws it.",
                        paper_id=pid,
                        refs=[c["claim_id"]],
                        skill="inspect-claim-evidence",
                    )
                )
        if d.get("next") == "framed":
            found.append(
                _direction(
                    "frame",
                    f"Frame {_q(title)}",
                    d.get("headline") or "No question addresses this paper yet.",
                    "Draft its track, questions and claim placements from the researcher's "
                    "plan and evidence documents; show the draft once; record it after a yes.",
                    "The research map no longer says 'Not framed' for it.",
                    paper_id=pid,
                    skill="frame-paper",
                )
            )
        unverified = [c for c in claims if c["status"] == "unverified"]
        if unverified:
            found.append(
                _direction(
                    "verify",
                    f"Verify {len(unverified)} unchecked claim(s) in {_q(title)}",
                    "No assessment checks them yet: "
                    + "; ".join(_q(c["line"], 80) for c in unverified[:3])
                    + ("; ..." if len(unverified) > 3 else ""),
                    "For each, reproduce the number from its cited artefact (or rerun what "
                    "wrote it) and record an assessment citing what you checked. Bind each "
                    "number as a quantity.",
                    "Each claim's derived status is 'holds' (or it is corrected).",
                    paper_id=pid,
                    refs=[c["claim_id"] for c in unverified],
                    skill="inspect-claim-evidence",
                )
            )
        numbers = paper_numbers.audit(conn, store_dir, caller, pid)
        counts = numbers.get("counts") or {}
        stale_runs = counts.get("traced_run_inputs_changed", 0)
        unrun = counts.get("traced_file_only", 0)
        if stale_runs or unrun:
            parts = []
            if stale_runs:
                parts.append(f"{stale_runs} come from a run whose inputs changed since")
            if unrun:
                parts.append(f"{unrun} come from a generated file no recorded run wrote")
            found.append(
                _direction(
                    "rerun",
                    f"Rerun the generators of {_q(title)}",
                    "Of the manuscript's numbers, " + " and ".join(parts) + ".",
                    "Propose the run list to the researcher, run it with their tools, import "
                    "inputs and outputs, and record the run by path.",
                    "The numbers check reads every generated number as traced to a recorded run.",
                    paper_id=pid,
                    refs=[f["version_id"] for f in numbers.get("macro_files") or []],
                    skill="rerun-paper",
                )
            )
        hand = counts.get("typed", 0) + counts.get("hand_macro", 0)
        if hand:
            # Numbers typed in prose and hand-written macros, each with its file and line
            # (review frame-rerun-a R2-F1: a hand macro alone left the list empty).
            where = [
                f"{n['text']} ({n['path']}:{n['line']})"
                for n in numbers.get("numbers") or []
                if n["kind"] == "typed"
            ] + [
                f"\\{m['name']} = {m['value']} (defined at {m['defined_at']['path']}:"
                f"{m['defined_at']['line']}, used {m['uses']} time(s))"
                for m in numbers.get("macros") or []
                if m["trace"] == "hand"
            ]
            found.append(
                _direction(
                    "trace",
                    f"Trace {hand} number(s) typed by hand in {_q(title)}",
                    "Typed by hand: "
                    + "; ".join(_clip(w, 120) for w in where[:3])
                    + ("; ..." if len(where) > 3 else ""),
                    "Propose writing each from the generator script as a macro, or binding it "
                    "to a claim; the researcher decides, nothing is edited unasked.",
                    "The numbers check reads none typed by hand.",
                    paper_id=pid,
                    skill="rerun-paper",
                )
            )
        for q in d.get("questions") or []:
            if not q.get("claims"):
                found.append(
                    _direction(
                        "answer",
                        f"Answer {_q(q.get('title') or q.get('entry_id'))}",
                        f"No claim answers this question of {_q(title)} yet.",
                        "Plan the run or analysis that would answer it, propose it to the "
                        "researcher, then record the result and a claim placed under it.",
                        "A claim answers it on the research map.",
                        paper_id=pid,
                        refs=[str(q.get("entry_id"))],
                        skill="rerun-paper",
                    )
                )
        last = checked.get(pid)
        if last and _days_between(last, today) <= RECHECK_DAYS:
            continue
        newest = library["newest_year"]
        paper = current_record(conn, pid) or {"body": {}}
        stated = paper["body"].get("contribution") or paper["body"].get("objective") or title
        found.append(
            _direction(
                "novelty",
                f"Check novelty for {_q(title)}",
                (f"Last novelty check {last}." if last else "No novelty check is recorded.")
                + f" The library holds {library['works']} work(s)"
                + (f", the newest from {newest}." if newest else ".")
                + f" Stated contribution: {_q(stated, 200)}",
                "Search the literature (your own tools) for work published since "
                + (last or (str(newest) if newest else "the library's newest work"))
                + " that makes this contribution; compare it with this paper's claims. "
                f"Record what you found as a note titled '{NOVELTY_TITLE}: ...' whose text "
                f"names {pid}, citing the works you imported.",
                f"A '{NOVELTY_TITLE}:' note recorded after {last or 'today'} names this "
                f"paper (the direction then rests for {RECHECK_DAYS} days).",
                paper_id=pid,
                skill="research-directions",
            )
        )

    if paper_id is None and built.get("unplaced_claims"):
        unplaced = built["unplaced_claims"]
        found.append(
            _direction(
                "frame",
                f"Place {len(unplaced)} claim(s) under a question",
                "They answer no question on the map: "
                + "; ".join(_q(c["line"], 60) for c in unplaced[:3])
                + ("; ..." if len(unplaced) > 3 else ""),
                "Frame the papers they belong to, or accept the layer's suggestion "
                "('Place it there' on the map).",
                "No claim is unplaced.",
                refs=[c["claim_id"] for c in unplaced],
                skill="frame-paper",
            )
        )

    rank = {k: i for i, k in enumerate(ORDER)}
    found.sort(key=lambda x: rank[x["kind"]])  # stable: record order within a kind
    counts_by_kind = {k: sum(1 for x in found if x["kind"] == k) for k in ORDER}
    return {
        "generation": "deterministic",
        "rule": RULE,
        "project_seq": project_seq(conn),
        "paper_id": paper_id,
        "library": library,
        "counts": counts_by_kind,
        "total": len(found),
        "directions": found[:limit],
        "truncated": len(found) > limit,
    }
