"""C20-WALK: the CP20.5 walk of the research map (spec/10 CP20.5; A06.1; decisions 0083, 0084,
0106, 0107).

    uv run python tools/walk_cp20.py [--out DIR] [--print-transcript] [--only hub|mcp] [--keep]

Each store is fresh: ``tools/research_map_fixture.py`` writes a synthetic research repository
that forks by track (``track2/`` and ``track5/``), with a stale run (``track5/run_sim.m`` changed
after it wrote ``track5/energy.csv``), a definitional pair (two values of "uphill energy" under
two definitions) and a corrected number (18.2 % corrected to 17.4 %). It is the project's
``study`` root.
No model is called and nothing is sent anywhere: every agent step is the real ``prometheos mcp
serve --stdio`` behind the official MCP SDK client, and every owner step is the hub (leg A) or the
hub owner's act through the service (leg B).

The story (per leg):

- **S1 owner.** Scan the folder, read the proposed outline (tracks track2 and track5, the paper,
  three questions, the stale candidate ``track5/energy.csv``), correct it (RQ1 under the paper; the
  clutch question, which nothing in the folder answers yet, placed nowhere) and confirm it once.
- **S2 agent.** Import the cited outputs and record five claims with bound quantities under RQ1, the
  definitional pair's second binding, its own assessments and the correction of 18.2 %.
- **S3 owner.** The research map: each claim's status, basis and causes under its question, the
  deliverable's stage and what keeps it from the next.
- **S4 owner.** Open the stale track 5 claim: its cause (``producer_changed``, naming the script)
  and its verifiables (path, line, digest and size).
- **S5 agent.** Ask PrometheOS to plan checks, read the plan, start the budget-eligible read-only
  check on the 17.4 % claim with no approval, record an assessment whose check carries expected,
  measured and tolerance (a pass naming an expected value without a measured one is refused) and
  mark the check done.
- **S6 agent.** Try to start the rerun the stale claim needs: ``approval_required``. Ask for two
  decisions, each with a card (``approval_required``): withdrawing the track 5 claim until its
  rerun, with a default date seconds away, and, last, applying the 18.2 % correction. (An approval
  request binds the project's exact state, so a grant is resumable only while nothing else was
  written after the request; the agent asks last.)
- **S7 owner.** Once the second card's default has passed, see it in effect and the records
  unchanged by it (record digest and the folder's bytes); then answer the first card (grant exactly
  its plan) and the agent resumes it once: the 18.2 % claim is superseded and the 17.4 % one reads
  corrected.

Then, for the walk: **S8** the two stores' normalised stored outcomes (claims with derived status,
basis and causes, the deliverable's stage, the outline confirmations, the checks and their states,
the approval requests and their states, the records by kind), every step's outcome and every agent
answer must be equal; **S9** the transcript (JSON and text) and the evidence (each step's answer,
the hub's screenshots and the boundary evidence) are written to ``--out``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

from browser_shell import (  # noqa: E402
    BrowserUnavailable,
    playwright_command,
    playwright_environment,
    require_headless_shell,
)
from research_map_fixture import GOLDEN, generate, pointer, text_of  # noqa: E402
from walk_cp10 import boundary_evidence, check, digest, first_difference  # noqa: E402
from walk_cp18 import McpChat, outcome_of  # noqa: E402

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
ALIAS = "study"
PAPER = GOLDEN["deliverable"]
RQ1, DAMPER, CLUTCH = GOLDEN["questions"]
UP = "results/uphill_energy.md"
OLD, NEW = GOLDEN["corrected"]
IMPORTS = ["track2/energy.csv", "track5/energy.csv", UP, OLD, NEW]
SCOPE = {"system": "Synthetic flywheel KERS bus", "operating_regime": "Urban and hilly cycles",
         "comparator": "Same bus without the device", "model_fidelity": "Reduced model",
         "validation_level": "Re-read from the stored output"}  # fmt: skip
# (key, statement, evidence path, line, value, quantity name, definition, unit)
CLAIMS = [
    ("track2", "Track 2 (urban cycle) uses 196.9 kWh.", "track2/energy.csv", 2, "196.9",
     "energy", "urban cycle, track 2", ""),
    ("track5", "Track 5 (hilly route) uses 238.0 kWh.", "track5/energy.csv", 2, "238.0",
     "energy", "hilly route, track 5", ""),
    ("uphill", "The uphill energy on the stored cycle is 196.9 MJ.", UP, 3, "196.9",
     "uphill energy", "stored cycle", "MJ"),
    ("saving_old", "The bus saves 18.2 % fuel.", OLD, 2, "18.2", "fuel saving", "bus, urban", ""),
    ("saving_new", "The bus saves 17.4 % fuel.", NEW, 2, "17.4", "fuel saving", "bus, urban", ""),
]  # fmt: skip
LINE = {c[0]: c[1] for c in CLAIMS}
CARD = {"what": "Replace the 18.2 % fuel saving with 17.4 %",
        "why": "fuel_saving_v2.csv corrects the earlier summary of the urban runs.",
        "recommendation": "Apply the correction: the 17.4 % claim replaces the 18.2 % one.",
        "default_text": "Until you decide, the 18.2 % claim reads as settling."}  # fmt: skip
SECOND = {"what": "Withdraw the track 5 energy until its rerun",
          "why": "track5/run_sim.m changed after it wrote track5/energy.csv.",
          "recommendation": "Withdraw the claim; record it again after the rerun.",
          "default_text": "Nothing is withdrawn: views keep showing the claim as "
                          "stale."}  # fmt: skip
DEFAULT_DELAY = 20  # seconds from the second card's request to its default date
CHECK = {"name": "Fuel saving re-read from fuel_saving_v2.csv", "result": "pass",
         "expected": "17.4 %", "measured": "17.4 %",
         "tolerance": "0.05 percentage points"}  # fmt: skip
# The derived status, basis and leading cause of each claim the owner reads in S3.
S3_EXPECTED = {"track2": ("holds", "agent_reported", "supported"),
               "track5": ("stale", None, "producer_changed"),
               "uphill": ("holds", "agent_reported", "supported"),
               "saving_old": ("settling", None, "correction_proposed"),
               "saving_new": ("unverified", None, "no_assessment")}  # fmt: skip
HUB_STATUS = {"holds": "Holds", "corrected": "Holds with a corrected number",
              "settling": "Settling", "unverified": "Unverified", "stale": "Stale: do not quote",
              "superseded": "Do not use (superseded)"}  # fmt: skip
AGENT_BASIS = "your agent's check, not yet reviewed"

STEPS: list[dict[str, Any]] = [
    {"id": "S1", "actor": "owner", "operation": "scan the folder, correct the proposed outline "
     "(RQ1 under the paper, the clutch question nowhere) and confirm it once",
     "service": "scan, read the outline and confirm it with the same edits (the hub owner's act)"},
    {"id": "S2", "actor": "agent", "operation": "import the cited outputs; record the claims "
     "under RQ1, the definitional pair, its assessments and the correction"},
    {"id": "S3", "actor": "owner", "operation": "read the research map: each claim's status, "
     "basis and causes under its question and the deliverable's stage",
     "service": "read the research map as the owner"},
    {"id": "S4", "actor": "owner", "operation": "open the stale claim's cause and verifiables",
     "service": "read the stale claim's details as the owner"},
    {"id": "S5", "actor": "agent", "operation": "plan checks; start the budget-eligible read-only "
     "check without an approval, record expected, measured and tolerance, mark it done"},
    {"id": "S6", "actor": "agent", "operation": "try to start the rerun; ask for two decisions "
     "with cards (one whose default is seconds away, then the correction's application)"},
    {"id": "S7", "actor": "owner", "operation": "see the second card's default in effect with "
     "the records unchanged; answer the first card (grant) and the agent resumes it",
     "service": "read the cards after the default; grant the first card (the hub owner's act)"},
]  # fmt: skip
SCRIPT_IDS = [s["id"] for s in STEPS]
AGENT_IDS = [s["id"] for s in STEPS if s["actor"] == "agent"]
BROWSER_IDS = ["S1", "S3", "S4", "S7"]
WALK_STEPS = [
    {"id": "S8", "operation": "compare the two stores' normalised stored outcomes, every step's "
     "outcome and every agent answer"},
    {"id": "S9", "operation": "write the transcript and the evidence for the reviewer"},
]  # fmt: skip
# The tables that hold the research record (a passed default must change none of them).
RECORD_TABLES = ("project_state", "records", "revisions", "bodies", "relations", "requests",
                 "approval_requests", "adoption_outlines")  # fmt: skip


# ----- the store -----------------------------------------------------------------------------


class Store:
    def __init__(self, work: Path) -> None:
        from prometheos.app.authority import OwnerAct
        from prometheos.app.reads import Caller
        from prometheos.app.service import Service

        self.work = work
        self.home = work / "home"
        self.study = work / "study"
        generate(self.study)
        self.svc = Service(home=self.home, actor="cli")
        self.owner = Caller(actor="hub-owner", scope="owner")
        self.act = OwnerAct(channel="hub-owner", actor="hub-owner")
        made = self.svc.init_project(
            title="Synthetic flywheel study (C20-WALK)",
            objective="Show what a flywheel KERS saves a city bus, from the study's own files.",
            timezone="UTC",
            roots={ALIAS: str(self.study)},
        )
        self.pid: str = made["project_id"]
        self.store_dir = Path(made["store"])
        self.folder_before = folder_state(self.study)
        self.ids: dict[str, str] = {}  # the walk's own handles (claims, corrections, approvals)
        self.versions: dict[str, dict[str, Any]] = {}
        self.default_at = ""
        self.record_digest_before = ""

    def rows(self, sql: str) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.store_dir / "record.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def record_digest(self) -> str:
        """Every row of the tables that hold the research record (not caches or run logs)."""
        conn = sqlite3.connect(f"file:{(self.store_dir / 'record.sqlite').as_posix()}?mode=ro",
                               uri=True)  # fmt: skip
        try:
            parts = []
            for table in RECORD_TABLES:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608
                parts.append(f"{table}:{json.dumps(rows, default=str)}")
            return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
        finally:
            conn.close()

    def status(self) -> dict[str, dict[str, Any]]:
        claims = self.svc.claim_status(self.pid, self.owner)["claims"]
        by_id = {c["claim_id"]: c for c in claims}
        return {k: by_id[self.ids[k]] for k, *_ in CLAIMS if self.ids.get(k) in by_id}

    def map(self) -> dict[str, Any]:
        return self.svc.research_map(self.pid, self.owner)

    def deliverable(self) -> dict[str, Any]:
        found = [d for d in self.map()["deliverables"] if d["title"] == PAPER]
        return found[0] if found else {}

    def approvals(self) -> dict[str, str]:
        return {a["approval_request_id"]: a["state"]
                for a in self.svc.approval_list(self.pid, self.owner)["approvals"]}  # fmt: skip

    def body(self, record_id: str) -> dict[str, Any]:
        return dict(self.svc.get(self.pid, self.owner, record_id, None)["body"])


def folder_state(root: Path) -> list[tuple[str, str, int]]:
    """Every file of the adopted folder with its digest and modification time."""
    return sorted(
        (p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest(),
         p.stat().st_mtime_ns)
        for p in root.rglob("*") if p.is_file()
    )  # fmt: skip


def ordered(values: Any) -> list[Any]:
    """Sorted by canonical text (values may hold None beside strings)."""
    return sorted(values, key=lambda v: json.dumps(v, sort_keys=True, default=str))


def snapshot(st: Store) -> dict[str, Any]:
    """What the two legs must agree on (ids, digests, times and actors normalised)."""
    status = st.status()
    rmap = st.map()
    under: dict[str, list[str]] = {}
    for d in rmap["deliverables"]:
        for q in d["questions"]:
            for c in q["claims"]:
                under.setdefault(c["claim_id"], []).append(q["title"])
    claims = ordered(
        (LINE[k], st.body(st.ids[k])["state"], c["status"], c["basis"],
         [x["code"] for x in c["causes"]], sorted(under.get(st.ids[k], [])))
        for k, c in status.items()
    )  # fmt: skip
    deliverables = ordered(
        (d["title"], d["stage"], d["next"], d["paper_status"],
         [(s["key"], s["reached"], sorted(b["code"] for b in s["blockers"])) for s in d["steps"]])
        for d in rmap["deliverables"]
    )  # fmt: skip
    actions = st.svc.list_records(st.pid, st.owner, kind="action", limit=200)["items"]
    checks = ordered(
        (a["body"]["title"], a["body"]["status"], a["body"]["verification"]["kind"],
         a["body"]["verification"]["estimated_minutes"])
        for a in actions if a["body"].get("verification")
    )  # fmt: skip
    approvals = ordered(
        (r["operation"], r["state"], (json.loads(r["plan"]).get("card") or {}).get("what"))
        for r in st.rows("SELECT operation, state, plan FROM approval_requests")
    )  # fmt: skip
    outlines = [
        json.loads(r["confirmed"]) for r in st.rows("SELECT confirmed FROM adoption_outlines")
    ]
    return {
        "claims": claims,
        "deliverables": deliverables,
        "questions": ordered(
            [(q["title"], "objective") for q in rmap["objective"]["questions"]]
            + [(q["title"], "unplaced") for q in rmap["unplaced_questions"]]
            + [(q["title"], d["title"]) for d in rmap["deliverables"] for q in d["questions"]]
        ),
        "tracks": sorted(t["title"] for t in rmap["tracks"]),
        "outline_confirmations": len(outlines),
        "outline_kept": [
            ordered((i["kind"], i["title"], bool(i.get("record"))) for i in o.get("items", []))
            for o in outlines
        ],
        "outline_edits": [len((o.get("edits") or {}).get("addresses", {})) for o in outlines],
        "checks": checks,
        "approvals": approvals,
        "records": ordered(
            (r["kind"], r["n"])
            for r in st.rows("SELECT kind, COUNT(*) AS n FROM records GROUP BY kind")
        ),
        "folder_unchanged": folder_state(st.study) == st.folder_before,
    }


class Recorder:
    def __init__(self, transport: str, evidence: Path) -> None:
        self.transport, self.evidence = transport, evidence
        evidence.mkdir(parents=True, exist_ok=True)
        self.steps: dict[str, dict[str, Any]] = {}
        self.failures: list[str] = []

    def add(self, step_id: str, via: str, answer: Any, outcome: str,
            assertions: list[dict[str, Any]]) -> None:  # fmt: skip
        (self.evidence / f"{step_id}.json").write_text(
            json.dumps(answer, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        if not assertions:
            assertions = [check("the step recorded at least one store assertion", False)]
        for a in assertions:
            if not a["passed"]:
                self.failures.append(
                    f"{self.transport} {step_id}: {a['claim']} ({a.get('observed')})"
                )
        meta = next(s for s in STEPS if s["id"] == step_id)
        self.steps[step_id] = {"id": step_id, "actor": meta["actor"], "via": via,
                               "operation": meta.get("service", meta["operation"])
                               if via == "owner-service" else meta["operation"],
                               "outcome": outcome, "answer_sha256": digest(normalise(answer)),
                               "store_assertions": assertions}  # fmt: skip

    def ordered(self) -> list[dict[str, Any]]:
        return [self.steps[i] for i in SCRIPT_IDS if i in self.steps]


VOLATILE = ("request_id", "created_at", "expires_at", "recorded_at", "decided_at", "used_at",
            "confirmed_at", "granted_at", "default_at", "started_at", "ended_at", "updated_at",
            "run_id", "elapsed_ms", "duration_ms", "store", "path_hint")  # fmt: skip
ID = re.compile(r"\b([a-z]+)_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
HEX = re.compile(r"\b(ol_|bl_|cap_)?[0-9a-f]{24,64}\b")
WHEN = re.compile(r"\b\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?")


# Lists whose order between items made in the same transaction or second is their random ids'
# order (the map's unplaced questions and tracks; cards created in the same second): compared as
# sets. Every other list keeps its order (the plan's cheapest-first order among them).
UNORDERED = ("unplaced_questions", "tracks", "cards", "recommended")


def normalise(value: Any) -> Any:
    """Answers without ids, request ids, digests of plans or times (they differ between stores).
    The project sequence and every other value stay."""
    if isinstance(value, dict):
        return {k: ordered(normalise(v)) if k in UNORDERED and isinstance(v, list)
                else normalise(v)
                for k, v in sorted(value.items()) if k not in VOLATILE}  # fmt: skip
    if isinstance(value, list):
        return [normalise(v) for v in value]
    if isinstance(value, str):
        value = ID.sub(r"\1_<id>", value)
        value = UUID.sub("<uuid>", value)
        value = HEX.sub(lambda m: f"{m.group(1) or ''}<hex>", value)
        return WHEN.sub("<time>", value)
    return value


# ----- the owner's steps on the store's side ----------------------------------------------


def outline_of(st: Store, gen: int) -> tuple[dict[str, Any], dict[str, Any]]:
    outline = st.svc.adoption_outline(st.pid, st.owner, gen)
    items = outline["items"]

    def item(kind: str, title: str) -> dict[str, Any]:
        return next((i for i in items if i["kind"] == kind and i["title"] == title), {})

    edits = {"drop": [], "titles": {}, "draws_on": {},
             "addresses": {item("question", RQ1).get("item_id", "oi_0000000000000000"):
                           item("deliverable", PAPER).get("item_id"),
                           item("question", CLUTCH).get("item_id", "oi_0000000000000001"):
                           None}}  # fmt: skip
    return outline, edits


def s1_checks(st: Store) -> list[dict[str, Any]]:
    scans = st.rows("SELECT scan_gen FROM adoption_scans")
    gen = max(int(r["scan_gen"]) for r in scans) if scans else 0
    outline = st.svc.adoption_outline(st.pid, st.owner, gen) if gen else {"items": []}
    items = outline["items"]
    kinds = lambda kind: [i for i in items if i["kind"] == kind]  # noqa: E731
    stale = kinds("stale_candidate")
    rmap = st.map()
    paper = st.deliverable()
    questions = {q["title"] for q in paper.get("questions", [])}
    unplaced = {q["title"] for q in rmap["unplaced_questions"]}
    records = dict(
        (r["kind"], r["n"])
        for r in st.rows("SELECT kind, COUNT(*) AS n FROM records GROUP BY kind")
    )  # fmt: skip
    return [
        check("one scan of the folder", len(scans) == 1, len(scans)),
        check("the outline proposes the tracks track2 and track5",
              sorted(i["title"] for i in kinds("track")) == GOLDEN["tracks"]),
        check("the outline proposes the paper",
              [i["title"] for i in kinds("deliverable")] == [PAPER]),
        check("the outline proposes the three questions",
              sorted(i["title"] for i in kinds("question")) == sorted(GOLDEN["questions"])),
        check("every outline item cites its file", all(i["cites"] for i in items), len(items)),
        check("the stale candidate is track5/energy.csv, older than track5/run_sim.m",
              [(i.get("path"), i.get("producer")) for i in stale]
              == [(GOLDEN["stale_result"], GOLDEN["stale_producer"])],
              [(i.get("path"), i.get("producer")) for i in stale]),
        check("the outline was confirmed once (one confirmation, one transaction)",
              len(st.rows("SELECT outline_id FROM adoption_outlines")) == 1),
        check("the confirmation recorded the paper, the three questions and both tracks",
              records.get("paper") == 1 and sorted(t["title"] for t in rmap["tracks"])
              == GOLDEN["tracks"], records),
        check("RQ1 is under the paper (the owner's correction), alone",
              questions == {RQ1}, sorted(questions)),
        check("the clutch question is placed nowhere (the owner's correction)",
              CLUTCH in unplaced, sorted(unplaced)),
        check("nothing was written into the adopted folder",
              folder_state(st.study) == st.folder_before),
    ]  # fmt: skip


def s3_checks(st: Store) -> list[dict[str, Any]]:
    status = st.status()
    paper = st.deliverable()
    rq1 = next((q for q in paper.get("questions", []) if q["title"] == RQ1), {"claims": []})
    shown = {c["claim_id"]: c for c in rq1["claims"]}
    found = {k: (c["status"], c["basis"], c["causes"][0]["code"] if c["causes"] else None)
             for k, c in status.items()}  # fmt: skip
    steps = {s["key"]: s for s in paper.get("steps", [])}
    verified = sorted(b["code"] for b in steps.get("verified", {}).get("blockers", []))
    old = status.get("saving_old", {"causes": []})
    uphill = status.get("uphill", {"causes": []})
    return [
        check("each claim's derived status, basis and first cause",
              found == S3_EXPECTED, found),
        check("all five claims are under RQ1 with the same status the map gives",
              all(shown.get(st.ids[k], {}).get("status") == status[k]["status"]
                  for k in status) and len(shown) == 5, len(shown)),
        check("the 18.2 % claim is settling: its correction is proposed",
              "correction_proposed" in {c["code"] for c in old["causes"]}),
        check("two definitions of uphill energy are not a conflict",
              "conflicting" not in {c["code"] for c in uphill["causes"]}
              and uphill.get("status") == "holds"),
        check("the deliverable is past framed: evidence mapped, next verified",
              (paper.get("stage"), paper.get("next")) == ("evidence_mapped", "verified"),
              (paper.get("stage"), paper.get("next"))),
        check("what keeps it from verified: the stale, settling and unverified claims",
              verified == ["settling", "stale", "unverified"], verified),
        check("agent-reported support is listed for the owner to confirm",
              sorted(c["line"] for c in paper.get("to_confirm", []))
              == sorted([LINE["track2"], LINE["uphill"]])),
    ]  # fmt: skip


def stale_details(st: Store) -> dict[str, Any]:
    paper = st.deliverable()
    for q in paper.get("questions", []):
        for c in q["claims"]:
            if c["claim_id"] == st.ids.get("track5"):
                return dict(c)
    return {}


def s4_expected(st: Store) -> dict[str, Any]:
    version = st.versions.get("track5/energy.csv", {})
    return {"sha12": str(version.get("sha256", ""))[:12], "bytes": version.get("byte_length")}


def s4_checks(st: Store) -> list[dict[str, Any]]:
    item = stale_details(st)
    cause = (item.get("causes") or [{}])[0]
    evidence = (item.get("details", {}).get("evidence") or [{}])[0]
    version = st.versions.get("track5/energy.csv", {})
    return [
        check("the stale claim's first cause is producer_changed, naming the script",
              (cause.get("code"), cause.get("ref"))
              == ("producer_changed", f"{ALIAS}:{GOLDEN['stale_producer']}"), cause),
        check("its verifiable: the cited file and line",
              (evidence.get("root_alias"), evidence.get("path"), evidence.get("locator"))
              == (ALIAS, GOLDEN["stale_result"],
                  {"type": "text", "start_line": 2, "end_line": 2}), evidence.get("locator")),
        check("with the imported version's digest and size",
              (evidence.get("sha256"), evidence.get("byte_length"))
              == (version.get("sha256"), version.get("byte_length")) and evidence.get("sha256"),
              evidence.get("byte_length")),
        check("the stale claim is not usable", item.get("usable") is False),
    ]  # fmt: skip


def wait_default(st: Store) -> None:
    due = datetime.fromisoformat(st.default_at.replace("Z", "+00:00"))
    while datetime.now(UTC) <= due + timedelta(seconds=1):
        time.sleep(0.5)


def s7_default_checks(st: Store, cards: dict[str, Any]) -> list[dict[str, Any]]:
    states = {c["card"]["what"]: c["default_state"] for c in cards.get("cards", [])}
    status = st.status()
    return [
        check("the second card's default date has passed: its default is in effect in views",
              states.get(SECOND["what"]) == "default_in_effect", states),
        check("the first card still waits", states.get(CARD["what"]) == "waiting", states),
        check("the passed default changed no record (record digest unchanged)",
              st.record_digest() == st.record_digest_before),
        check("its correction is still proposed and the claim still stands",
              st.body(st.ids["second"])["status"] == "proposed"
              and st.body(st.ids["track5"])["state"] == "proposed"),
        check("the track 5 claim still reads stale (the default changes nothing)",
              status["track5"]["status"] == "stale"),
        check("nothing was written into the adopted folder",
              folder_state(st.study) == st.folder_before),
    ]  # fmt: skip


def s7_answer_checks(st: Store, resumed: tuple[bool, Any]) -> list[dict[str, Any]]:
    ok, answer = resumed
    status = st.status()
    states = st.approvals()
    return [
        check("the agent resumed the granted plan once", ok, outcome_of(ok, answer)),
        check("the correction is applied", st.body(st.ids["correction"])["status"] == "applied"),
        check("the 18.2 % claim is superseded",
              (st.body(st.ids["saving_old"])["state"], status["saving_old"]["status"])
              == ("superseded", "superseded"), status["saving_old"]["status"]),
        check("the 17.4 % claim reads corrected, on its agent-reported check",
              (status["saving_new"]["status"], status["saving_new"]["basis"])
              == ("corrected", "agent_reported"), status["saving_new"]["status"]),
        check("the first card's request is used; the second card and the rerun still wait",
              states.get(st.ids["card_request"]) == "used"
              and states.get(st.ids["second_request"]) == "pending"
              and states.get(st.ids["rerun_request"]) == "pending", states),
        check("the stale claim still keeps the paper from verified",
              "stale" in {b["code"] for s in st.deliverable().get("steps", [])
                          if s["key"] == "verified" for b in s["blockers"]}),
    ]  # fmt: skip


def owner_s1_service(st: Store) -> dict[str, Any]:
    gen = int(st.svc.adoption_scan(st.pid, st.owner, root_alias=ALIAS, mode="wait")["scan_gen"])
    outline, edits = outline_of(st, gen)
    preview = st.svc.adoption_outline_preview(st.pid, st.owner, gen, outline["sha256"], edits)
    confirmed = st.svc.adoption_outline_confirm(st.pid, gen, outline["sha256"], edits, None,
                                                authority=st.act)  # fmt: skip
    return {
        "outline_counts": outline["counts"],
        "preview": preview,
        "confirmed": {k: confirmed[k] for k in ("records_created", "relations_added")},
    }


def owner_grant(st: Store) -> dict[str, Any]:
    approval = st.svc.approval_get(st.pid, st.owner, st.ids["card_request"])
    return st.svc.approval_decide(st.pid, st.ids["card_request"], grant=True,
                                  plan_sha256=approval["plan_sha256"], owner=st.act)  # fmt: skip


# ----- the agent's steps (both legs) ------------------------------------------------------------


class Agent:
    """The agent's MCP session and what it remembers between its calls."""

    def __init__(self, st: Store, chat: McpChat) -> None:
        self.st, self.chat = st, chat
        self.files: dict[str, dict[str, Any]] = {}

    async def call(self, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
        return await self.chat.call(tool, args)

    async def apply(self, ops: list[dict[str, Any]], card: dict[str, Any] | None = None
                    ) -> tuple[bool, Any]:  # fmt: skip
        from prometheos.domain.ids import new_request_id

        ok, info = await self.call("project_info", {})
        request: dict[str, Any] = {"schema_version": 1, "project_id": self.st.pid,
                                   "request_id": new_request_id(),
                                   "expected_seq": info["project_seq"] if ok else 0,
                                   "operations": ops}  # fmt: skip
        if card:
            request["card"] = card
        return await self.call("apply", {"request": request})

    def ev(self, path: str, line: int) -> dict[str, Any]:
        f = self.files[path]
        return {"source_id": f["source_id"], "version_id": f["version_id"],
                "locator": {"type": "text", "start_line": line, "end_line": line},
                "quote_sha256": None}  # fmt: skip

    def quantity(self, path: str, line: int, value: str, name: str, definition: str,
                 unit: str) -> dict[str, Any]:  # fmt: skip
        return {"decimal": value, "unit": unit, "source_ref": self.ev(path, line),
                "source_pointer": pointer(text_of(path), line,
                                          f"{value} {unit}" if unit else value),
                "name": name, "definition": definition}  # fmt: skip


class Calls:
    """One step's agent calls: each answer, its outcome and the outcome it should have."""

    def __init__(self) -> None:
        self.answers: dict[str, Any] = {}
        self.checks: list[dict[str, Any]] = []
        self.unexpected: list[str] = []

    def add(self, name: str, result: tuple[bool, Any], expect: str = "ok") -> Any:
        ok, answer = result
        outcome = outcome_of(ok, answer)
        self.answers[name] = {"outcome": outcome, "answer": answer}
        self.checks.append(check(f"{name}: {expect}", outcome == expect, outcome))
        if outcome != expect:
            self.unexpected.append(f"{name}={outcome}")
        return answer

    def outcome(self) -> str:
        return "ok" if not self.unexpected else "unexpected:" + ",".join(self.unexpected)


def create(kind: str, record_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"op": "create", "kind": kind, "id": record_id, "base_rev": 0, "sensitivity": "normal",
            "occurred_at": None, "body": body}  # fmt: skip


def agent_assessment(claim_id: str, checks: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
    return {"claim_ref": {"id": claim_id, "rev": 1}, "outcome": "supports",
            "rationale": rationale, "checks": checks, "author_ref": None,
            "method": "reported_agent", "scope_match": True}  # fmt: skip


async def agent_s2(st: Store, rec: Recorder, agent: Agent) -> None:
    from prometheos.domain.ids import new_id

    calls = Calls()
    rmap = calls.add("research_map", await agent.call("research_map", {}))
    rq1 = next((q["entry_id"] for d in (rmap or {}).get("deliverables", [])
                for q in d["questions"] if q["title"] == RQ1), "entry_missing")  # fmt: skip
    ok, view = await agent.call(
        "source_import_plan",
        {"root_alias": ALIAS, "paths": IMPORTS, "retention": "copy", "wait_seconds": 20},
    )
    ok, view = await agent.chat.finished(ok, view)
    plan = calls.add("source_import_plan", (ok, view))
    items = (plan or {}).get("result", plan) or {}
    ok, view = await agent.call("source_import_commit", {"plan_id": items.get("plan_id", "none"),
                                                         "wait_seconds": 20})  # fmt: skip
    calls.add("source_import_commit", await agent.chat.finished(ok, view))
    agent.files = {i["relative_path"]: i for i in items.get("items", [])}
    st.versions = {p: {"sha256": i.get("sha256"),
                       "byte_length": i.get("byte_length", i.get("size"))}
                   for p, i in agent.files.items()}  # fmt: skip
    for key, statement, path, line, value, name, definition, unit in CLAIMS:
        cid = new_id("claim")
        st.ids[key] = cid
        body = {
            "statement": statement,
            "scope": SCOPE,
            "evidence": [agent.ev(path, line)],
            "assessment_refs": [],
            "state": "proposed",
            "superseded_by": None,
            "quantities": [agent.quantity(path, line, value, name, definition, unit)],
        }
        calls.add(f"claim {key} under RQ1", await agent.apply([
            create("claim", cid, body),
            {"op": "relation_add", "source_ref": {"id": cid, "rev": 1},
             "target_ref": {"id": rq1, "rev": 1}, "relation": "answers"}]))  # fmt: skip
    pair = {"title": "Uphill energy on the published climb",
            "text": "The published climb gives another uphill energy: another definition.",
            "category": "finding", "evidence": [agent.ev(UP, 4)],
            "claim_refs": [{"id": st.ids["uphill"], "rev": 1}], "author_ref": None,
            "origin": "agent",
            "quantities": [agent.quantity(UP, 4, "63.6", "uphill energy", "published climb", "MJ")],
            "review": "unreviewed"}  # fmt: skip
    calls.add(
        "the definitional pair's second binding",
        await agent.apply([create("entry", new_id("entry"), pair)]),
    )
    for key in ("track2", "track5", "uphill"):
        calls.add(f"assessment of {key}", await agent.apply([create(
            "assessment", new_id("assessment"),
            agent_assessment(st.ids[key], [], "Re-read the stored output."))]))  # fmt: skip
    corr = new_id("correction")
    st.ids["correction"] = corr
    old, new = {"id": st.ids["saving_old"], "rev": 1}, {"id": st.ids["saving_new"], "rev": 1}
    calls.add("the correction of 18.2 %", await agent.apply([create("correction", corr, {
        "target_refs": [old], "replacement_refs": [new],
        "reason": "fuel_saving_v2.csv corrects the summary.", "evidence": [],
        "effects": [{"target_ref": old, "effect": "supersede", "replacement_ref": new,
                     "replacement_text": None}],
        "status": "proposed", "applied_seq": None})]))  # fmt: skip
    rec.add("S2", "mcp", calls.answers, calls.outcome(), [*calls.checks,
        check("five claims are recorded, each with its bound quantity",
              len(st.rows("SELECT id FROM records WHERE kind = 'claim'")) == 5),
        check("the correction waits for the owner (still proposed)",
              st.body(corr)["status"] == "proposed"),
        check("nothing was written into the adopted folder",
              folder_state(st.study) == st.folder_before),
    ])  # fmt: skip


async def agent_s5(st: Store, rec: Recorder, agent: Agent) -> None:
    from prometheos.domain.ids import new_id

    calls = Calls()
    calls.add("verification_propose", await agent.call("verification_propose", {}))
    plan = calls.add("verification_plan", await agent.call("verification_plan", {}))
    checks = (plan or {}).get("checks", [])
    mine = next((c for c in checks if c["claim_id"] == st.ids["saving_new"] and c["eligible"]), {})
    rerun = next((c for c in checks if c["kind"] == "rerun"), {})
    st.ids["check"], st.ids["rerun"] = mine.get("action_id", ""), rerun.get("action_id", "")
    approvals_before = len(st.approvals())
    calls.add(
        "start the eligible check",
        await agent.apply(
            [
                {
                    "op": "transition",
                    "id": st.ids["check"],
                    "base_rev": mine.get("rev", 1),
                    "to": "in_progress",
                    "reason": "Checking the cited summary now.",
                    "evidence": [],
                }
            ]
        ),
    )
    started = st.body(st.ids["check"])["status"] if st.ids["check"] else None
    no_approval = len(st.approvals()) == approvals_before
    evidence = agent.ev(NEW, 2)
    half = {k: v for k, v in CHECK.items() if k != "measured"} | {"measured": None}
    calls.add("a pass naming an expected value without a measured one",
              await agent.apply([create("assessment", new_id("assessment"), agent_assessment(
                  st.ids["saving_new"], [half | {"evidence": [evidence]}], "Re-read."))]),
              "error:validation_error")  # fmt: skip
    calls.add("the assessment with expected, measured and tolerance",
              await agent.apply([create("assessment", new_id("assessment"), agent_assessment(
                  st.ids["saving_new"], [CHECK | {"evidence": [evidence]}],
                  "Re-read the corrected summary."))]))  # fmt: skip
    current = st.svc.get(st.pid, st.owner, st.ids["check"], None)["rev"] if st.ids["check"] else 1
    calls.add(
        "mark the check done",
        await agent.apply(
            [
                {
                    "op": "transition",
                    "id": st.ids["check"],
                    "base_rev": current,
                    "to": "done",
                    "reason": "Re-read; the assessment records it.",
                    "evidence": [evidence],
                }
            ]
        ),
    )
    status = st.status()["saving_new"]
    assessments = st.svc.list_records(st.pid, st.owner, kind="assessment", limit=200)["items"]
    stored = [r for r in assessments if r["body"]["claim_ref"]["id"] == st.ids["saving_new"]]
    rec.add("S5", "mcp", calls.answers, calls.outcome(), [*calls.checks,
        check("the plan offers the 17.4 % claim's read-only check within the budget",
              (mine.get("kind"), mine.get("eligible"), mine.get("estimated_minutes"))
              == ("read_only", True, 10), mine),
        check("the rerun for the stale claim is planned and needs the owner",
              (rerun.get("cause"), rerun.get("eligible")) == ("producer_changed", False), rerun),
        check("the start was accepted without an approval request",
              started == "in_progress" and no_approval, started),
        check("the stored check carries expected, measured and tolerance",
              len(stored) == 1 and [{k: c.get(k) for k in ("expected", "measured", "tolerance")}
                                    for c in stored[0]["body"]["checks"]]
              == [{k: CHECK[k] for k in ("expected", "measured", "tolerance")}], len(stored)),
        check("the check is done", st.body(st.ids["check"])["status"] == "done"),
        check("the 17.4 % claim now holds on the agent's reported check",
              (status["status"], status["basis"]) == ("holds", "agent_reported"), status["status"]),
    ])  # fmt: skip


async def agent_s6(st: Store, rec: Recorder, agent: Agent) -> None:
    from prometheos.domain.ids import new_id

    calls = Calls()
    rev = st.svc.get(st.pid, st.owner, st.ids["rerun"], None)["rev"] if st.ids["rerun"] else 1
    asked = calls.add(
        "start the rerun",
        await agent.apply(
            [
                {
                    "op": "transition",
                    "id": st.ids["rerun"],
                    "base_rev": rev,
                    "to": "in_progress",
                    "reason": "Rerunning the changed script.",
                    "evidence": [],
                }
            ]
        ),
        "error:approval_required",
    )
    st.ids["rerun_request"] = str(((asked or {}).get("details") or {}).get("approval_request_id"))
    second = new_id("correction")
    st.ids["second"] = second
    target = {"id": st.ids["track5"], "rev": 1}
    calls.add("a correction withdrawing the track 5 claim", await agent.apply([create(
        "correction", second, {
            "target_refs": [target], "replacement_refs": [],
            "reason": "track5/run_sim.m changed after it wrote track5/energy.csv.",
            "evidence": [], "effects": [{"target_ref": target, "effect": "withdraw",
                                         "replacement_ref": None, "replacement_text": None}],
            "status": "proposed", "applied_seq": None})]))  # fmt: skip
    due = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=DEFAULT_DELAY)
    st.default_at = due.strftime("%Y-%m-%dT%H:%M:%SZ")
    asked = calls.add("its application, with a card whose default is seconds away",
                      await agent.apply([{"op": "correction_apply", "id": second, "base_rev": 1}],
                                        SECOND | {"default_at": st.default_at}),
                      "error:approval_required")  # fmt: skip
    st.ids["second_request"] = str(((asked or {}).get("details") or {}).get("approval_request_id"))
    # Last, the correction's application with its card: an approval request binds the project's
    # exact state, so the owner's grant can be resumed only while nothing else was written.
    asked = calls.add("the 18.2 % correction's application, with a decision card",
                      await agent.apply([{"op": "correction_apply", "id": st.ids["correction"],
                                          "base_rev": 1}], CARD),
                      "error:approval_required")  # fmt: skip
    st.ids["card_request"] = str(((asked or {}).get("details") or {}).get("approval_request_id"))
    ok, cards = await agent.call("decision_cards", {})
    calls.add("decision_cards", (ok, cards))
    states = {c["card"]["what"]: c["default_state"] for c in (cards or {}).get("cards", [])}
    st.record_digest_before = st.record_digest()
    rec.add("S6", "mcp", calls.answers, calls.outcome(), [*calls.checks,
        check("the rerun did not start", st.body(st.ids["rerun"])["status"] == "proposed"),
        check("two cards wait, the track 5 one's default not yet passed",
              states == {CARD["what"]: "waiting", SECOND["what"]: "waiting"}, states),
        check("each card carries what, why, recommendation and default",
              all(set(c["card"]) >= {"what", "why", "recommendation", "default_text"}
                  for c in (cards or {}).get("cards", []))),
        check("the 18.2 % claim now waits on its card",
              "card_waiting" in {c["code"] for c in st.status()["saving_old"]["causes"]}),
    ])  # fmt: skip


async def agent_resume(st: Store, agent: Agent) -> tuple[bool, Any]:
    return await agent.call("approval_resume", {"approval_id": st.ids["card_request"]})


# ----- the MCP leg -----------------------------------------------------------------------------


async def mcp_leg(st: Store, rec: Recorder) -> None:
    done = owner_s1_service(st)
    rec.add("S1", "owner-service", done, "ok", s1_checks(st))
    async with McpChat(st) as chat:  # type: ignore[arg-type]
        agent = Agent(st, chat)
        await agent_s2(st, rec, agent)
        rec.add("S3", "owner-service", normalise(st.map()), "ok", s3_checks(st))
        rec.add("S4", "owner-service", stale_details(st), "ok", s4_checks(st))
        await agent_s5(st, rec, agent)
        await agent_s6(st, rec, agent)
        wait_default(st)
        ok, cards = await agent.call("decision_cards", {})
        before = s7_default_checks(st, cards if ok else {})
        granted = owner_grant(st)
        resumed = await agent_resume(st, agent)
        rec.add("S7", "owner-service", {"cards": cards, "granted": granted,
                                        "resumed": resumed[1]}, "ok",
                before + s7_answer_checks(st, resumed))  # fmt: skip


# ----- the hub leg -----------------------------------------------------------------------------


class Leg:
    def __init__(self, signals: Path, browser: subprocess.Popen[str]) -> None:
        self.signals, self.browser = signals, browser

    async def wait(self, name: str, seconds: float = 300) -> dict[str, Any]:
        path = self.signals / f"{name}.json"
        deadline = time.monotonic() + seconds
        while not path.exists():
            if self.browser.poll() is not None:
                raise RuntimeError(f"the browser spec ended before it sent {name}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the browser spec did not send {name} in time")
            await asyncio.sleep(0.25)
        await asyncio.sleep(0.1)
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data

    def send(self, name: str, data: Any = None) -> None:
        temp = self.signals / f"{name}.tmp"
        temp.write_text(json.dumps(data or {}), encoding="utf-8")
        temp.replace(self.signals / f"{name}.json")


def browser_checks(step_id: str, shown: dict[str, Any],
                   expected: dict[str, Any] | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """What the owner saw in the hub, judged against the story (and, for S4, the stored values)."""
    if not shown:
        return [check("the browser spec recorded this step", False)]
    if step_id == "S1":
        return [check("the outline shows tracks track2 and track5",
                      sorted(shown.get("tracks", [])) == GOLDEN["tracks"]),
                check("and the paper", shown.get("deliverables") == [PAPER]),
                check("and the three questions",
                      sorted(shown.get("questions", [])) == sorted(GOLDEN["questions"])),
                check("and the stale candidate with its script",
                      shown.get("stale") == [f"{GOLDEN['stale_result']} is older than its script "
                                             f"{GOLDEN['stale_producer']}"]),
                check("the preview counted the corrected outline before confirming",
                      "1 paper(s)" in str(shown.get("counts")) and "0 left out" in
                      str(shown.get("counts"))),
                check("the hub says it is confirmed and offers no second confirmation",
                      shown.get("confirmed") is True
                      and shown.get("confirm_buttons") == 0)]  # fmt: skip
    if step_id == "S3":
        claims = {c.get("line"): c for q in shown.get("questions", []) if q.get("title") == RQ1
                  for c in q.get("claims", [])}  # fmt: skip
        seen = {k: claims.get(LINE[k], {}).get("status") for k in LINE}
        texts = {k: str(claims.get(LINE[k], {}).get("text", "")) for k in LINE}
        return [check("each claim under RQ1 shows its derived status",
                      seen == {k: v[0] for k, v in S3_EXPECTED.items()}, seen),
                check("each status in words, with its basis where it holds",
                      all(texts[k].startswith(HUB_STATUS[S3_EXPECTED[k][0]]) for k in LINE)
                      and all(AGENT_BASIS in texts[k] for k in ("track2", "uphill"))),
                check("the stale and settling claims say why",
                      "changed since the evidence was written" in texts["track5"]
                      and "waits for your decision" in texts["saving_old"], texts["track5"]),
                check("the deliverable's stage: evidence mapped, next verified",
                      str(shown.get("stage", "")).startswith("Evidence mapped · next: Verified"),
                      shown.get("stage")),
                check("what keeps it from verified names the three claims",
                      all(any(LINE[k] in b for b in shown.get("blockers", []))
                          for k in ("track5", "saving_old", "saving_new"))),
                check("agent-reported support is listed to confirm",
                      sorted(shown.get("to_confirm", [])) == sorted([LINE["track2"],
                                                                     LINE["uphill"]]))]  # fmt: skip
    if step_id == "S4":
        expected = expected or {}
        text = str(shown.get("evidence", ""))
        why = " ".join(shown.get("why", []))
        return [check("the cause names the script that changed after the run",
                      f"{ALIAS}:{GOLDEN['stale_producer']}" in why
                      and "changed after it wrote" in why, why),
                check("the verifiable shows the file and line",
                      f"{ALIAS}/{GOLDEN['stale_result']}" in text and "line 2" in text, text),
                check("with the stored digest and size",
                      bool(expected.get("sha12")) and f"sha256 {expected.get('sha12')}" in text
                      and f"{expected.get('bytes')} bytes" in text, text)]  # fmt: skip
    if step_id == "S7":
        after = shown.get("after", {})
        return [check("the second card says its default is in effect",
                      "in effect in views since" in str(shown.get("second_default"))),
                check("the first card still waits",
                      "in effect" not in str(shown.get("first_default", "in effect"))),
                check("the owner granted exactly the first card's plan",
                      shown.get("granted") is True),
                check("after the agent resumed it the 17.4 % claim reads corrected",
                      after.get("saving_new") == "corrected", after),
                check("and the 18.2 % claim is not offered as usable",
                      after.get("saving_old") in (None, "superseded"), after),
                check("its check shows expected, measured and tolerance",
                      all(CHECK[k] in str(shown.get("checks", ""))
                          for k in ("expected", "measured", "tolerance")),
                      shown.get("checks")),
                check("the answered card is gone, the second still waits",
                      shown.get("cards_after") == [SECOND["what"]],
                      shown.get("cards_after"))]  # fmt: skip
    return []


async def hub_story(st: Store, rec: Recorder, leg: Leg, owner: dict[str, Any]) -> None:
    await leg.wait("ready", 300)
    leg.send("adopt")
    await leg.wait("adopted", 300)
    owner["S1"] = s1_checks(st)
    async with McpChat(st) as chat:  # type: ignore[arg-type]
        agent = Agent(st, chat)
        await agent_s2(st, rec, agent)
        leg.send("map")
        await leg.wait("map-seen", 180)
        owner["S3"] = s3_checks(st)
        owner["S4"] = s4_checks(st)
        owner["S4-expected"] = [s4_expected(st)]  # handed to browser_checks
        await agent_s5(st, rec, agent)
        await agent_s6(st, rec, agent)
        leg.send("cards", {"default_at": st.default_at})
        await leg.wait("default-seen", 180)
        ok, cards = await agent.call("decision_cards", {})
        owner["S7"] = s7_default_checks(st, cards if ok else {})
        leg.send("grant")
        await leg.wait("granted", 120)
        resumed = await agent_resume(st, agent)
        owner["S7"] += s7_answer_checks(st, resumed)
        leg.send("resumed")
        await leg.wait("after-seen", 180)
    leg.send("done")


def hub_leg(st: Store, out: Path, rec: Recorder) -> dict[str, Any]:
    from prometheos import launcher

    try:
        require_headless_shell(ROOT / "hub")
        playwright = playwright_command(ROOT / "hub")
    except BrowserUnavailable as exc:
        raise RuntimeError(f"the browser leg cannot run: {exc}") from exc
    env = {**os.environ, "PROMETHEOS_HOME": str(st.home), "BROWSER": "none"}
    started = subprocess.run(
        [sys.executable, "-m", "prometheos", "start", "--no-open", "--project", st.pid, "--json"],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    if started.returncode != 0:
        raise RuntimeError(f"prometheos start failed: {started.stderr[-600:]}")
    url = json.loads(started.stdout)["url"]
    host = url.split("//", 1)[1].split(":", 1)[0]
    port = int(json.loads((st.home / "launcher" / "hub.json").read_text())["port"])
    code = launcher._owner_call(st.home, port, "code")["code"]
    signals = out / "signals"
    signals.mkdir()
    steps_path = out / "hub-steps.jsonl"
    info = {"url": url, "pair_url": f"{url}#pair={code}", "signals": str(signals),
            "shots": str(out / "screenshots"), "steps": str(steps_path)}  # fmt: skip
    info_path = out / "hub-info.json"
    info_path.write_text(json.dumps(info), encoding="utf-8")
    pw_env = {**playwright_environment(), "WALK_INFO": str(info_path),
              "WALK_PW_OUT": tempfile.mkdtemp(prefix="prometheos-walk20-pw-"),
              "BOUNDARY_EVIDENCE": str(out / "browser-boundary.json"),
              "PLAYWRIGHT_JSON_OUTPUT_FILE": str(out / "playwright-report.json"),
              "PROMETHEOS_BOUNDARY_HOME_HOST": host}  # fmt: skip
    owner: dict[str, list[dict[str, Any]]] = {}
    code_exit = None
    try:
        with open(out / "playwright.log", "w", encoding="utf-8") as log:
            browser = subprocess.Popen(
                [*playwright, "-c", "playwright.walk20.config.ts", "--reporter=line,json"],
                cwd=ROOT / "hub", env=pw_env, stdout=log, stderr=subprocess.STDOUT, text=True,
            )  # fmt: skip
            try:
                asyncio.run(hub_story(st, rec, Leg(signals, browser), owner))
                code_exit = browser.wait(timeout=300)
            except Exception as exc:
                inner = getattr(exc, "exceptions", None)  # the MCP client's task group
                rec.failures += [f"hub: {e!r}" for e in inner] if inner else [f"hub: {exc}"]
            finally:
                if browser.poll() is None:
                    browser.kill()
    finally:
        subprocess.run([sys.executable, "-m", "prometheos", "stop", "--json"], env=env,
                       capture_output=True, timeout=60, check=False)  # fmt: skip
    if code_exit != 0:
        rec.failures.append(f"hub: the Playwright walk failed (exit {code_exit})")
        print((out / "playwright.log").read_text(encoding="utf-8")[-3000:])
    _, problems = boundary_evidence(out)
    rec.failures += problems
    seen: dict[str, dict[str, Any]] = {}
    if steps_path.exists():
        for line in steps_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                step = json.loads(line)
                seen[step["id"]] = step
    expected = (owner.get("S4-expected") or [{}])[0]
    for step_id in BROWSER_IDS:
        asserted = owner.get(step_id, []) + browser_checks(step_id, seen.get(step_id, {}), expected)
        rec.add(step_id, "hub", seen.get(step_id, {}),
                "ok" if all(a["passed"] for a in asserted) else "error", asserted)  # fmt: skip
    return {"url": url}


# ----- orchestration ---------------------------------------------------------------------------


def judge(doc: dict[str, Any], outcomes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    equivalence: dict[str, Any] = {}
    failures: list[str] = []
    difference = first_difference(outcomes["hub"], outcomes["mcp"])
    equivalence["stored outcomes"] = "equal" if difference is None else difference
    if difference is not None:
        failures.append(f"stored outcomes differ: {difference}")
    one = {s["id"]: s for s in doc["hub"]["steps"]}
    two = {s["id"]: s for s in doc["mcp"]["steps"]}
    missing = [i for i in SCRIPT_IDS if i not in one or i not in two]
    if missing:
        failures.append(f"steps missing: {missing}")
    equivalence["scripted steps present on both legs"] = "all" if not missing else missing
    both = [i for i in SCRIPT_IDS if i in one and i in two]
    differing = {i: (one[i]["outcome"], two[i]["outcome"]) for i in both
                 if one[i]["outcome"] != two[i]["outcome"]}  # fmt: skip
    if differing:
        failures.append(f"step outcomes differ: {differing}")
    equivalence["step outcomes"] = "equal" if not differing else differing
    unequal = [i for i in both if i in AGENT_IDS
               and one[i]["answer_sha256"] != two[i]["answer_sha256"]]  # fmt: skip
    if unequal:
        failures.append(f"agent answers differ at {unequal}")
    equivalence["normalised agent answers"] = "equal" if not unequal else unequal
    return equivalence, failures


def transcript_lines(doc: dict[str, Any], failures: list[str]) -> list[str]:
    fixture = doc["fixture"]
    lines = ["# C20-WALK transcript", "", doc["summary"], "",
             f"Fixture: {fixture['generator']} ({', '.join(fixture['cases'])})", ""]  # fmt: skip
    for leg in ("hub", "mcp"):
        for s in (doc.get(leg) or {}).get("steps", []):
            lines.append(f"- {leg} {s['id']} ({s['actor']} via {s['via']}) [{s['outcome']}] "
                         f"{s['operation']}")  # fmt: skip
            lines += [f"  - [{'pass' if a['passed'] else 'FAIL'}] {a['claim']}"
                      for a in s["store_assertions"]]  # fmt: skip
    equivalence = doc.get("equivalence") or {}
    lines += ["", "- S8 equivalence of the two stores:"]
    lines += [f"  - {k}: {v}" for k, v in equivalence.items()] or ["  - not judged (one leg)"]
    lines += ["- S9 this transcript, transcript.json, outcome-*.json, evidence/ and screenshots/",
              "", *[f"- FAIL {f}" for f in failures]]  # fmt: skip
    return lines


def candidate() -> dict[str, Any]:
    """The commit the walk ran on, and whether the tree had uncommitted changes (and their
    digest), so the evidence binds one candidate (AGENTS.md)."""

    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
        return done.stdout

    status = git("status", "--porcelain")
    # The raw diff's own sha256: digest() canonicalises JSON values and refuses bytes (the
    # papers-1 integration run, whose receipts leave the tree dirty, found this).
    diff = git("diff", "HEAD").encode("utf-8")
    return {
        "commit": git("rev-parse", "HEAD").strip(),
        "dirty": bool(status.strip()),
        "dirty_diff_sha256": hashlib.sha256(diff).hexdigest() if status.strip() else None,
    }


def redact_pairing(path: Path) -> None:
    """Review cp20-a-r2 A6: the hub's one-time pairing code never stays in kept evidence."""
    if not path.exists():
        return
    info = json.loads(path.read_text(encoding="utf-8"))
    if "pair_url" in info:
        info["pair_url"] = str(info["pair_url"]).split("#", 1)[0] + "#pair=(spent; not kept)"
    path.write_text(json.dumps(info), encoding="utf-8")


def run(out: Path | None, only: str | None, keep: bool, show: bool) -> int:
    out = (out or Path(tempfile.mkdtemp(prefix="prometheos-walk20-out-"))).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into a folder that is not empty: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prometheos-walk20-"))
    doc: dict[str, Any] = {
        "walk": "C20-WALK",
        "script": STEPS,
        "walk_steps": WALK_STEPS,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # Review cp20-a-r2 A5: the evidence names the exact candidate it ran on.
        "candidate": candidate(),
        "fixture": {
            "generator": "tools/research_map_fixture.py",
            "cases": [
                "per-track forks",
                "a stale run",
                "a definitional pair",
                "a corrected number",
            ],
        },
    }
    failures: list[str] = []
    outcomes: dict[str, Any] = {}
    try:
        for leg in ("hub", "mcp"):
            if only not in (None, leg):
                continue
            st = Store(work / f"store-{leg}")
            rec = Recorder(leg, out / "evidence" / leg)
            if leg == "hub":
                try:
                    doc["hub_leg"] = hub_leg(st, out, rec)
                except RuntimeError as exc:
                    rec.failures.append(f"hub: {exc}")
            else:
                asyncio.run(mcp_leg(st, rec))
            try:
                outcomes[leg] = snapshot(st)
            except Exception as exc:  # a broken leg still leaves its transcript
                outcomes[leg] = {"error": repr(exc)}
                rec.failures.append(f"{leg}: the stored outcome could not be read ({exc!r})")
            (out / f"outcome-{leg}.json").write_text(
                json.dumps(outcomes[leg], indent=2, sort_keys=True, default=str), encoding="utf-8"
            )
            doc[leg] = {"steps": rec.ordered()}
            failures += rec.failures
        if "hub" in outcomes and "mcp" in outcomes:
            equivalence, problems = judge(doc, outcomes)
            doc["equivalence"] = equivalence
            failures += problems
        doc["failures"] = failures
        doc["summary"] = ("PASS: every step's assertions held and the two stores reached "
                          "equivalent outcomes." if not failures and only is None else
                          f"{'FAIL' if failures else 'PASS'}: "
                          f"{len(failures)} problem(s)")  # fmt: skip
        redact_pairing(out / "hub-info.json")
        (out / "transcript.json").write_text(json.dumps(doc, indent=2, default=str),
                                             encoding="utf-8")  # fmt: skip
        lines = transcript_lines(doc, failures)
        (out / "transcript.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if show:
            print("\n".join(lines))
        print(doc["summary"])
        for failure in failures:
            print(" -", failure)
        print("transcript:", out / "transcript.json")
        return 0 if not failures else 1
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", choices=("hub", "mcp"))
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--print-transcript", action="store_true")
    args = parser.parse_args(argv)
    return run(args.out, args.only, args.keep, args.print_transcript)


if __name__ == "__main__":
    sys.exit(main())
