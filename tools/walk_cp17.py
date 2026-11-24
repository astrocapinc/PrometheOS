"""C17-WALK: the CP17 end-to-end walk (spec/10 CP17.6; checkpoints.json C17-WALK).

    uv run python tools/walk_cp17.py [--out DIR] [--print-transcript] [--only hub|mcp] [--keep]

On the built app with a fresh store and the seeded synthetic corpus (tools/adoption_corpus.py,
seed 17) as an approved research root:

1. **Hub half (store A).** The real hub (the packaged assets, started as ``prometheos hub
   serve`` starts) serves store A. A Playwright spec (``hub/walk17/walk.spec.ts``, Playwright's
   Chromium headless shell inside the loopback-only boundary of decision 0028, proved first by
   ``hub/walk17/00-boundary.spec.ts``) drives the owner: scan the folder, review a family, make
   the consolidation plan. While the hub keeps running, the agent's session over the real
   ``prometheos mcp serve --stdio`` process (the official MCP SDK client) reads the plan,
   reports two plan steps and asks to accept a baseline (an approval request). The owner then
   chooses a family's current file and accepts the baseline in the hub. The harness changes the
   folder (one note edited, the chosen file moved, one note deleted, one added); the owner scans
   again in the hub and sees the changes; the agent reads the drift from the baseline over MCP.
2. **MCP half (store B).** On a second fresh store, with the hub stopped, the same script runs
   with every agent step over MCP and the owner's steps through the service in the harness (a
   synthetic owner channel, as in C10-WALK and C18-WALK).
3. The two stores' normalised outcomes (scans, families, plan steps, work items, decisions,
   baselines, approvals, drift) must be equal, as must every step's outcome and the normalised
   answer of every agent step.

Output in ``--out`` (a new folder): ``transcript.json`` and ``transcript.md``,
``outcome-hub.json`` and ``outcome-mcp.json``, ``evidence/``, ``browser-boundary.json``,
``playwright-report.json`` and screenshots. Exit 0 only when every assertion passed and the
outcomes are equal. Synthetic data only; nothing is sent anywhere.
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

from adoption_corpus import generate  # noqa: E402
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

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

SEED = 17
ALIAS = "corpus"
FAMILY_KEY = "data:sweep"
EDITED = "notes/decision-log.md"
DELETED = "notes/Zusammenfassung_Résumé_ß.md"
ADDED = "notes/new-idea.md"
MOVED_INTO = "results/archive_moved/"
DEDUPE_TEXT = (
    "Nothing deleted. Each group of identical files has one copy outside backup folders; "
    "the researcher may remove the others."
)
CONVENTION_TEXT = (
    "Use v1, v2, ... in names instead of FINAL or BEST, and keep a dated note of which file is "
    "current."
)

STEPS: list[dict[str, str]] = [
    {"id": "W1.1", "actor": "owner", "operation": "adoption_scan"},
    {"id": "W1.2", "actor": "owner", "operation": "adoption_family"},
    {"id": "W1.3", "actor": "owner", "operation": "adoption_plan_create"},
    {"id": "W2.1", "actor": "agent", "operation": "adoption_read#plan"},
    {"id": "W2.2", "actor": "agent", "operation": "adoption_manage#step_report"},
    {"id": "W2.3", "actor": "agent", "operation": "adoption_manage#step_report"},
    {"id": "W2.4", "actor": "agent", "operation": "adoption_manage#baseline_accept"},
    {"id": "W3.1", "actor": "owner", "operation": "adoption_decide"},
    {"id": "W3.2", "actor": "owner", "operation": "adoption_baseline_accept"},
    {"id": "W4.1", "actor": "harness", "operation": "change the folder"},
    {"id": "W5.1", "actor": "owner", "operation": "adoption_scan"},
    {"id": "W5.2", "actor": "agent", "operation": "adoption_read#drift"},
]
SCRIPT_IDS = [s["id"] for s in STEPS]
AGENT_IDS = {s["id"] for s in STEPS if s["actor"] == "agent"}

STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BASELINE = re.compile(r"bl_[0-9a-f]{24}")
DECISION = re.compile(r"dec_[0-9a-f]{24}")
ROOT_DIGEST = re.compile(r'"root_digest(?:_before|_after)?": "[0-9a-f]{64}"')
PLAN_DIGEST = re.compile(r'"plan_sha256": "[0-9a-f]{64}"')


class Store:
    """A fresh project store with the seeded corpus as its approved root."""

    def __init__(self, work: Path) -> None:
        from prometheos.app.reads import Caller
        from prometheos.app.service import Service

        self.work = work
        self.corpus = work / "corpus"
        self.golden = generate(self.corpus, SEED)
        self.home = work / "home"
        self.svc = Service(home=self.home, actor="hub")
        self.owner = Caller(actor="hub", scope="owner")
        self.pid: str = self.svc.init_project(
            title="Synthetic study (C17-WALK)",
            objective="Adopt an existing working folder without changing it.",
            roots={ALIAS: str(self.corpus)},
        )["project_id"]
        self.choice = next(
            f["current"] for f in self.golden["families"] if f["id"] == "sweep_results"
        )

    def norm(self, value: Any) -> Any:
        """Times, UUIDs (run, approval and work-item IDs), baseline and decision IDs and root
        digests (they include the harness's own file times) become placeholders."""
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        text = text.replace(self.pid, "<project>")
        text = ROOT_DIGEST.sub('"root_digest": "<root-digest>"', text)
        # an approval plan binds the project ID, so its digest differs between stores
        text = PLAN_DIGEST.sub('"plan_sha256": "<plan-digest>"', text)
        text = BASELINE.sub("<baseline>", text)
        text = DECISION.sub("<decision>", text)
        text = STAMP.sub("<time>", text)
        text = UUID.sub("<uuid>", text)
        return json.loads(text)

    def rows(self, sql: str) -> list[sqlite3.Row]:
        path = Path(self.svc.open(self.pid).db_path)
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(sql))

    def latest_gen(self) -> int:
        return int(self.svc.adoption_scans(self.pid, self.owner)["scans"][0]["scan_gen"])

    def family(self, gen: int, key: str) -> dict[str, Any]:
        listed = self.svc.adoption_families(self.pid, self.owner, gen, limit=500)["families"]
        found = next(f for f in listed if f["key"] == key)
        return self.svc.adoption_family(self.pid, self.owner, gen, found["family_id"])

    def change_folder(self) -> dict[str, Any]:
        """W4.1: the researcher's own changes (edit, move, delete, add)."""
        edited = self.corpus / EDITED
        edited.write_text(edited.read_text("utf-8") + "\n- 2026-04-01: rerun later.\n", "utf-8")
        target = self.corpus / (MOVED_INTO + self.choice.rsplit("/", 1)[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.corpus / self.choice), str(target))
        (self.corpus / DELETED).unlink()
        (self.corpus / ADDED).write_text("# A new idea\n\nTry a finer sweep.\n", "utf-8")
        return {
            "edited": EDITED,
            "moved": [self.choice, MOVED_INTO + self.choice.rsplit("/", 1)[-1]],
            "deleted": DELETED,
            "added": ADDED,
        }


