"""C11-WALK: the CP11 end-to-end walk (spec/10 CP11.4; checkpoints.json C11-WALK).

    uv run python tools/walk_cp11.py [--out DIR] [--print-transcript] [--only hub|mcp] [--keep]

The researcher's literature work on the built app, from a fresh store made from the synthetic
fixtures (``tools/synthetic_pdf.py``, ``tools/synthetic_bib.py`` and the Zotero stub
``tools/zotero_stub.py``; nothing leaves the PC):

1. **Hub half (store A).** The real hub (the assets shipped in the package) serves a project that
   has no research folder. A Playwright spec (``hub/walk11/walk.spec.ts``, Playwright's Chromium
   headless shell inside the loopback-only boundary of decision 0028, proved first by
   ``hub/walk11/00-boundary.spec.ts``) drives the owner: M1.1 adds the papers folder, M1.2 imports
   its PDFs and bibliography, M1.3 imports selected Zotero items, M1.4 searches and reads a page,
   M1.5 builds a source-linked matrix over three works (full text, full text and an
   abstract-only Zotero item), M1.6 classifies by rules, M1.7 classifies one work by a manual
   choice label, M1.8 cites a verified span (two misquotes are refused verbatim) and M1.9
   reviews. While the hub keeps running, an agent session over the real
   ``prometheos mcp serve --stdio`` process (the official MCP SDK client) reads the same matrix
   (A1), records an assumption and a finding relying on it (A2) and proposes a correction (A3).
   The owner then exports the brief (M2.1), applies the correction (M2.2: the row and the brief
   are flagged) and re-reviews the finding (M2.3). A last agent session reads the outcome (A4).
2. **MCP half (store B).** On a second fresh store, with the hub stopped, the same script runs
   through MCP alone: the owner's steps go through the service in the harness (a synthetic owner
   channel, as in C10-WALK and C18-WALK), every agent step is the same MCP call.
3. The two stores' normalised outcomes (works with their provenance, retained item versions,
   relations, the matrix with its cells, rows and briefs, entries, corrections, approvals, runs,
   decisions; IDs, digests and times normalised), every step's outcome and the normalised answer
   of every agent step must be equal. The Zotero stub must have seen only GET requests, without an
   Origin header or a key.

Output in ``--out`` (a new folder): ``transcript.json`` and ``transcript.md`` (each step, the
operation, response digests and store assertions), ``outcome-hub.json`` and ``outcome-mcp.json``,
``evidence/`` (the raw responses), ``zotero-requests.jsonl``, ``browser-boundary.json``,
``playwright-report.json`` and the browser's screenshots. Exit 0 only when every assertion passed
and the outcomes are equal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_bib as bibs  # noqa: E402
import synthetic_pdf as pdfs  # noqa: E402
from browser_shell import (  # noqa: E402
    BrowserUnavailable,
    playwright_command,
    playwright_environment,
    require_headless_shell,
)
from walk_cp10 import (  # noqa: E402
    boundary_evidence,
    canon,
    check,
    digest,
    first_difference,
    hub_stopped_note,
)
from walk_cp18 import SHA256, STAMP, UUID, McpChat, outcome_of  # noqa: E402
from zotero_stub import ZoteroStub  # noqa: E402

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

SENTENCE = "The drag coefficient was 0.0042 at 10 Pa."
LOSSES = "Losses scale with p^0.8."
REVIEW_SENTENCE = "We review kinetic energy recovery systems."
COLLECTION = "Flywheels"
DESELECT = "PREP0002"  # the preprint stays out of the library
COLUMNS: list[tuple[str, str, str | None]] = [
    ("Method", "choice", "experiment, simulation, review"),
    ("Pressure", "quantity", None),
    ("Drag coefficient", "quantity", None),
    ("Key finding", "text", None),
]
RULES = {
    "method_text": "was evacuated",
    "method_value": "experiment",
    "pressure_text": "evacuated to {value}",
    "pressure_unit": "Pa",
    "finding_text": "reduces windage loss by {value} %",
}
RULE_SET = {
    "rules": [
        {
            "id": "method-1",
            "column": "method",
            "keywords": [RULES["method_text"]],
            "value": RULES["method_value"],
        },
        {
            "id": "pressure-1",
            "column": "pressure",
            "pattern": RULES["pressure_text"],
            "unit": RULES["pressure_unit"],
        },
        {"id": "key-finding-1", "column": "key-finding", "pattern": RULES["finding_text"]},
    ]
}
TITLES = {
    "drag": "Aerodynamic",
    "kers": "Kinetic",
    "bearing": "Bearing Drag in Rarefied Gas",
}

# Every step of the script, in order. ``owner`` steps run in the browser on the hub half and
# through the service (a synthetic owner channel) on the MCP half; ``agent`` steps are the same
# MCP call on both halves.
STEPS: list[dict[str, Any]] = [
    {"id": "M1.1", "actor": "owner", "operation": "add the papers folder"},
    {"id": "M1.2", "actor": "owner", "operation": "import the PDFs and the bibliography"},
    {"id": "M1.3", "actor": "owner", "operation": "import selected Zotero items (read-only)"},
    {"id": "M1.4", "actor": "owner", "operation": "search the page text and read a page"},
    {"id": "M1.5", "actor": "owner", "operation": "create a matrix over three works"},
    {"id": "M1.6", "actor": "owner", "operation": "classify by rules"},
    {"id": "M1.7", "actor": "owner", "operation": "classify one work by a manual choice"},
    {"id": "M1.8", "actor": "owner", "operation": "cite a verified span (misquotes refused)"},
    {"id": "M1.9", "actor": "owner", "operation": "review the cited cell"},
    {"id": "A1", "actor": "agent", "operation": "read the library and the same matrix"},
    {"id": "A2", "actor": "agent", "operation": "record an assumption and a finding relying on it"},
    {"id": "A3", "actor": "agent", "operation": "propose a correction"},
    {"id": "M2.1", "actor": "owner", "operation": "export the brief"},
    {"id": "M2.2", "actor": "owner", "operation": "apply the correction; row and brief flagged"},
    {"id": "M2.3", "actor": "owner", "operation": "re-review the finding; the row's flag clears"},
    {"id": "A4", "actor": "agent", "operation": "read the outcome"},
]
SCRIPT_IDS = [s["id"] for s in STEPS]
AGENT_IDS = [s["id"] for s in STEPS if s["actor"] == "agent"]


# ----- a fresh store -------------------------------------------------------------------------


class Store:
    """A fresh project with NO research folder, a papers folder on disk, the Zotero stub's port
    in the home's settings, and the walk's names for its IDs."""

    def __init__(self, work: Path, zotero_port: int) -> None:
        from prometheos.app.reads import Caller
        from prometheos.app.service import Service

        self.base = work
        self.papers = work / "papers"
        pdfs.write_folder(
            self.papers, {"drag.pdf": pdfs.paper(pdfs.DRAG), "kers.pdf": pdfs.paper(pdfs.KERS)}
        )
        bibs.write_folder(self.papers, {"refs.bib": bibs.REFS_BIB.encode("utf-8")})
        self.home = work / "home"
        self.svc = Service(home=self.home, actor="hub")
        self.owner = Caller(actor="hub", scope="owner")
        self.pid: str = self.svc.init_project(
            title="Synthetic flywheel literature (C11-WALK)",
            objective="Compare flywheel losses across papers, every value quoted from its source.",
            timezone="Europe/London",
        )["project_id"]
        (self.home / "zotero.json").write_text(json.dumps({"port": zotero_port}), "utf-8")
        self.names: dict[str, str] = {}

    def name(self, record_id: str, label: str) -> str:
        if record_id:
            self.names[record_id] = label
        return record_id

    def ids(self) -> dict[str, str]:
        return {label: rid for rid, label in self.names.items()}

    def norm(self, value: Any) -> Any:
        """IDs to their walk names, other times, UUIDs and digests to placeholders."""
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        for rid in sorted(self.names, key=len, reverse=True):
            text = text.replace(rid, f"<{self.names[rid]}>")
        text = STAMP.sub("<time>", text)
        text = SHA256.sub("<sha256>", text)
        text = UUID.sub("<uuid>", text)
        return json.loads(text)

    def db_path(self) -> Path:
        return Path(self.svc.open(self.pid).db_path)

    def rows(self, sql: str) -> list[sqlite3.Row]:
        uri = f"file:{self.db_path().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(sql))

    def works(self) -> list[dict[str, Any]]:
        listed: dict[str, Any] = self.svc.library_works(self.pid, self.owner, limit=200)
        return list(listed["works"])

    def work(self, label: str) -> dict[str, Any]:
        """The work a title key names (``drag``, ``kers``, ``bearing``)."""
        start = TITLES[label]
        return next(w for w in self.works() if w["title"].startswith(start))

    def name_works(self) -> None:
        for w in self.works():
            self.name(w["work_id"], "work:" + w["title"])

    def view(self) -> dict[str, Any]:
        shown: dict[str, Any] = self.svc.matrix_show(self.pid, self.owner, self.ids()["matrix"])
        return shown

    def row(self, label: str) -> dict[str, Any]:
        work_id = self.work(label)["work_id"]
        return next(r for r in self.view()["rows"] if r["work_id"] == work_id)

    def cell(self, label: str, column: str) -> dict[str, Any] | None:
        return next((c for c in self.row(label)["cells"] if c["column_id"] == column), None)


# ----- the normalised outcome of a store -----------------------------------------------------


def snapshot(st: Store) -> dict[str, Any]:
    """Works with provenance, retained item versions, relations, the matrix (rows, cells, briefs),
    entries, corrections, decisions, approvals, runs and the project's roots, normalised."""
    out: dict[str, Any] = {}
    works = st.svc.list_records(st.pid, st.owner, kind="work", limit=200)["items"]
    out["works"] = [
        {
            "title": r["body"]["title"],
            "rev": r["rev"],
            "access_state": r["body"]["access_state"],
            "identifiers": r["body"]["identifiers"],
            "year": r["body"]["year"],
            "venue": r["body"]["venue"],
            "entry_type": r["body"].get("entry_type"),
            "contributors": r["body"]["contributors"],
            "full_text_pages": len(r["body"]["full_text_refs"]),
            "abstract": (r["body"]["abstract_ref"] or {}).get("locator"),
            "merged_into": (r["body"]["merged_into"] or {}).get("id"),
            "provenance": sorted(
                canon([p["basis"], p.get("entry"), p["field"], p.get("value"), p.get("locator")])
                for p in r["body"]["field_provenance"]
            ),
        }
        for r in works
    ]
    versions = st.svc.list_records(st.pid, st.owner, kind="version", limit=200)["items"]
    sources = st.svc.list_records(st.pid, st.owner, kind="source", limit=200)["items"]
    origin = {s["id"]: (s["body"]["origin"], s["body"]["title"]) for s in sources}
    out["retained"] = [
        # digests of the retained bytes: the same synthetic bytes on both stores (a prefix, so the
        # 64-digit normaliser does not hide them). A brief names the store's own record IDs, so
        # only its length is compared.
        [
            origin[v["body"]["source_id"]][0],
            origin[v["body"]["source_id"]][1][:60],
            v["body"]["mime"],
            ""
            if origin[v["body"]["source_id"]][1].startswith("Brief:")
            else v["body"]["sha256"][:16],
            v["body"]["byte_length"],
        ]
        for v in versions
        if v["body"]["source_id"] in origin
    ]
    out["relations"] = [
        [r["relation"], r["source_id"], r["target_id"]]
        for r in st.rows(
            "SELECT relation, source_id, target_id FROM relations WHERE retired_seq IS NULL"
        )
    ]
    out["decisions"] = [
        [r["choice"], r["work_a"], r["work_b"]]
        for r in st.rows("SELECT choice, work_a, work_b FROM library_decisions")
    ]
    entries = st.svc.list_records(st.pid, st.owner, kind="entry", limit=200)["items"]
    out["entries"] = [
        [r["body"]["title"], r["body"]["category"], r["body"]["review"], r["lifecycle"]]
        for r in entries
    ]
    corrections = st.svc.list_records(st.pid, st.owner, kind="correction", limit=200)["items"]
    out["corrections"] = [[r["body"]["status"], r["body"]["reason"]] for r in corrections]
    if "matrix" in st.ids():
        view = st.view()
        out["matrix"] = {
            "columns": view["matrix"]["columns"],
            "rubric_version": view["matrix"]["rubric_version"],
            "rows": sorted(
                (
                    {
                        "work": st.names.get(r["work_id"], "<other>"),
                        "access_state": r["access_state"],
                        "needs_review": r["needs_review"],
                        "cells": r["cells"],
                    }
                    for r in view["rows"]
                ),
                key=lambda r: r["work"],
            ),
            "briefs": view["briefs"],
            "counts": view["counts"],
        }
    out["approvals"] = [
        [r["operation"], r["state"], r["caller_rank"]]
        for r in st.rows("SELECT operation, state, caller_rank FROM approval_requests")
    ]
    out["runs"] = [
        [r["operation"], r["state"], r["caller_rank"]]
        for r in st.rows("SELECT operation, state, caller_rank FROM operation_runs")
    ]
    out["roots"] = [r["alias"] for r in st.svc.project_roots(st.pid, st.owner)["roots"]]
    out["project_seq"] = st.svc.project_info(st.pid, st.owner)["project_seq"]
    return settle(st.norm(out))