# ----- the normalised outcome of a store ----------------------------------------------------


def snapshot(st: Store) -> dict[str, Any]:
    out: dict[str, Any] = {}
    scans = st.svc.adoption_scans(st.pid, st.owner)["scans"]
    out["scans"] = sorted(
        [
            s["scan_gen"],
            s["state"],
            (s["summary"] or {}).get("files_on_disk"),
            (s["summary"] or {}).get("entries"),
            s["root_digest_before"] == s["root_digest_after"],
        ]
        for s in scans
    )
    gen = st.latest_gen()
    families = []
    for f in st.svc.adoption_families(st.pid, st.owner, gen, limit=500)["families"]:
        family = st.svc.adoption_family(st.pid, st.owner, gen, f["family_id"])
        families.append(
            [
                family["key"],
                sorted(m["path"] for m in family["members"]),
                family["current"],
                family["band"],
                family["deciding_rule"],
                len(family["conflicts"]),
            ]
        )
    out["families"] = sorted(families, key=canon)
    plan = st.svc.adoption_plan(st.pid, st.owner, gen)
    out["plan"] = {
        "version": plan["version"],
        "steps": sorted(
            [s["step"]["step_id"], s["step"]["kind"], s["state"]] for s in plan["steps"]
        ),
    }
    out["work_items"] = sorted(
        [r["body"]["title"], r["body"]["status"]]
        for r in st.svc.list_records(st.pid, st.owner, kind="action", limit=200)["items"]
    )
    out["decisions"] = sorted(
        [r["family_id"], r["current_path"], r["current_sha256"]]
        for r in st.rows("SELECT * FROM adoption_decisions")
    )
    out["baselines"] = [
        [r["scan_gen"], r["plan_version"], json.loads(r["payload"])]
        for r in st.rows("SELECT * FROM adoption_baselines ORDER BY scan_gen")
    ]
    out["approvals"] = sorted(
        [r["operation"], r["state"]]
        for r in st.rows("SELECT operation, state FROM approval_requests")
    )
    out["drift"] = st.svc.adoption_drift(st.pid, st.owner, gen)["counts"]
    return st.norm(out)


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


# ----- the agent (MCP) ---------------------------------------------------------------------


class McpChat:
    """One agent session: a real ``prometheos mcp serve --stdio`` process behind the official
    MCP SDK client."""

    def __init__(self, st: Store) -> None:
        from mcp import Client, StdioServerParameters

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "prometheos", "mcp", "serve", "--stdio", "--project", st.pid],
            env={**os.environ, "PROMETHEOS_HOME": str(st.home), "PYTHONIOENCODING": "utf-8"},
        )
        self.client = Client(params)

    async def __aenter__(self) -> McpChat:
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.__aexit__(*exc)

    async def call(self, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
        result = await self.client.call_tool(tool, args)
        if result.is_error:
            text = result.content[0].text  # type: ignore[union-attr]
            return False, json.loads(text[text.index("{") :])["error"]
        content = result.structured_content or {}
        # A family tool answers {op, result}; a hand-written tool answers its result.
        return True, content.get("result", content) if "op" in content else content


def outcome_of(ok: bool, answer: Any) -> str:
    return "ok" if ok else f"error:{answer.get('code', '?')}"


def _step(plan: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((s["step"] for s in plan["steps"] if s["step"]["kind"] == kind), None)


async def agent_plan_session(st: Store, rec: Recorder) -> None:
    """W2: the agent reads the plan, reports two proposal steps, and asks for the baseline."""
    gen = st.latest_gen()
    async with McpChat(st) as chat:
        request = {"request": {"op": "plan", "scan_gen": gen}}
        ok, plan = await chat.call("adoption_read", request)
        a = [
            check("the plan is readable over MCP", ok, outcome_of(ok, plan)),
            check(
                "it holds the dedupe and convention proposals",
                ok
                and _step(plan, "dedupe_proposal") is not None
                and _step(plan, "convention_proposal") is not None,
            ),
            check(
                "every step forbids writing in the adopted folder",
                ok
                and all(
                    "write_into_adopted_folder" in s["step"]["forbidden"] for s in plan["steps"]
                ),
            ),
        ]
        rec.add("W2.1", "mcp", request, plan, outcome_of(ok, plan), a)
        for step_id, kind, text in (
            ("W2.2", "dedupe_proposal", DEDUPE_TEXT),
            ("W2.3", "convention_proposal", CONVENTION_TEXT),
        ):
            step = _step(plan, kind) if ok else None
            result = {
                "summary": text.split(".")[0],
                "proposal": text,
                "paths": [i["path"] for i in (step or {}).get("inputs", [])][:50],
                "actions_taken": ["adoption_read"],
            }
            request = {
                "request": {
                    "op": "step_report",
                    "scan_gen": gen,
                    "step_id": (step or {}).get("step_id", "st_0"),
                    "result": result,
                }
            }
            ok2, answer = await chat.call("adoption_manage", request)
            stored = [
                r["state"]
                for r in st.rows("SELECT step_id, state FROM adoption_steps")
                if step and r["step_id"] == step["step_id"]
            ]
            rec.add(
                step_id,
                "mcp",
                request,
                answer,
                outcome_of(ok2, answer),
                [
                    check(
                        "the result was validated",
                        ok2 and answer.get("state") == "validated",
                        answer,
                    ),
                    check("the stored step is validated", stored == ["validated"], stored),
                ],
            )
        request = {"request": {"op": "baseline_accept", "scan_gen": gen, "note": "please"}}
        ok3, answer = await chat.call("adoption_manage", request)
        pending = [
            r["state"]
            for r in st.rows(
                "SELECT state FROM approval_requests WHERE operation = 'adoption_baseline_accept'"
            )
        ]
        baselines = st.rows("SELECT * FROM adoption_baselines")
        rec.add(
            "W2.4",
            "mcp",
            request,
            answer,
            outcome_of(ok3, answer),
            [
                check(
                    "an agent cannot accept a baseline: approval_required",
                    not ok3 and answer.get("code") == "approval_required",
                    answer.get("code"),
                ),
                check("the request waits for the owner", pending == ["pending"], pending),
                check("no baseline exists yet", len(baselines) == 0, len(baselines)),
            ],
        )


async def agent_drift_session(st: Store, rec: Recorder) -> None:
    """W5.2: the agent reads what changed since the accepted baseline."""
    gen = st.latest_gen()
    baseline_id = st.rows("SELECT baseline_id FROM adoption_baselines")[0]["baseline_id"]
    async with McpChat(st) as chat:
        request = {"request": {"op": "drift", "scan_gen": gen, "against": baseline_id}}
        ok, drift = await chat.call("adoption_read", request)
        moved = MOVED_INTO + st.choice.rsplit("/", 1)[-1]
        family = (
            next((f for f in (drift or {}).get("families", []) if f["key"] == FAMILY_KEY), None)
            if ok
            else None
        )
        rec.add(
            "W5.2",
            "mcp",
            request,
            drift,
            outcome_of(ok, drift),
            [
                check(
                    "the drift from the baseline is readable over MCP", ok, outcome_of(ok, drift)
                ),
                check(
                    "the chosen file is reported moved to its new place",
                    family is not None and family["change"] == "moved" and family["now"] == moved,
                    family,
                ),
                check(
                    "the counts name one edit, one move, one deletion, one addition (and the "
                    "baseline's own later scans)",
                    ok
                    and drift["counts"]["moved"] == 1
                    and drift["counts"]["deleted"] == 1
                    and drift["counts"]["changed"] >= 1,
                    drift.get("counts") if ok else None,
                ),
            ],
        )


# ----- the owner in the harness (MCP half) --------------------------------------------------


def owner_steps_before(st: Store, rec: Recorder) -> None:
    request = {"root_alias": ALIAS}
    scan = st.svc.adoption_scan(st.pid, st.owner, root_alias=ALIAS, mode="wait")
    families = st.svc.adoption_families(st.pid, st.owner, scan["scan_gen"])["total"]
    rec.add(
        "W1.1",
        "harness owner",
        request,
        scan,
        "ok" if scan["state"] == "complete" else "error",
        [
            check("the scan completed", scan["state"] == "complete", scan["state"]),
            check(
                "the folder was unchanged (root digests equal)",
                scan["root_digest_before"] == scan["root_digest_after"],
            ),
            check("families were found", families > 0, families),
        ],
    )
    family = st.family(scan["scan_gen"], FAMILY_KEY)
    rec.add(
        "W1.2",
        "harness owner",
        {"key": FAMILY_KEY},
        family,
        "ok",
        [
            check(
                "the family has members and reasons",
                len(family["members"]) > 1 and all("reasons" in m for m in family["members"]),
                len(family["members"]),
            ),
        ],
    )
    created = st.svc.adoption_plan_create(st.pid, st.owner, scan["scan_gen"])
    items = st.svc.list_records(st.pid, st.owner, kind="action", limit=200)["items"]
    rec.add(
        "W1.3",
        "harness owner",
        {"scan_gen": scan["scan_gen"]},
        created,
        "ok",
        [
            check("the plan has steps", created["steps"] > 0, created["steps"]),
            check(
                "each step is a proposed work item",
                len(items) == created["steps"]
                and all(r["body"]["status"] == "proposed" for r in items),
                len(items),
            ),
        ],
    )


def owner_steps_after(st: Store, rec: Recorder) -> None:
    from prometheos.app.authority import OwnerAct

    act = OwnerAct(channel="cli-interactive", actor="synthetic-owner")
    gen = st.latest_gen()
    family = st.family(gen, FAMILY_KEY)
    decided = st.svc.adoption_decide(
        st.pid, gen, family["family_id"], st.choice, None, authority=act
    )
    rec.add(
        "W3.1",
        "harness owner",
        {"family": FAMILY_KEY, "current": st.choice},
        decided,
        "ok",
        [
            check(
                "the chosen file is current by the owner's rule (R1)",
                decided["current"] == st.choice and decided["deciding_rule"] == "R1",
                [decided["current"], decided["deciding_rule"]],
            ),
        ],
    )
    accepted = st.svc.adoption_baseline_accept(st.pid, gen, None, authority=act)
    states = [
        r["state"]
        for r in st.rows(
            "SELECT state FROM adoption_steps WHERE kind IN "
            "('dedupe_proposal', 'convention_proposal')"
        )
    ]
    rec.add(
        "W3.2",
        "harness owner",
        {"scan_gen": gen},
        accepted,
        "ok",
        [
            check("a baseline was accepted", accepted["baseline_id"].startswith("bl_")),
            check(
                "the agent's validated results were accepted with it",
                states == ["accepted", "accepted"],
                states,
            ),
        ],
    )


def owner_rescan(st: Store, rec: Recorder) -> None:
    scan = st.svc.adoption_scan(st.pid, st.owner, root_alias=ALIAS, mode="wait")
    drift = st.svc.adoption_drift(st.pid, st.owner, scan["scan_gen"])
    rec.add("W5.1", "harness owner", {"root_alias": ALIAS}, drift, "ok", rescan_checks(scan, drift))


def rescan_checks(scan: dict[str, Any], drift: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        check(
            "the rescan completed without writing",
            scan["state"] == "complete" and scan["root_digest_before"] == scan["root_digest_after"],
        ),
        check(
            "one edit, one move, one deletion, one addition",
            drift["counts"] == {"added": 1, "changed": 1, "moved": 1, "deleted": 1},
            drift["counts"],
        ),
        check(
            "only what changed was analysed again",
            drift["analysis"]["extracted"] <= 3,
            drift["analysis"],
        ),
    ]


def harness_change(st: Store, rec: Recorder) -> None:
    changed = st.change_folder()
    rec.add(
        "W4.1",
        "harness",
        changed,
        changed,
        "ok",
        [
            check(
                "the researcher's changes are on disk",
                (st.corpus / ADDED).exists() and not (st.corpus / DELETED).exists(),
            ),
        ],
    )


async def mcp_phase(st: Store, rec: Recorder) -> None:
    owner_steps_before(st, rec)
    await agent_plan_session(st, rec)
    owner_steps_after(st, rec)
    harness_change(st, rec)
    owner_rescan(st, rec)
    await agent_drift_session(st, rec)


# ----- the hub half ----------------------------------------------------------------------------


def hub_phase(st: Store, out: Path, rec: Recorder) -> None:
    try:
        require_headless_shell(ROOT / "hub")
        playwright = playwright_command(ROOT / "hub")
    except BrowserUnavailable as exc:
        raise RuntimeError(f"the browser half cannot run: {exc}") from exc
    info_path, steps_path, signals = out / "hub-info.json", out / "hub-steps.json", out / "signals"
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
    try:
        deadline = time.monotonic() + 60
        while not info_path.exists():
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the walk hub did not start; see hub.log")
            time.sleep(0.2)
        time.sleep(0.5)
        fixture = {
            "alias": ALIAS,
            "family_key": FAMILY_KEY,
            "choice": st.choice,
            "moved": MOVED_INTO + st.choice.rsplit("/", 1)[-1],
        }
        env = {
            **playwright_environment(),
            "WALK_INFO": str(info_path),
            "WALK_STEPS": str(steps_path),
            "WALK_SIGNALS": str(signals),
            "WALK_SHOTS": str(out / "screenshots"),
            "WALK_PW_OUT": str(Path(tempfile.mkdtemp(prefix="prometheos-walk17-pw-"))),
            "BOUNDARY_EVIDENCE": str(out / "browser-boundary.json"),
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(out / "playwright-report.json"),
            "WALK_FIXTURE": json.dumps(fixture),
        }
        pw_log = open(out / "playwright.log", "w", encoding="utf-8")
        browser = subprocess.Popen(
            [*playwright, "-c", "playwright.walk17.config.ts", "--reporter=line,json"],
            cwd=ROOT / "hub",
            env=env,
            stdout=pw_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        handovers = [
            ("owner-1.done", "agent-1.done", lambda: asyncio.run(agent_plan_session(st, rec))),
            ("owner-2.done", "folder.done", lambda: harness_change(st, rec)),
            ("owner-3.done", "agent-2.done", lambda: asyncio.run(agent_drift_session(st, rec))),
        ]
        for wait_for, answer, run_it in handovers:
            deadline = time.monotonic() + 420
            while not (signals / wait_for).exists():
                if browser.poll() is not None or time.monotonic() > deadline:
                    break
                time.sleep(0.25)
            if not (signals / wait_for).exists():
                break
            try:
                run_it()
            except Exception as exc:
                (signals / "harness.failed").write_text(repr(exc), encoding="utf-8")
                raise
            (signals / answer).write_text("done", encoding="utf-8")
        try:
            code = browser.wait(timeout=600)
        finally:
            pw_log.close()
        if code != 0:
            rec.failures.append(f"hub: the Playwright walk failed (exit {code})")
            print((out / "playwright.log").read_text(encoding="utf-8")[-4000:])
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
    lines = ["# C17-WALK transcript", "", doc["summary"], ""]
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
    boundary = doc.get("browser_boundary")
    if boundary:
        lines += ["", "## Browser boundary (decision 0028)", ""]
        lines.append(
            f"- evidence: {boundary['evidence']}; all passed: {boundary.get('all_passed')}"
        )
        lines.append(f"- browser: {boundary.get('browser')}")
        for control, status in boundary.get("controls", {}).items():
            lines.append(f"- [{status}] {control}")
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in doc["limitations"]]
    if doc.get("failures"):
        lines += ["", "## Failures", ""] + [f"- {f}" for f in doc["failures"]]
    return "\n".join(lines) + "\n"


def judge(doc: dict[str, Any], outcomes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
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
    equivalence["step outcomes"] = "equal" if not differing else f"differ {differing}"
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


def run(out: Path | None, only: str | None, keep: bool, show: bool) -> int:
    out = (out or Path(tempfile.mkdtemp(prefix="prometheos-walk17-out-"))).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into a folder that is not empty: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prometheos-walk17-"))
    doc: dict[str, Any] = {
        "walk": "C17-WALK",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "script": SCRIPT_IDS,
        "corpus": {"generator": "adoption-corpus", "seed": SEED},
        "limitations": [
            "The MCP half's owner steps run through the service in the harness (a synthetic "
            "owner channel); the hub half's owner steps are the real browser.",
            "The corpus's planted cloud placeholders are ordinary files here (a cloud client's "
            "attributes cannot be set from user mode); the inventory tests prove placeholders.",
            "The agent is a scripted MCP client following the adopt-folder skill; real agent "
            "behaviour and the owner's real folder are E15.",
            "W4.1 (the researcher changing the folder) is done by the harness on disk.",
        ],
    }
    failures: list[str] = []
    outcomes: dict[str, Any] = {}
    equivalence: dict[str, Any] = {}
    try:
        if only in (None, "hub"):
            store_a = Store(work / "store-a")
            rec_a = Recorder("A", "hub", store_a, out / "evidence" / "hub")
            hub_phase(store_a, out, rec_a)
            outcomes["hub"] = snapshot(store_a)
            (out / "outcome-hub.json").write_text(
                json.dumps(outcomes["hub"], indent=2, sort_keys=True, ensure_ascii=False),
                encoding="utf-8",
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
            store_b = Store(work / "store-b")
            rec_b = Recorder("B", "mcp", store_b, out / "evidence" / "mcp")
            asyncio.run(mcp_phase(store_b, rec_b))
            outcomes["mcp"] = snapshot(store_b)
            (out / "outcome-mcp.json").write_text(
                json.dumps(outcomes["mcp"], indent=2, sort_keys=True, ensure_ascii=False),
                encoding="utf-8",
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
        if show:
            print(markdown)
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