UNORDERED = (
    "works",
    "retained",
    "relations",
    "decisions",
    "entries",
    "corrections",
    "approvals",
    "runs",
)


def settle(normalised: dict[str, Any]) -> dict[str, Any]:
    """Sort the sets by their normalised content (order-free comparison; raw IDs mean nothing)."""
    out = dict(normalised)
    for key in UNORDERED:
        out[key] = sorted(out[key], key=canon)
    return out


# ----- the transcript ----------------------------------------------------------------------


class Recorder:
    def __init__(self, store: str, transport: str, st: Store, evidence: Path) -> None:
        self.store, self.transport, self.st = store, transport, st
        self.evidence = evidence
        evidence.mkdir(parents=True, exist_ok=True)
        self.steps: list[dict[str, Any]] = []
        self.failures: list[str] = []

    def add(
        self,
        step_id: str,
        via: str,
        request: Any,
        response: Any,
        outcome: str,
        assertions: list[dict[str, Any]],
    ) -> None:
        meta = next(s for s in STEPS if s["id"] == step_id)
        answer = response
        if isinstance(answer, dict) and answer.get("ok") is False and "error" in answer:
            answer = answer["error"]
        normalised = self.st.norm(answer)
        (self.evidence / f"{step_id}.json").write_text(
            json.dumps(response, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
        (self.evidence / f"{step_id}.normalised.json").write_text(
            json.dumps(normalised, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
        if not assertions:
            assertions = [check("the step recorded at least one store assertion", False)]
        for a in assertions:
            if not a["passed"]:
                self.failures.append(
                    f"{self.transport} {step_id}: {a['claim']} ({a.get('observed')})"
                )
        self.steps.append(
            {
                "id": step_id,
                "actor": meta["actor"],
                "via": via,
                "operation": meta["operation"],
                "request": self.st.norm(request),
                "outcome": outcome,
                "response_sha256": digest(response),
                "normalised_response_sha256": digest(normalised),
                "store_assertions": assertions,
            }
        )

    def ordered(self) -> list[dict[str, Any]]:
        return sorted(self.steps, key=lambda s: SCRIPT_IDS.index(s["id"]))


# ----- the agent (MCP, official SDK client) -----------------------------------------------------


def entry_body(title: str, text: str) -> dict[str, Any]:
    return {
        "title": title,
        "text": text,
        "category": "assumption",
        "evidence": [],
        "claim_refs": [],
        "author_ref": None,
        "origin": "agent",
        "quantities": [],
        "review": "unreviewed",
    }


def create_op(kind: str, record_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "op": "create",
        "kind": kind,
        "id": record_id,
        "base_rev": 0,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": body,
    }


def correction_body(target: str, replacement: str) -> dict[str, Any]:
    return {
        "target_refs": [{"id": target, "rev": 1}],
        "replacement_refs": [{"id": replacement, "rev": 1}],
        "reason": "the loss model was refitted",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": target, "rev": 1},
                "effect": "supersede",
                "replacement_ref": {"id": replacement, "rev": 1},
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }


def without_nulls(value: Any) -> Any:
    """MCP's closed output models spell an absent member as ``null``; the service omits it."""
    if isinstance(value, dict):
        return {k: without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [without_nulls(v) for v in value]
    return value


def view_digest(st: Store, view: dict[str, Any]) -> str:
    return digest(st.norm(without_nulls(view)))


async def agent_session(st: Store, rec: Recorder) -> dict[str, str]:
    """A1-A3: the agent reads the library and the matrix, records an assumption and a finding
    relying on it, and proposes a correction. Returns the names the hub half's browser needs."""
    from prometheos.domain.ids import new_id, new_request_id

    async with McpChat(st) as chat:
        matrix_id = st.ids()["matrix"]
        drag_id = st.work("drag")["work_id"]
        # A1: the same library and matrix, read by the agent.
        before = st.view()
        ok, library = await chat.call("library", {"request": {"op": "works", "limit": 200}})
        ok2, shown = await chat.call(
            "library", {"request": {"op": "work", "work_id": st.work("bearing")["work_id"]}}
        )
        ok3, matrix = await chat.call("matrix", {"request": {"op": "show", "matrix_id": matrix_id}})
        agent_matrix = matrix.get("result", {}) if ok3 else {}
        bearing_row = next(
            (
                r
                for r in agent_matrix.get("rows", [])
                if r["work_id"] == st.work("bearing")["work_id"]
            ),
            {},
        )
        bases = {
            p["basis"]
            for p in ((shown.get("result") or {}).get("record") or {})
            .get("body", {})
            .get("field_provenance", [])
        }
        titles = [w["title"] for w in (library.get("result") or {}).get("works", [])]
        # The agent may plan an import of the owner's Zotero items, but committing it is the
        # owner's decision (decision 0070 section 7): its request waits for a grant.
        ok_plan, zplan = await chat.call(
            "zotero_import_plan", {"item_keys": ["BOOK0005"], "wait_seconds": 20}
        )
        zplan_id = (zplan.get("result") or {}).get("plan_id", "") if ok_plan else ""
        st.name(zplan_id, "agent_zotero_plan")
        ok_ask, zask = await chat.call("zotero_import_commit", {"plan_id": zplan_id})
        asked_id = ((zask.get("details") or {}) if not ok_ask else {}).get(
            "approval_request_id", ""
        )
        st.name(asked_id, "agent_zotero_approval")
        rec.add(
            "A1",
            "mcp",
            {"tools": ["library works", "library work", "matrix show"]},
            {
                "works": titles,
                "matrix": agent_matrix,
                "bearing_provenance": sorted(bases),
                "zotero_plan": zplan,
                "zotero_commit_asked": zask,
            },
            outcome_of(ok and ok2 and ok3 and ok_plan, matrix),
            [
                check(
                    "the agent plans the import of one Zotero item and its commit is an approval "
                    "request: nothing is imported until the owner grants it",
                    not ok_ask
                    and zask.get("code") == "approval_required"
                    and [
                        (r["operation"], r["state"])
                        for r in st.rows("SELECT operation, state FROM approval_requests")
                    ]
                    == [("zotero_import_commit", "pending")]
                    and not any(w["title"].startswith("Rotor dynamics") for w in st.works()),
                    zask,
                ),
                check(
                    "the agent sees the same matrix the owner built (cells, states, evidence)",
                    view_digest(st, agent_matrix["rows"]) == view_digest(st, before["rows"]),
                    first_difference(
                        st.norm(without_nulls(agent_matrix.get("rows"))),
                        st.norm(without_nulls(before["rows"])),
                    )
                    or "equal",
                ),
                check(
                    "it sees the Zotero-only article as abstract-only, with Zotero provenance",
                    bearing_row.get("access_state") == "abstract_only" and bases == {"zotero"},
                    [bearing_row.get("access_state"), sorted(bases)],
                ),
                check(
                    "library text reaches the agent framed as untrusted data",
                    (library.get("result") or {}).get("content_trust") == "untrusted_data",
                ),
                check("the agent's reads changed nothing", st.view()["rows"] == before["rows"]),
            ],
        )
        # A2: an assumption entry, then a finding that relies on it.
        assumption, replacement, correction = (
            new_id("entry"),
            new_id("entry"),
            new_id("correction"),
        )
        st.name(assumption, "assumption")
        st.name(replacement, "replacement")
        st.name(correction, "correction")
        ok, info = await chat.call("project_info", {})
        first = {
            "schema_version": 1,
            "project_id": st.pid,
            "request_id": new_request_id(),
            "expected_seq": info["project_seq"],
            "operations": [
                create_op("entry", assumption, entry_body("Windage scales with p^0.8", "p^0.8"))
            ],
        }
        ok_a, applied = await chat.call("apply", {"request": first})
        ok_r, page = await chat.call(
            "library",
            {"request": {"op": "read", "work_id": drag_id, "page": 2, "max_chars": 8000}},
        )
        at = page["result"]["text"].index(LOSSES) if ok_r else 0
        ok_s, span = await chat.call(
            "library",
            {
                "request": {
                    "op": "read",
                    "work_id": drag_id,
                    "page": 2,
                    "start": at,
                    "end": at + len(LOSSES),
                }
            },
        )
        ok_p, proposed = await chat.call(
            "matrix_manage",
            {
                "request": {
                    "op": "propose",
                    "matrix_id": matrix_id,
                    "work_id": drag_id,
                    "column_id": "key-finding",
                    "value_text": LOSSES,
                    "evidence": [span["result"]["evidence"]] if ok_s else [],
                    "relies_on": [assumption],
                }
            },
        )
        cell = st.cell("drag", "key-finding")
        rec.add(
            "A2",
            "mcp",
            {"assumption": "Windage scales with p^0.8", "finding": LOSSES},
            {"apply": applied, "propose": proposed},
            outcome_of(ok_a and ok_p, proposed),
            [
                check(
                    "the assumption is stored, unreviewed, as the agent's",
                    st.svc.get(st.pid, st.owner, assumption, None)["body"]["origin"] == "agent",
                ),
                check(
                    "the agent's finding is a proposed cell quoting the page",
                    bool(cell)
                    and cell["value_text"] == LOSSES
                    and cell["state"] == "proposed"
                    and cell["evidence"][0]["where"]["page"] == 2,
                    cell,
                ),
                check(
                    "it is not reviewed: only the owner reviews",
                    st.row("drag")["needs_review"] is False
                    and st.cell("drag", "drag-coefficient")["state"] == "reviewed",
                ),
            ],
        )
        # A3: a replacement and a correction, proposed (nothing applies until the owner does).
        ok, info = await chat.call("project_info", {})
        second = {
            "schema_version": 1,
            "project_id": st.pid,
            "request_id": new_request_id(),
            "expected_seq": info["project_seq"],
            "operations": [
                create_op("entry", replacement, entry_body("Windage scales with p^0.9", "p^0.9")),
                create_op("correction", correction, correction_body(assumption, replacement)),
            ],
        }
        ok_c, filed = await chat.call("apply", {"request": second})
        stored = st.svc.get(st.pid, st.owner, correction, None)["body"]
        rec.add(
            "A3",
            "mcp",
            {"correction": "Windage scales with p^0.9"},
            filed,
            outcome_of(ok_c, filed),
            [
                check("the correction is stored as proposed", stored["status"] == "proposed"),
                check("nothing is flagged yet", st.row("drag")["needs_review"] is False),
                check(
                    "the agent asked for no approval but the one Zotero request of A1",
                    [r["operation"] for r in st.rows("SELECT operation FROM approval_requests")]
                    == ["zotero_import_commit"],
                ),
            ],
        )
    return {"correction": correction, "assumption": assumption}


async def last_session(st: Store, rec: Recorder) -> None:
    """A4: a fresh agent session reads the outcome."""
    async with McpChat(st) as chat:
        ok, matrix = await chat.call(
            "matrix", {"request": {"op": "show", "matrix_id": st.ids()["matrix"]}}
        )
    shown = matrix.get("result", {}) if ok else {}
    drag_id = st.work("drag")["work_id"]
    row = next((r for r in shown.get("rows", []) if r["work_id"] == drag_id), {})
    cells = {c["column_id"]: c for c in row.get("cells", [])}
    rec.add(
        "A4",
        "mcp",
        {},
        matrix,
        outcome_of(ok, matrix),
        [
            check("the row is no longer flagged (re-reviewed)", row.get("needs_review") is False),
            check(
                "the brief is still flagged: it was exported before the correction",
                bool(shown.get("briefs")) and all(b["needs_review"] for b in shown["briefs"]),
            ),
            check(
                "the cells keep their states: the finding and the drag coefficient are reviewed",
                cells.get("key-finding", {}).get("state") == "reviewed"
                and cells.get("drag-coefficient", {}).get("state") == "reviewed",
                {k: v["state"] for k, v in cells.items()},
            ),
            check(
                "the Zotero-only article's row is all unsupported",
                {
                    c["state"]
                    for r in shown.get("rows", [])
                    if r["access_state"] == "abstract_only"
                    for c in r["cells"]
                }
                == {"unsupported"},
            ),
        ],
    )


# ----- the MCP half: the owner's steps through the service ---------------------------------------


def expect_refused(call: Any) -> str:
    from prometheos.domain.models import PrometheosError

    try:
        call()
    except PrometheosError as exc:
        return str(exc.message)
    return ""


def owner_steps_one(st: Store, rec: Recorder) -> None:
    """M1.1-M1.9 through the service, with the same claims as the browser spec."""
    from cp1_support import OWNER_ACT

    svc, pid, owner = st.svc, st.pid, st.owner
    before = svc.project_info(pid, owner)
    added = svc.project_root_add(pid, "papers", str(st.papers), authority=OWNER_ACT)
    roots = svc.project_roots(pid, owner)
    rec.add(
        "M1.1",
        "owner-service",
        {"alias": "papers"},
        roots,
        "ok",
        [
            check(
                "the project lists the papers folder, available",
                [(r["alias"], r["available"]) for r in roots["roots"]] == [("papers", True)],
            ),
            check(
                "adding it is one audited settings revision",
                roots["settings_rev"] == before["settings_rev"] + 1 and bool(added),
            ),
        ],
    )

    plan = svc.library_import_plan(pid, owner, root_alias="papers")
    done = svc.library_import_commit(pid, plan["plan_id"], owner)
    st.name_works()
    drag, kers = st.work("drag"), st.work("kers")
    everything = st.works()
    rec.add(
        "M1.2",
        "owner-service",
        {"root": "papers"},
        {"counts": done["counts"]},
        "ok",
        [
            check(
                "the drag paper is a full-text work of 4 pages",
                drag["access_state"] == "full_text" and drag["page_count"] == 4,
            ),
            check("the review paper is a full-text work", kers["access_state"] == "full_text"),
            check(
                "the bibliography's entries became works (metadata only)",
                len([w for w in everything if w["access_state"] == "metadata_only"]) >= 4,
            ),
            check(
                "the PDF work took the bibliography entry with its DOI (one work, two kinds of "
                "source)",
                drag["sources"] >= 1
                and len(
                    [
                        w
                        for w in everything
                        if w["identifiers"].get("doi") == drag["identifiers"].get("doi")
                    ]
                )
                == 1,
            ),
        ],
    )

    zplan = svc.zotero_import_plan(pid, owner, collection="COLL0001")
    plan_rows = {i["key"]: i["action"] for i in zplan["items"]}
    kept = sorted(k for k in plan_rows if k != DESELECT)
    zdone = svc.zotero_import_commit(
        pid, zplan["plan_id"], owner, authority=OWNER_ACT, item_keys=kept
    )
    st.name_works()
    everything = st.works()
    holders = [w for w in everything if w["identifiers"].get("doi") == drag["identifiers"]["doi"]]
    shown = svc.library_work(pid, owner, drag["work_id"])
    bases = {p["basis"] for p in shown["record"]["body"]["field_provenance"]}
    bearing = st.work("bearing")
    rec.add(
        "M1.3",
        "owner-service",
        {"collection": COLLECTION, "deselected": DESELECT},
        {"works": len(everything), "bases": sorted(bases)},
        "ok",
        [
            check(
                "the collection plan lists three items; the DRAG item would be merged",
                len(plan_rows) == 3 and plan_rows["DRAG0001"] == "merge",
                plan_rows,
            ),
            check(
                "the DOI the PDF, the BibTeX file and Zotero share is one work",
                len(holders) == 1 and holders[0]["work_id"] == drag["work_id"],
            ),
            check(
                "that work holds the provenance of every source (Zotero beside the others)",
                "zotero" in bases and "bibliography" in bases and len(bases) >= 3,
                sorted(bases),
            ),
            check(
                "the Zotero-only article is an abstract-only work",
                bearing["access_state"] == "abstract_only",
            ),
            check(
                "the deselected preprint was not imported",
                not any(w["title"] == "Bearing drag in rarefied gas" for w in everything),
            ),
            check("two items were committed", zdone["counts"]["committed"] == 2),
        ],
    )

    hits = svc.library_search(pid, owner, "drag coefficient")["hits"]
    read = svc.library_read(pid, owner, drag["work_id"], page=2, max_chars=8000)
    rec.add(
        "M1.4",
        "owner-service",
        {"query": "drag coefficient"},
        {"hits": hits},
        "ok",
        [
            check(
                "the search names the page and character span of the hit",
                any(
                    h["work_id"] == drag["work_id"] and h["page"] == 2 and h["end"] > h["start"]
                    for h in hits
                ),
            ),
            check("the page reads with the sentence in it", SENTENCE in read["text"]),
        ],
    )
    hit = next(h for h in hits if h["work_id"] == drag["work_id"] and h["page"] == 2)

    columns = [
        {
            "id": re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"),
            "label": label,
            "value_kind": kind,
            "instructions": "",
            **({"choices": [c.strip() for c in choices.split(",")]} if choices else {}),
        }
        for label, kind, choices in COLUMNS
    ]
    made = svc.matrix_create(
        pid,
        owner,
        title="Flywheel losses",
        columns=columns,
        work_ids=[drag["work_id"], kers["work_id"], bearing["work_id"]],
    )
    st.name(made["matrix_id"], "matrix")
    view = st.view()
    rec.add(
        "M1.5",
        "owner-service",
        {"title": "Flywheel losses"},
        view,
        "ok",
        [
            check(
                "one matrix, titled as typed",
                [m["title"] for m in svc.matrix_list(pid, owner)["matrices"]]
                == ["Flywheel losses"],
            ),
            check(
                "its columns are the typed ones",
                [c["id"] for c in view["matrix"]["columns"]]
                == ["method", "pressure", "drag-coefficient", "key-finding"],
            ),
            check(
                "one row per work, each showing the work's access state",
                sorted(r["access_state"] for r in view["rows"])
                == ["abstract_only", "full_text", "full_text"],
            ),
        ],
    )

    run = svc.matrix_extract(pid, owner, matrix_id=made["matrix_id"], rules=RULE_SET)
    view = st.view()
    rec.add(
        "M1.6",
        "owner-service",
        {"rules": "method, pressure, key-finding"},
        view,
        "ok",
        [
            check(
                "the drag paper's method is classified by the rule, as proposed",
                (st.cell("drag", "method") or {}).get("value_text") == "experiment"
                and st.cell("drag", "method")["state"] == "proposed",
            ),  # type: ignore[index]
            check(
                "its pressure is the quoted 10 Pa and its finding the quoted 92 %",
                (st.cell("drag", "pressure") or {}).get("value_text") == "10 Pa"
                and (st.cell("drag", "key-finding") or {}).get("value_text")
                == "reduces windage loss by 92 %",
            ),
            check(
                "the abstract-only Zotero work has no value claim: every cell is unsupported",
                {c["state"] for c in st.row("bearing")["cells"]} == {"unsupported"},
            ),
            check(
                "the rule run proposed three cells",
                run["counts"].get("created") == 3,
                run["counts"],
            ),
        ],
    )

    kers_text = svc.library_read(pid, owner, kers["work_id"], page=1, max_chars=8000)["text"]
    k_at = kers_text.index(REVIEW_SENTENCE)
    k_span = svc.library_read(
        pid, owner, kers["work_id"], page=1, start=k_at, end=k_at + len(REVIEW_SENTENCE)
    )
    svc.matrix_propose(
        pid,
        owner,
        matrix_id=made["matrix_id"],
        work_id=kers["work_id"],
        column_id="method",
        value_text="review",
        evidence=[k_span["evidence"]],
    )
    cell = st.cell("kers", "method")
    rec.add(
        "M1.7",
        "owner-service",
        {"work": "kers", "column": "method", "value": "review"},
        cell or {},
        "ok",
        [
            check(
                "the review paper is labelled 'review', proposed, citing page 1",
                bool(cell)
                and cell["value_text"] == "review"
                and cell["state"] == "proposed"
                and cell["evidence"][0]["where"]["page"] == 1,
                cell,
            )
        ],
    )

    first = expect_refused(
        lambda: svc.matrix_propose(
            pid,
            owner,
            matrix_id=made["matrix_id"],
            work_id=drag["work_id"],
            column_id="drag-coefficient",
            value_text="0.0042",
            evidence=[hit["evidence"]],
            quantities=[{"decimal": "0.0042", "unit": ""}],
        )
    )
    at = read["text"].index(SENTENCE)
    span = svc.library_read(pid, owner, drag["work_id"], page=2, start=at, end=at + len(SENTENCE))
    second = expect_refused(
        lambda: svc.matrix_propose(
            pid,
            owner,
            matrix_id=made["matrix_id"],
            work_id=drag["work_id"],
            column_id="drag-coefficient",
            value_text="0.042",
            evidence=[span["evidence"]],
            quantities=[{"decimal": "0.042", "unit": ""}],
        )
    )
    svc.matrix_propose(
        pid,
        owner,
        matrix_id=made["matrix_id"],
        work_id=drag["work_id"],
        column_id="drag-coefficient",
        value_text="0.0042",
        evidence=[span["evidence"]],
        quantities=[{"decimal": "0.0042", "unit": ""}],
    )
    cell = st.cell("drag", "drag-coefficient")
    rec.add(
        "M1.8",
        "owner-service",
        {"column": "drag-coefficient", "attempts": ["0.0042 on a hit", "0.042", "0.0042"]},
        {"refused": [first, second], "cell": cell},
        "ok",
        [
            check(
                "the search hit quotes only 'drag coefficient': the number is refused, verbatim",
                "0.0042 does not occur verbatim in the quoted evidence" in first,
                first,
            ),
            check(
                "the sentence is real but 0.042 is not in it: refused, verbatim",
                "cell drag-coefficient: 0.042 does not occur verbatim" in second,
                second,
            ),
            check(
                "the cell binds 0.0042 to the page, the span and the quote digest",
                bool(cell)
                and cell["value_text"] == "0.0042"
                and cell["state"] == "proposed"
                and cell["evidence"][0]["where"]["page"] == 2
                and cell["evidence"][0]["where"]["start"] == at
                and cell["evidence"][0]["where"]["end"] == at + len(SENTENCE),
                cell,
            ),
        ],
    )

    svc.matrix_review(
        pid,
        owner,
        matrix_id=made["matrix_id"],
        work_id=drag["work_id"],
        column_ids=["drag-coefficient"],
        authority=OWNER_ACT,
    )
    cell = st.cell("drag", "drag-coefficient")
    rec.add(
        "M1.9",
        "owner-service",
        {"cell": "drag-coefficient"},
        cell or {},
        "ok",
        [
            check(
                "the owner's review is stored: the cell is reviewed",
                bool(cell) and cell["state"] == "reviewed",
            ),
            check(
                "the other cells were not reviewed with it",
                (st.cell("drag", "method") or {}).get("state") == "proposed",
            ),
        ],
    )


def owner_steps_two(st: Store, rec: Recorder) -> None:
    """M2.1-M2.3 through the service."""
    from cp1_support import OWNER_ACT, request

    svc, pid, owner = st.svc, st.pid, st.owner
    matrix_id = st.ids()["matrix"]
    exported = svc.matrix_export(pid, owner, matrix_id, "markdown")
    st.name(exported["brief"]["version_id"], "brief")
    view = st.view()
    rec.add(
        "M2.1",
        "owner-service",
        {"format": "markdown"},
        view["briefs"],
        "ok",
        [
            check(
                "one brief is registered for the matrix, current",
                len(view["briefs"]) == 1 and not view["briefs"][0]["needs_review"],
            )
        ],
    )

    correction = st.ids()["correction"]
    raw = request(
        pid,
        [{"op": "correction_apply", "id": correction, "base_rev": 1}],
        svc.project_info(pid, owner)["project_seq"],
    )
    digest_ = svc.review_request(pid, raw, owner)["digest"]
    grant = svc.grant(
        pid,
        operation="exact_request",
        digest=digest_,
        ttl_seconds=60,
        max_uses=1,
        label="SYNTHETIC-TEST owner decision (C11-WALK)",
        synthetic=True,
    )
    svc.apply(pid, raw, owner, grant.secret)
    view = st.view()
    row = st.row("drag")
    rec.add(
        "M2.2",
        "owner-service",
        {"correction": "correction"},
        {"row": row["needs_review"], "briefs": view["briefs"]},
        "ok",
        [
            check("the drag paper's row needs review", row["needs_review"] is True),
            check(
                "the brief is flagged",
                len(view["briefs"]) == 1 and view["briefs"][0]["needs_review"] is True,
            ),
        ],
    )

    svc.matrix_review(
        pid,
        owner,
        matrix_id=matrix_id,
        work_id=st.work("drag")["work_id"],
        column_ids=["key-finding"],
        authority=OWNER_ACT,
    )
    view = st.view()
    row = st.row("drag")
    cell = st.cell("drag", "key-finding")
    rec.add(
        "M2.3",
        "owner-service",
        {"cell": "key-finding"},
        cell or {},
        "ok",
        [
            check("the row's flag is cleared", row["needs_review"] is False),
            check(
                "the finding is reviewed with the agent's value",
                bool(cell) and cell["value_text"] == LOSSES and cell["state"] == "reviewed",
            ),
            check(
                "the brief stays flagged until it is exported again",
                all(b["needs_review"] for b in view["briefs"]),
            ),
        ],
    )


async def mcp_phase(st: Store, rec: Recorder) -> None:
    owner_steps_one(st, rec)
    await agent_session(st, rec)
    owner_steps_two(st, rec)
    await last_session(st, rec)


# ----- the hub half (Playwright against the real hub, the agent over MCP) ------------------


def hub_fixture(st: Store) -> dict[str, Any]:
    return {
        "papers": str(st.papers),
        "sentence": SENTENCE,
        "losses": LOSSES,
        "review_sentence": REVIEW_SENTENCE,
        "columns": [list(c) if c[2] else [c[0], c[1]] for c in COLUMNS],
        "rules": RULES,
        "titles": TITLES,
        "zotero": {"collection": COLLECTION, "deselect": DESELECT},
    }


def hub_phase(st: Store, out: Path, rec: Recorder) -> None:
    try:
        require_headless_shell(ROOT / "hub")
        playwright = playwright_command(ROOT / "hub")  # the local runner, never npx
    except BrowserUnavailable as exc:
        raise RuntimeError(f"the browser half cannot run: {exc}") from exc
    info_path = out / "hub-info.json"
    steps_path = out / "hub-steps.json"
    signals = out / "signals"
    signals.mkdir()
    log = open(out / "hub.log", "w", encoding="utf-8")
    server = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "walk_hub18.py"),
            "--home",
            str(st.home),
            "--project",
            st.pid,
            "--info",
            str(info_path),
            "--tokens",
            "8",
        ],
        cwd=ROOT,
        stdout=log,
        stderr=log,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    browser = None

    def name_created() -> None:
        if steps_path.exists():
            for step in json.loads(steps_path.read_text(encoding="utf-8")):
                for name, record_id in step.get("created", {}).items():
                    if name in ("matrix", "brief"):  # works are named by title (name_works)
                        st.name(record_id, name)

    try:
        deadline = time.monotonic() + 60
        while not info_path.exists():
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the walk hub did not start; see hub.log")
            time.sleep(0.2)
        time.sleep(0.5)
        env = {
            **playwright_environment(),
            "WALK_INFO": str(info_path),
            "WALK_STEPS": str(steps_path),
            "WALK_SIGNALS": str(signals),
            "WALK_SHOTS": str(out / "screenshots"),
            "WALK_PW_OUT": str(Path(tempfile.mkdtemp(prefix="prometheos-walk11-pw-"))),
            "BOUNDARY_EVIDENCE": str(out / "browser-boundary.json"),
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(out / "playwright-report.json"),
            "WALK_FIXTURE": json.dumps(hub_fixture(st)),
        }
        pw_log = open(out / "playwright.log", "w", encoding="utf-8")
        browser = subprocess.Popen(
            [*playwright, "-c", "playwright.walk11.config.ts", "--reporter=line,json"],
            cwd=ROOT / "hub",
            env=env,
            stdout=pw_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # The owner's first steps run in the browser; then the agent's session runs while the hub
        # keeps serving, and the browser waits for it before the owner's last steps.
        deadline = time.monotonic() + 480
        while not (signals / "owner-1.done").exists():
            if browser.poll() is not None or time.monotonic() > deadline:
                break
            time.sleep(0.25)
        if (signals / "owner-1.done").exists():
            name_created()
            st.name_works()
            try:
                names = asyncio.run(agent_session(st, rec))
            except Exception as exc:
                (signals / "agent.failed").write_text(repr(exc), encoding="utf-8")
                raise
            (signals / "agent.done").write_text(json.dumps(names), encoding="utf-8")
        try:
            code = browser.wait(timeout=600)
        finally:
            pw_log.close()
        if code != 0:
            rec.failures.append(f"hub: the Playwright walk failed (exit {code})")
            print((out / "playwright.log").read_text(encoding="utf-8")[-4000:])
        name_created()
        if code == 0:
            asyncio.run(last_session(st, rec))
    finally:
        if browser is not None and browser.poll() is None:
            browser.kill()
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
    if steps_path.exists():
        for step in json.loads(steps_path.read_text(encoding="utf-8")):
            rec.add(
                step["id"],
                step["via"],
                step.get("request", {}),
                step.get("response", {}),
                step["outcome"],
                step["assertions"],
            )


# ----- orchestration ---------------------------------------------------------------------------


def transcript_markdown(doc: dict[str, Any]) -> str:
    lines = ["# C11-WALK transcript", "", doc["summary"], ""]
    for half in ("hub", "mcp"):
        data = doc.get(half)
        if not data:
            continue
        lines += [f"## {half} half ({data['transport']}, store {data['store']})", ""]
        lines += [
            "| Step | Actor | Via | Operation | Outcome | Response digest (normalised) |",
            "|---|---|---|---|---|---|",
        ]
        for s in data["steps"]:
            lines.append(
                f"| {s['id']} | {s['actor']} | {s['via']} | {s['operation']} | {s['outcome']} | "
                f"`{s['normalised_response_sha256'][:16]}` |"
            )
        lines.append("")
        for s in data["steps"]:
            for a in s["store_assertions"]:
                mark = "pass" if a["passed"] else "FAIL"
                lines.append(f"- {s['id']} [{mark}] {a['claim']}")
        lines.append("")
    lines += ["## Equivalence", ""]
    for key, value in doc["equivalence"].items():
        lines.append(f"- {key}: {value}")
    if doc.get("zotero"):
        lines += ["", "## Zotero stub (read-only)", ""]
        for key, value in doc["zotero"].items():
            lines.append(f"- {key}: {value}")
    boundary = doc.get("browser_boundary")
    if boundary:
        lines += ["", "## Browser boundary (decision 0028)", ""]
        lines.append(
            f"- evidence: {boundary['evidence']}; all passed: {boundary.get('all_passed')}"
        )
        lines.append(f"- browser: {boundary.get('browser')}")
        for control, status in boundary.get("controls", {}).items():
            lines.append(f"- [{status}] {control}")
    if doc.get("failures"):
        lines += ["", "## Failures", ""] + [f"- {f}" for f in doc["failures"]]
    return "\n".join(lines) + "\n"


def judge(doc: dict[str, Any], outcomes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Compare the two halves: stored outcomes, the scripted steps, each step's outcome and the
    normalised answer of every agent step."""
    equivalence: dict[str, Any] = {}
    failures: list[str] = []
    difference = first_difference(outcomes["hub"], outcomes["mcp"])
    equivalence["stored outcomes"] = "equal" if difference is None else difference
    if difference is not None:
        failures.append(f"stored outcomes differ: {difference}")
    hub_steps = {s["id"]: s for s in doc["hub"]["steps"]}
    mcp_steps = {s["id"]: s for s in doc["mcp"]["steps"]}
    missing = [i for i in SCRIPT_IDS if i not in hub_steps or i not in mcp_steps]
    equivalence["scripted steps present on both halves"] = (
        "all" if not missing else f"missing {missing}"
    )
    if missing:
        failures.append(f"steps missing: {missing}")
    both = [i for i in SCRIPT_IDS if i in hub_steps and i in mcp_steps]
    differing = {
        i: (hub_steps[i]["outcome"], mcp_steps[i]["outcome"])
        for i in both
        if hub_steps[i]["outcome"] != mcp_steps[i]["outcome"]
    }
    equivalence["step outcomes (ok / error:code)"] = (
        "equal" if not differing else f"differ {differing}"
    )
    if differing:
        failures.append(f"step outcomes differ: {differing}")
    compared = [i for i in both if i in AGENT_IDS]
    unequal = [
        i
        for i in compared
        if hub_steps[i]["normalised_response_sha256"] != mcp_steps[i]["normalised_response_sha256"]
    ]
    equivalence["normalised answers of every agent step"] = (
        f"equal for {len(compared)} steps" if not unequal else f"differ at {unequal}"
    )
    if unequal:
        failures.append(f"agent answers differ at {unequal}")
    return equivalence, failures


def zotero_report(stub: ZoteroStub) -> tuple[dict[str, Any], list[str]]:
    """The stub saw only reads: GET, no Origin header, no key, nothing dropped."""
    seen = list(stub.requests)
    methods = sorted({r["method"] for r in seen})
    report = {
        "requests": len(seen),
        "methods": methods,
        "with Origin": len([r for r in seen if r["origin"] is not None]),
        "with a key or authorisation": len(
            [r for r in seen if r["has_authorization"] or r["has_zotero_api_key"]]
        ),
        "dropped by the stub's hardening": len(stub.dropped),
    }
    problems = []
    if not seen:
        problems.append("zotero: the stub saw no request (the import did not read it)")
    if methods != ["GET"]:
        problems.append(f"zotero: methods other than GET were sent: {methods}")
    if report["with Origin"] or report["with a key or authorisation"] or stub.dropped:
        problems.append(f"zotero: the adapter sent what it must not: {report}")
    return report, problems


def run(out: Path | None, only: str | None, keep: bool, show: bool) -> int:
    from cp1_support import AGENT  # noqa: F401  (imported to fail early when tests/ is absent)

    out = (out or Path(tempfile.mkdtemp(prefix="prometheos-walk11-out-"))).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into a folder that is not empty: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prometheos-walk11-"))
    stub = ZoteroStub(log_path=out / "zotero-requests.jsonl")
    port = stub.start()
    doc: dict[str, Any] = {
        "walk": "C11-WALK",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "script": SCRIPT_IDS,
        "limitations": [
            "The MCP half's owner steps (M1.x, M2.x) run through the service in the harness, a "
            "synthetic owner channel: the correction is applied with a SYNTHETIC-TEST owner "
            "decision bound to its digest, the folder and the reviews with an owner act. The hub "
            "half's owner steps are the real browser.",
            "Zotero is a stub of the documented local API (tools/zotero_stub.py); the owner's "
            "real Zotero is the external gate E10.",
            "The agent is a scripted MCP client; real agent behaviour is observed with real use.",
            "Page text only: tables, formulas and layout-aware extraction wait for CP11-F1.",
        ],
    }
    failures: list[str] = []
    outcomes: dict[str, Any] = {}
    equivalence: dict[str, Any] = {}
    try:
        if only in (None, "hub"):
            store_a = Store(work / "store-a", port)
            rec_a = Recorder("A", "hub", store_a, out / "evidence" / "hub")
            hub_phase(store_a, out, rec_a)
            outcomes["hub"] = snapshot(store_a)
            (out / "outcome-hub.json").write_text(
                json.dumps(outcomes["hub"], indent=2, sort_keys=True), encoding="utf-8"
            )
            doc["hub"] = {
                "store": "A",
                "transport": "hub (owner in the browser) + agent over MCP stdio",
                "steps": rec_a.ordered(),
            }
            failures += rec_a.failures
            doc["browser_boundary"], problems = boundary_evidence(out)
            failures += problems
        if only in (None, "mcp"):
            note = hub_stopped_note(out)
            equivalence["the hub was stopped during the MCP half"] = note
            if note == "NOT STOPPED":
                failures.append("the hub still answered during the MCP half")
            store_b = Store(work / "store-b", port)
            rec_b = Recorder("B", "mcp", store_b, out / "evidence" / "mcp")
            asyncio.run(mcp_phase(store_b, rec_b))
            outcomes["mcp"] = snapshot(store_b)
            (out / "outcome-mcp.json").write_text(
                json.dumps(outcomes["mcp"], indent=2, sort_keys=True), encoding="utf-8"
            )
            doc["mcp"] = {
                "store": "B",
                "transport": "MCP stdio (official SDK client), hub stopped",
                "steps": rec_b.ordered(),
            }
            failures += rec_b.failures
        if "hub" in outcomes and "mcp" in outcomes:
            judged, problems = judge(doc, outcomes)
            equivalence = judged | equivalence
            failures += problems
        doc["zotero"], problems = zotero_report(stub)
        failures += problems
        doc["equivalence"] = equivalence
        doc["failures"] = failures
        doc["summary"] = (
            (
                "PASS: every step's store assertions held and the two stores reached equivalent "
                "outcomes."
                if only is None
                else f"PASS: {only} half only; assertions held. Equivalence was not checked."
            )
            if not failures
            else f"FAIL: {len(failures)} problem(s); see failures."
        )
        doc["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (out / "transcript.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        markdown = transcript_markdown(doc)
        (out / "transcript.md").write_text(markdown, encoding="utf-8")
        if show:  # the checkpoint runner keeps stdout as evidence
            print(markdown)
        print(doc["summary"])
        for failure in failures:
            print(" -", failure)
        print("transcript:", out / "transcript.json")
        return 0 if not failures else 1
    finally:
        stub.stop()
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, help="a new folder for the evidence (default: a temp one)"
    )
    parser.add_argument("--only", choices=("hub", "mcp"), help="run one half (debugging)")
    parser.add_argument("--keep", action="store_true", help="keep the disposable stores")
    parser.add_argument(
        "--print-transcript",
        action="store_true",
        help="print the transcript (the checkpoint runner retains stdout)",
    )
    args = parser.parse_args(argv)
    return run(args.out, args.only, args.keep, args.print_transcript)


if __name__ == "__main__":
    sys.exit(main())
