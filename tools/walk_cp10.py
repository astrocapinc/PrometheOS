"""C10-WALK: the CP10 end-to-end walk (spec/10 "End-to-end walks"; decision 0022 item 5).

    uv run python tools/walk_cp10.py [--out DIR] [--print-transcript] [--only hub|mcp] [--keep]

KERS scenarios K1-K4 run on the actual built app against a fresh store made from the synthetic
fixtures (baseline, plant result, meeting, manuscript):

1. **Hub half.** The real hub (the assets shipped in the package) serves store A. A Playwright
   spec (``hub/walk/walk.spec.ts``, Playwright's Chromium headless shell inside the loopback-only
   boundary of decision 0028) drives the owner's browser steps and an agent's HTTP steps (bearer
   token), asserting stored state after each. ``hub/walk/00-boundary.spec.ts`` runs first and
   proves the boundary with negative controls against reserved non-routable targets; its
   evidence (``browser-boundary.json``) must show every control passed.
2. **MCP half.** On a second fresh store B, with the hub stopped, the official MCP SDK client
   drives the real ``prometheos mcp serve --stdio`` process for the agent, in two chats (K4 closes
   the first). K2.3 uses a synthetic-test OwnerAct; K3.6 uses a SYNTHETIC-TEST capability.
   These test acts are not owner-route evidence; E09 and RPA-HEADLESS stay open.
3. The two stores' normalised outcomes (records, states, relations, flags, queues, approvals,
   runs, with IDs and timestamps normalised) and the agent's authority results must be equal.

Output in ``--out`` (a new folder): ``transcript.json`` (each step, the operation, the response
digests and the store assertion, for both halves), ``transcript.md`` (the same for a human),
``outcome-hub.json`` and ``outcome-mcp.json`` (the normalised outcomes), ``evidence/`` (the raw
responses), ``browser-boundary.json`` (the boundary controls and the journey worker's browser),
``playwright-report.json`` (Playwright's JSON report: which test ran in which worker; every walk
test must have run in a worker the journey guard checked) and the browser's screenshots.
Exit 0 only when every assertion passed and the outcomes are equal.

K2 commits nothing between the owner's grant and the agent's resume (decision 0020): an
approved apply plan is bound to the project sequence.

Synthetic fixtures only; nothing is sent anywhere.
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
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

import browser_shell  # noqa: E402
from browser_shell import (  # noqa: E402
    BrowserUnavailable,
    playwright_command,
    playwright_environment,
    require_headless_shell,
)

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# Every step of the script, in order. A hub step with ``browser`` set exists only in the hub half
# (the MCP half has no browser); every other step is the same operation on both transports.
STEPS: list[dict[str, Any]] = [
    {"id": "K1.1", "actor": "agent", "operation": "context_get", "compare": True},
    {"id": "K1.2", "actor": "agent", "operation": "record_get", "compare": True},
    {"id": "K1.3", "actor": "agent", "operation": "knowledge_links", "compare": True},
    {"id": "K1.4", "actor": "agent", "operation": "record_get", "compare": True},
    {"id": "K1.5", "actor": "owner", "operation": "browser: Knowledge page", "browser": True},
    {"id": "K2.1", "actor": "agent", "operation": "apply (create correction)"},
    {"id": "K2.2", "actor": "agent", "operation": "apply (correction_apply)", "authority": True},
    {"id": "K2.3", "actor": "owner", "operation": "grant the exact plan", "owner": True},
    {"id": "K2.4", "actor": "agent", "operation": "approval_resume"},
    {"id": "K2.5", "actor": "agent", "operation": "approval_resume (replay)"},
    {"id": "K2.6", "actor": "agent", "operation": "record_get", "compare": True},
    {"id": "K2.7", "actor": "agent", "operation": "now_get", "compare": True},
    {"id": "K2.8", "actor": "owner", "operation": "browser: flags", "browser": True},
    {"id": "K3.1", "actor": "agent", "operation": "source_receipt_record"},
    {"id": "K3.2", "actor": "agent", "operation": "execution_report"},
    {"id": "K3.3", "actor": "agent", "operation": "awaiting_interpretation", "compare": True},
    {"id": "K3.4", "actor": "owner", "operation": "browser: Now awaiting", "browser": True},
    {"id": "K3.5", "actor": "agent", "operation": "awaiting_resolve", "authority": True},
    {"id": "K3.6", "actor": "owner", "operation": "resolve with a reason", "owner": True},
    {"id": "K3.7", "actor": "agent", "operation": "awaiting_interpretation", "compare": True},
    {"id": "K4.1", "actor": "agent", "operation": "source_import_plan"},
    {"id": "K4.2", "actor": "agent", "operation": "run_get (queued at close)"},
    {"id": "K4.3", "actor": "agent", "operation": "chat closed; new chat opened"},
    {"id": "K4.4", "actor": "agent", "operation": "now_get", "compare": True},
    {"id": "K4.5", "actor": "agent", "operation": "run_list and run_resume", "compare": True},
    {
        "id": "K4.6",
        "actor": "owner",
        "operation": "browser: Now and Runs, new context",
        "browser": True,
    },
]
SCRIPT_IDS = [s["id"] for s in STEPS if not s.get("browser")]


SHA256 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
AGENT_NAME = re.compile(r"(?<![\w-])(?:http-agent|mcp)(?![\w-])")
SEQUENCE = re.compile(r"project_seq=\d+")


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def stable(value: Any) -> Any:
    if isinstance(value, str) and "\n" in value:
        # Only list items within one block are unordered. Headings and prose keep their
        # positions: moving findings under assumptions must remain a different answer.
        lines = value.split("\n")
        start = 0
        while start < len(lines):
            if not lines[start].startswith("- "):
                start += 1
                continue
            end = start + 1
            while end < len(lines) and lines[end].startswith("- "):
                end += 1
            lines[start:end] = sorted(lines[start:end])
            start = end
        return "\n".join(lines)
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((stable(v) for v in value), key=canon)
    return value


def canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canon(value).encode("utf-8")).hexdigest()


# ----- the fresh store (the given) --------------------------------------------------------

PLANT_SCOPE = {
    "system": "PLANT-B",
    "operating_regime": "variable-demand synthetic event",
    "comparator": "reference controller under the same constraints",
    "model_fidelity": "dynamic plant with actuator constraints",
    "validation_level": "no measured validation",
}


class Seeded:
    """A scenario on a fresh store from the synthetic fixtures, plus what the walk adds."""

    def __init__(self, work: Path) -> None:
        from cp1_support import OWNER
        from cp7_support import create
        from cp10_continuity_support import Scenario

        from prometheos.domain.ids import new_id, new_request_id

        work.mkdir(parents=True, exist_ok=True)
        s = Scenario(work).build(supported_scope=PLANT_SCOPE)
        self.s = s
        meeting = s.svc.capture_meeting(
            s.pid,
            OWNER,
            title="Supervision meeting",
            text=(ROOT / "fixtures" / "meeting.txt").read_text(encoding="utf-8"),
            held_at="2026-09-28T10:00:00Z",
            request_id=new_request_id(),
        )
        s.name(meeting["meeting_id"], "meeting")
        # Finding A is the best currently supported result: a researcher-reviewed support.
        assessment = s.name(new_id("assessment"), "assessment_a")
        s.env.apply(
            [
                create(
                    "assessment",
                    {
                        "claim_ref": {"id": s.claim_a, "rev": 1},
                        "outcome": "supports",
                        "rationale": "The plant result matches within the stated regime only.",
                        "checks": [],
                        "author_ref": None,
                        "method": "reported_agent",
                        "scope_match": True,
                    },
                    assessment,
                )
            ]
        )
        s.paper.protected(
            [s.paper.transition_op(s.claim_a, "supported_within_conditions", "reviewed")]
        )
        self.created: dict[str, str] = {}

    @property
    def pid(self) -> str:
        return self.s.pid

    @property
    def home(self) -> Path:
        return self.s.env.home

    def name(self, record_id: str, label: str) -> str:
        self.created[label] = record_id
        return self.s.name(record_id, label)

    def norm(self, value: Any) -> Any:
        return self.s.normalise(value)

    def view(self, value: Any) -> Any:
        """What two transports' answers to the same read must share: IDs, times, digests, the
        transport's own name and project sequence numbers normalised, lists in a stable order
        (the order of unrelated items follows their random IDs)."""
        text = json.dumps(self.norm(value), sort_keys=True)
        text = SHA256.sub("<sha256>", text)
        text = UUID.sub("<uuid>", text)
        text = AGENT_NAME.sub("<agent>", text)
        text = SEQUENCE.sub("project_seq=<n>", text)
        return stable(json.loads(text))


# ----- the normalised outcome of a store ---------------------------------------------------


def snapshot(seed: Seeded) -> dict[str, Any]:
    """Records, states, relations, flags, queues, approvals and runs of the store, with IDs
    and timestamps normalised. Read as the owner through the service and the store's tables."""
    from cp1_support import AGENT, OWNER

    s, svc, pid, norm = seed.s, seed.s.svc, seed.pid, seed.norm

    def records(kind: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = svc.list_records(
            pid, OWNER, kind=kind, limit=200, include_archived=True
        )["items"]
        return items

    named = {k: v for k, v in s.names.items()}

    def label(record_id: str) -> str:
        return named.get(record_id, "<other>")

    out: dict[str, Any] = {}
    out["counts"] = {
        kind: len(records(kind))
        for kind in ("claim", "assessment", "correction", "entry", "anchor", "action", "execution")
    }
    out["claims"] = {
        label(r["id"]): {
            "state": r["body"]["state"],
            "scope": r["body"]["scope"],
            "superseded_by": norm(r["body"]["superseded_by"]),
            "assessments": len(r["body"]["assessment_refs"]),
        }
        for r in records("claim")
        if r["id"] in named
    }
    out["assumptions"] = {
        label(r["id"]): {
            "title": r["body"]["title"],
            "superseded_by": norm(r["body"].get("superseded_by")),
        }
        for r in records("entry")
        if r["body"]["category"] == "assumption"
    }
    out["entries"] = sorted(
        (
            norm(
                {
                    k: r["body"][k]
                    for k in ("category", "title", "review", "origin", "text")
                    if r["body"]["category"] != "assumption"
                }
            )
            for r in records("entry")
            if r["body"]["category"] != "assumption"
        ),
        key=canon,
    )
    out["corrections"] = [
        {"status": r["body"]["status"], "applied": r["body"]["applied_seq"] is not None}
        for r in records("correction")
    ]
    out["anchors"] = {
        label(r["id"]): norm({"status": r["body"]["status"], "reason": r["body"]["reason"]})
        for r in records("anchor")
    }
    out["actions"] = sorted(
        (
            norm({"title": r["body"]["title"], "status": r["body"]["status"]})
            for r in records("action")
        ),
        key=canon,
    )
    out["executions"] = {
        label(r["id"]): {
            "state": r["body"]["state"],
            "receipts": len(r["body"]["receipt_refs"]),
        }
        for r in records("execution")
    }
    applied_seq = next(
        (r["body"]["applied_seq"] for r in records("correction") if r["body"]["applied_seq"]), None
    )
    with closing(sqlite3.connect(s.env.store_dir / "record.sqlite")) as conn:
        conn.row_factory = sqlite3.Row
        out["relations"] = sorted(
            (label(r["source_id"]), r["relation"], label(r["target_id"]))
            for r in conn.execute(
                "SELECT source_id, target_id, relation FROM relations WHERE retired_seq IS NULL"
            )
        )
        out["flags"] = sorted(
            (
                label(r["record_id"]),
                label(r["cause_id"]),
                norm(r["reason"]),
                r["created_seq"] == applied_seq,
            )
            for r in conn.execute(
                "SELECT record_id, reason, cause_id, created_seq FROM view_flags "
                "WHERE cleared_seq IS NULL"
            )
        )
        out["approvals"] = sorted(
            (r["operation"], r["state"], r["caller_rank"])
            for r in conn.execute("SELECT operation, state, caller_rank FROM approval_requests")
        )
        out["runs"] = sorted(
            (r["operation"], r["state"], r["caller_rank"])
            for r in conn.execute("SELECT operation, state, caller_rank FROM operation_runs")
        )
    queue = svc.awaiting_list(pid, OWNER)
    out["awaiting"] = sorted(label(i["target_id"]) for i in queue["items"])
    now = svc.now(pid, AGENT, limit=200)
    out["now"] = {
        section: sorted((label(i["id"]), norm(i.get("reason", ""))) for i in now[section]["items"])
        for section in ("changed", "due", "running", "awaiting_interpretation", "approvals")
    }
    return norm(out)  # type: ignore[no-any-return]


def first_difference(a: Any, b: Any, path: str = "") -> str | None:
    if type(a) is not type(b):
        return f"{path}: {str(a)[:100]!r} != {str(b)[:100]!r}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}/{key}: only in {'first' if key in a else 'second'}"
            found = first_difference(a[key], b[key], f"{path}/{key}")
            if found:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: lengths {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            found = first_difference(x, y, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if a == b else f"{path}: {str(a)[:100]!r} != {str(b)[:100]!r}"


# ----- the transcript ----------------------------------------------------------------------


class Recorder:
    def __init__(self, store: str, transport: str, seed: Seeded, evidence: Path) -> None:
        self.store, self.transport, self.seed = store, transport, seed
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
        normalised = self.seed.view(answer)
        (self.evidence / f"{step_id}.json").write_text(
            json.dumps(response, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
        (self.evidence / f"{step_id}.normalised.json").write_text(
            json.dumps(normalised, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
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
                "request": self.seed.norm(request),
                "outcome": outcome,
                "response_sha256": digest(response),
                "normalised_response_sha256": digest(normalised),
                "compare": bool(meta.get("compare")),
                "store_assertions": assertions,
            }
        )


def check(claim: str, passed: bool, observed: Any = None) -> dict[str, Any]:
    return {"claim": claim, "passed": bool(passed), "observed": observed}


# ----- the MCP half --------------------------------------------------------------------------


def process_exited(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return True
        code = ctypes.c_ulong()
        try:
            if not kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                raise RuntimeError("could not observe the first MCP process exit")
            return code.value != 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class McpChat:
    """One chat: a real ``prometheos mcp serve --stdio`` process behind the official client."""

    def __init__(self, seed: Seeded, *, hold: bool = False) -> None:
        from mcp import Client, StdioServerParameters

        self.process_info = seed.home.parent / (
            "process-first.json" if hold else "process-second.json"
        )
        self.params = StdioServerParameters(
            command=sys.executable,
            args=[
                str(ROOT / "tools" / "walk_mcp.py"),
                "mcp",
                "serve",
                "--stdio",
                "--project",
                seed.pid,
            ],
            env={
                **os.environ,
                "PROMETHEOS_HOME": str(seed.home),
                "PYTHONIOENCODING": "utf-8",
                "WALK_PROCESS_INFO": str(self.process_info),
                "WALK_HOLD_DISPATCH": "1" if hold else "0",
            },
        )
        self.client = Client(self.params)

    async def __aenter__(self) -> McpChat:
        await self.client.__aenter__()
        self.pid = json.loads(self.process_info.read_text(encoding="utf-8"))["pid"]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.__aexit__(*exc)

    async def call(self, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
        result = await self.client.call_tool(tool, args)
        if result.is_error:
            text = result.content[0].text  # type: ignore[union-attr]
            return False, json.loads(text[text.index("{") :])["error"]
        return True, result.structured_content


async def mcp_phase(seed: Seeded, rec: Recorder) -> None:
    from cp1_support import AGENT, OWNER, OWNER_ACT, request
    from cp7_support import create
    from cp10_continuity_support import receipt_payload

    from prometheos.domain.ids import new_id, new_request_id

    s, svc, pid = seed.s, seed.s.svc, seed.pid

    def raw_request(ops: list[dict[str, Any]]) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(request(pid, ops, s.env.seq()))
        return loaded

    async with McpChat(seed, hold=True) as chat:
        # K1: the best supported result, its comparator, scope, source revision and limits.
        ok, context = await chat.call("context_get", {"purpose": "orient", "budget": 12000})
        text = context["text"] if ok else ""
        statement = s.body(s.claim_a)["statement"]
        rec.add(
            "K1.1",
            "mcp",
            {"purpose": "orient"},
            context,
            "ok" if ok else "error",
            [
                check(
                    "the orientation context states the supported finding",
                    any(
                        "CLAIM [supported_within_conditions]" in line and statement in line
                        for line in text.splitlines()
                    ),
                ),
                check("... with its comparator", s.body(s.claim_a)["scope"]["comparator"] in text),
                check("... and its limits", s.body(s.claim_a)["scope"]["validation_level"] in text),
            ],
        )
        claim = None
        for section in context["manifest"]["sections"]:
            if section["name"] != "findings":
                continue
            for candidate in section["included"]:
                candidate_ok, found = await chat.call("record_get", {"record_id": candidate})
                if candidate_ok and found["body"]["state"] == "supported_within_conditions":
                    claim = found
                    break
        assert claim is not None, "the context must discover a supported finding"
        discovered_claim = claim["id"]
        ok = True
        rec.add(
            "K1.2",
            "mcp",
            {"record_id": "claim_a"},
            claim,
            "ok" if ok else "error",
            [
                check(
                    "the finding is supported within its conditions",
                    claim["body"]["state"] == "supported_within_conditions",
                ),
                check(
                    "its scope matches the cited PLANT-B fixture's regime, fidelity and comparator",
                    all(claim["body"]["scope"][k] == value for k, value in PLANT_SCOPE.items()),
                ),
            ],
        )
        ok, links = await chat.call("knowledge_links", {"record_id": discovered_claim})
        rec.add(
            "K1.3",
            "mcp",
            {"record_id": "claim_a"},
            links,
            "ok" if ok else "error",
            [
                check(
                    "it relies on the shared assumption",
                    any(r["id"] == s.drag for r in links["relies_on"]),
                    links["relies_on"],
                )
            ],
        )
        evidence_version = claim["body"]["evidence"][0]["version_id"]
        ok, version = await chat.call("record_get", {"record_id": evidence_version})
        stored = s.svc.get(pid, OWNER, evidence_version, None)["body"]
        rec.add(
            "K1.4",
            "mcp",
            {"record_id": "evidence version"},
            version,
            "ok" if ok else "error",
            [
                check(
                    "the cited source revision is pinned by digest and size",
                    version["body"]["sha256"] == stored["sha256"]
                    and version["body"]["byte_length"] == stored["byte_length"],
                ),
                check("its bytes are retained", version["body"]["availability"] == "retained"),
            ],
        )

        # K2: an authorised correction flags what depended on the assumption.
        correction = seed.name(new_id("correction"), "correction")
        body = {
            "target_refs": [{"id": s.drag, "rev": 1}],
            "replacement_refs": [{"id": s.drag_v2, "rev": 1}],
            "reason": "The constant-drag assumption fails at high speed.",
            "evidence": [],
            "effects": [
                {
                    "target_ref": {"id": s.drag, "rev": 1},
                    "effect": "supersede",
                    "replacement_ref": {"id": s.drag_v2, "rev": 1},
                    "replacement_text": None,
                }
            ],
            "status": "proposed",
            "applied_seq": None,
        }
        proposal = raw_request([create("correction", body, correction)])
        ok, created = await chat.call("apply", {"request": proposal})
        rec.add(
            "K2.1",
            "mcp",
            proposal,
            created,
            "ok" if ok else "error",
            [
                check(
                    "the proposal is stored as a proposed correction",
                    s.body(correction)["status"] == "proposed",
                )
            ],
        )
        apply_ops = [{"op": "correction_apply", "id": correction, "base_rev": 1}]
        applying = raw_request(apply_ops)
        seq_before = s.env.seq()
        ok, error = await chat.call("apply", {"request": applying})
        approval_id = error["details"].get("approval_request_id", "") if not ok else ""
        seed.name(approval_id, "approval_correction")
        rec.add(
            "K2.2",
            "mcp",
            applying,
            error,
            "error:" + (error["code"] if not ok else "none"),
            [
                check(
                    "the agent is told it needs the owner",
                    not ok and error["code"] == "approval_required",
                ),
                check(
                    "nothing committed and nothing is flagged",
                    s.env.seq() == seq_before and s.flags() == [],
                ),
            ],
        )
        # Synthetic-test OwnerAct, not owner-route evidence (E09 stays open).
        shown = svc.approval_owner_view(pid, approval_id)
        granted = svc.approval_decide(
            pid, approval_id, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
        )
        rec.add(
            "K2.3",
            "owner-service",
            {"approval": "approval_correction"},
            granted,
            "ok",
            [
                check(
                    "the grant is held by the server for exactly this plan",
                    granted["state"] == "granted"
                    and shown["plan_sha256"] == error["details"]["plan_sha256"],
                )
            ],
        )
        # Nothing may commit between the grant and the resume (decision 0020).
        seq_at_grant = s.env.seq()
        ok, resumed = await chat.call("approval_resume", {"approval_id": approval_id})
        commit_seq = resumed["result"]["project_seq"] if ok else None
        rec.add(
            "K2.4",
            "mcp",
            {"approval_id": "approval_correction"},
            resumed,
            "ok" if ok else "error:" + resumed["code"],
            [
                check("the stored plan ran once", ok and resumed["state"] == "used"),
                check(
                    "the project did not move between the grant and the resume",
                    commit_seq == seq_at_grant + 1,
                    [seq_at_grant, commit_seq],
                ),
                check(
                    "the assumption is superseded by its replacement",
                    s.body(s.drag)["superseded_by"] == {"id": s.drag_v2, "rev": 1},
                ),
                check(
                    "the dependents were flagged in the same commit",
                    {f["record_id"] for f in s.flags()}
                    == {s.claim_a, s.action, s.anchor, s.anchor_claim}
                    and all(f["created_seq"] == commit_seq for f in s.flags()),
                ),
            ],
        )
        ok, again = await chat.call("approval_resume", {"approval_id": approval_id})
        rec.add(
            "K2.5",
            "mcp",
            {"approval_id": "approval_correction"},
            again,
            "ok" if ok else "error",
            [
                check(
                    "a second resume replays the stored result and changes nothing",
                    ok and again.get("replayed") is True and s.env.seq() == commit_seq,
                )
            ],
        )
        ok, anchor = await chat.call("record_get", {"record_id": s.anchor})
        rec.add(
            "K2.6",
            "mcp",
            {"record_id": "anchor_uses_assumption"},
            anchor,
            "ok" if ok else "error",
            [
                check(
                    "the manuscript anchor that used the assumption needs review",
                    anchor["body"]["status"] == "needs_review",
                ),
                check(
                    "the control anchor is unchanged",
                    s.body(s.anchor_control)["status"] == "current",
                ),
            ],
        )
        ok, now = await chat.call("now_get", {"limit": 200})
        changed = {i["id"] for i in now["changed"]["items"]}
        rec.add(
            "K2.7",
            "mcp",
            {"limit": 200},
            now,
            "ok" if ok else "error",
            [
                check(
                    "Now lists the flagged dependents as changed",
                    {s.claim_a, s.anchor, s.anchor_claim} <= changed,
                ),
                check(
                    "... and not the replacement or the correction",
                    s.drag_v2 not in changed and correction not in changed,
                ),
            ],
        )

        # K3: a finished run whose output nobody has interpreted.
        outputs = [
            {
                "source_id": s.plant["source_id"],
                "version_id": s.plant["version_id"],
                "sha256": s.plant["sha256"],
                "role": "result",
            }
        ]
        receipt_request = {
            "receipt": receipt_payload(outputs),
            "title": "Walk receipt",
            "request_id": new_request_id(),
        }
        ok, receipt = await chat.call("source_receipt_record", receipt_request)
        version_id = next(
            (r["id"] for r in receipt.get("results", []) if r["id"].startswith("version_")), ""
        )
        seed.name(version_id, "walk_receipt")
        rec.add(
            "K3.1",
            "mcp",
            {"title": "Walk receipt"},
            receipt,
            "ok" if ok else "error",
            [
                check(
                    "the receipt is stored as reported, not observed",
                    s.body(version_id)["payload_schema"] == "execution_receipt_v1",
                )
            ],
        )
        report = {
            "execution_id": s.run_done,
            "state": "reported_completed",
            "receipt_version_ids": [version_id],
            "request_id": new_request_id(),
        }
        ok, reported = await chat.call("execution_report", report)
        rec.add(
            "K3.2",
            "mcp",
            {"execution": "run_done", "state": "reported_completed"},
            reported,
            "ok" if ok else "error",
            [
                check(
                    "the run is recorded completed, nothing was started or stopped",
                    s.body(s.run_done)["state"] == "reported_completed",
                )
            ],
        )
        ok, queue = await chat.call("awaiting_interpretation", {})
        waiting = {i["target_id"] for i in queue["items"]}
        rec.add(
            "K3.3",
            "mcp",
            {},
            queue,
            "ok" if ok else "error",
            [
                check("the finished run awaits interpretation", s.run_done in waiting),
                check(
                    "so does the receipt nobody registered a run for",
                    s.bare_receipt["version_id"] in waiting,
                ),
                check("a run still reported running does not", s.run_live not in waiting),
            ],
        )
        ok, refused = await chat.call(
            "awaiting_resolve",
            {
                "target_id": s.run_done,
                "resolution": "dismissed",
                "text": "The sweep used the old drag model; nothing from it is used.",
                "request_id": new_request_id(),
            },
        )
        pending = refused["details"].get("approval_request_id", "") if not ok else ""
        seed.name(pending, "approval_dismissal")
        rec.add(
            "K3.5",
            "mcp",
            {"target": "run_done", "resolution": "dismissed"},
            refused,
            "error:" + (refused["code"] if not ok else "none"),
            [
                check(
                    "an agent cannot dismiss a result itself: approval_required",
                    not ok and refused["code"] == "approval_required",
                ),
                check(
                    "the run is still in the queue",
                    s.run_done in {i["target_id"] for i in svc.awaiting_list(pid, OWNER)["items"]},
                ),
            ],
        )
        reason = "Superseded by the walk run; nothing in it is used."
        raw = svc.awaiting_resolve_request(
            pid, OWNER, s.bare_receipt["version_id"], "dismissed", reason, new_request_id()
        )
        grant = svc.grant(
            pid,
            operation="exact_request",
            digest=svc.review_request(pid, raw, OWNER)["digest"],
            label="SYNTHETIC-TEST walk owner",
            synthetic=True,
        )
        resolved = svc.apply(pid, raw, OWNER, grant.secret)
        entry = s.body(resolved["results"][0]["id"])
        rec.add(
            "K3.6",
            "owner-service",
            {"target": "bare_receipt", "resolution": "dismissed"},
            resolved,
            "ok",
            [
                check(
                    "the dismissal is a reviewed decision entry that carries the reason",
                    entry["review"] == "reviewed"
                    and entry["category"] == "decision"
                    and entry["text"] == reason,
                )
            ],
        )
        ok, queue = await chat.call("awaiting_interpretation", {})
        waiting = {i["target_id"] for i in queue["items"]}
        rec.add(
            "K3.7",
            "mcp",
            {},
            queue,
            "ok" if ok else "error",
            [
                check(
                    "the dismissed receipt left the queue",
                    s.bare_receipt["version_id"] not in waiting,
                ),
                check("the finished run still waits for an interpretation", s.run_done in waiting),
            ],
        )

        # K4 (first chat): a long operation returns a run ID at once.
        plan_args = {"root_alias": "research", "paths": ["artifact.bin"], "wait_seconds": 0}
        ok, started = await chat.call("source_import_plan", plan_args)
        run_id = started.get("run_id", "") if ok else ""
        seed.name(run_id, "import_run")
        rec.add(
            "K4.1",
            "mcp",
            plan_args,
            started,
            "ok" if ok else "error",
            [check("the answer is a run ID (oprun_), not the result", run_id.startswith("oprun_"))],
        )
        ok, view = await chat.call("run_get", {"run_id": run_id})
        first_pid = chat.pid
        rec.add(
            "K4.2",
            "mcp",
            {"run_id": "import_run"},
            view,
            "ok" if ok else "error",
            [
                check(
                    "the real run is durably queued at chat close (synthetic dispatcher hold)",
                    view.get("state") == "queued",
                    view.get("state"),
                )
            ],
        )
    # Query the closed process without sending a signal (Windows signal 0 is destructive).
    first_closed = process_exited(first_pid)
    assert first_closed, "the first MCP process must have exited"
    async with McpChat(seed) as chat:
        rec.add(
            "K4.3",
            "mcp",
            {"closed": "chat 1"},
            {"chat": 2},
            "ok",
            [
                check(
                    "the second chat is a new server process and the first exited",
                    first_closed and chat.pid != first_pid,
                    [first_pid, chat.pid],
                )
            ],
        )
        ok, now = await chat.call("now_get", {"limit": 200})
        sections = {
            k: [i["id"] for i in now[k]["items"]]
            for k in ("changed", "running", "awaiting_interpretation", "approvals")
        }
        rec.add(
            "K4.4",
            "mcp",
            {"limit": 200},
            now,
            "ok" if ok else "error",
            [
                check(
                    "it shows what changed since the correction",
                    {s.claim_a, s.anchor} <= set(sections["changed"]),
                ),
                check("... what is still running", s.run_live in sections["running"]),
                check(
                    "... what awaits interpretation",
                    s.run_done in sections["awaiting_interpretation"],
                ),
                check(
                    "... and the decision still waiting for the owner",
                    seed.created["approval_dismissal"] in sections["approvals"],
                ),
            ],
        )
        ok, listed = await chat.call("run_list", {"operation": "source_import_plan", "limit": 20})
        candidates = [
            r for r in listed["runs"] if r["state"] in ("queued", "failed", "paused_budget")
        ]
        assert len(candidates) == 1, "fresh chat discovers the unfinished import without an ID"
        discovered_run = candidates[0]["run_id"]
        ok, view = await chat.call("run_resume", {"run_id": discovered_run, "wait_seconds": 20})
        rec.add(
            "K4.5",
            "mcp",
            {"run_id": "import_run"},
            view,
            "ok" if ok else "error",
            [
                check(
                    "the fresh chat discovered and resumed the queued run to a stored result",
                    ok and view["state"] == "completed" and bool(view.get("result")),
                )
            ],
        )
        _ = (AGENT,)


# ----- the hub half (Playwright against the real hub) ---------------------------------------


def receipt_template(seed: Seeded) -> dict[str, Any]:
    """The execution receipt the agent records in K3 (the same on both halves)."""
    from cp10_continuity_support import receipt_payload

    plant = seed.s.plant
    outputs = [
        {
            "source_id": plant["source_id"],
            "version_id": plant["version_id"],
            "sha256": plant["sha256"],
            "role": "result",
        }
    ]
    return receipt_payload(outputs)


def boundary_evidence(out: Path) -> tuple[dict[str, Any], list[str]]:
    """The browser boundary controls' evidence for this walk (``browser-boundary.json``), and
    that every walk test ran in a worker whose browser the journey guard checked (Playwright's
    JSON report against the evidence's journey records)."""
    summary, problems = browser_shell.boundary_evidence(out / "browser-boundary.json")
    if not problems:
        problems = browser_shell.journey_problems(
            out / "playwright-report.json", summary["journey_browsers"]
        )
    return summary, [f"hub: {problem}" for problem in problems]


def hub_phase(seed: Seeded, out: Path, rec: Recorder) -> None:
    try:
        require_headless_shell(ROOT / "hub")
        playwright = playwright_command(ROOT / "hub")  # the local runner, never npx
    except BrowserUnavailable as exc:
        raise RuntimeError(f"the browser half cannot run: {exc}") from exc
    info_path = out / "hub-info.json"
    steps_path = out / "hub-steps.json"
    names_path = out / "hub-names.json"
    log = open(out / "hub.log", "w", encoding="utf-8")
    server = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "walk_hub.py"),
            "--home",
            str(seed.home),
            "--project",
            seed.pid,
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
    try:
        deadline = time.monotonic() + 60
        while not info_path.exists():
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the walk hub did not start; see hub.log")
            time.sleep(0.2)
        time.sleep(0.5)
        names_path.write_text(
            json.dumps({"names": seed.s.names, "created": seed.created}, indent=2), encoding="utf-8"
        )
        env = {
            **playwright_environment(),
            "WALK_INFO": str(info_path),
            "WALK_NAMES": str(names_path),
            "WALK_STEPS": str(steps_path),
            "WALK_SHOTS": str(out / "screenshots"),
            "WALK_PW_OUT": str(Path(tempfile.mkdtemp(prefix="prometheos-walk-pw-"))),
            "BOUNDARY_EVIDENCE": str(out / "browser-boundary.json"),
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(out / "playwright-report.json"),
            "WALK_FIXTURE": json.dumps(
                {
                    "receipt": receipt_template(seed),
                    "claim_a": seed.s.claim_a,
                    "claim_b": seed.s.claim_b,
                    "drag": seed.s.drag,
                    "drag_v2": seed.s.drag_v2,
                    "anchor": seed.s.anchor,
                    "anchor_claim": seed.s.anchor_claim,
                    "anchor_control": seed.s.anchor_control,
                    "action": seed.s.action,
                    "run_live": seed.s.run_live,
                    "run_done": seed.s.run_done,
                    "bare_receipt": seed.s.bare_receipt["version_id"],
                    "plant_source": seed.s.plant["source_id"],
                    "plant_version": seed.s.plant["version_id"],
                    "plant_sha256": seed.s.plant["sha256"],
                    "reference_version": seed.s.reference["version_id"],
                    "project_id": seed.pid,
                }
            ),
        }
        done = subprocess.run(
            [*playwright, "-c", "playwright.walk.config.ts", "--reporter=line,json"],
            cwd=ROOT / "hub",
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        (out / "playwright.log").write_text(done.stdout + "\n" + done.stderr, encoding="utf-8")
        if done.returncode != 0:
            rec.failures.append(f"hub: the Playwright walk failed (exit {done.returncode})")
            print(done.stdout[-3000:], done.stderr[-2000:])
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
    if steps_path.exists():
        for step in json.loads(steps_path.read_text(encoding="utf-8")):
            for name, record_id in step.get("created", {}).items():
                seed.name(record_id, name)
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
    lines = ["# C10-WALK transcript", "", doc["summary"], ""]
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
    return "\n".join(lines) + "\n"


def judge(doc: dict[str, Any], outcomes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Compare the two halves: stored outcomes, the scripted steps, each step's outcome (so the
    agent's authority results) and the normalised answers to the pure reads."""
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
    compared = [i for i in both if next(s for s in STEPS if s["id"] == i)["actor"] == "agent"]
    unequal = [
        i
        for i in compared
        if hub_steps[i]["normalised_response_sha256"] != mcp_steps[i]["normalised_response_sha256"]
    ]
    equivalence["normalised response digests of every agent step"] = (
        f"equal for {len(compared)} steps" if not unequal else f"differ at {unequal}"
    )
    if unequal:
        failures.append(
            f"normalised read responses differ at {unequal} (all agent replies compared)"
        )
    return equivalence, failures


def hub_stopped_note(out: Path) -> str:
    """Nothing answers on the hub's port while the MCP half runs."""
    import socket

    try:
        port = int(json.loads((out / "hub-info.json").read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError):
        return "not checked"
    with socket.socket() as probe:
        probe.settimeout(1.0)
        refused = probe.connect_ex(("127.0.0.1", port)) != 0
    return "confirmed (the hub's port refuses connections)" if refused else "NOT STOPPED"


def run(out: Path | None, only: str | None, keep: bool, show: bool) -> int:
    from cp1_support import AGENT  # noqa: F401  (imported to fail early when tests/ is absent)

    out = (out or Path(tempfile.mkdtemp(prefix="prometheos-walk-out-"))).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into a folder that is not empty: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prometheos-walk-"))
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc: dict[str, Any] = {
        "limitations": [
            "K2.3 uses a synthetic-test OwnerAct and K3.6 a SYNTHETIC-TEST capability; "
            "these are not owner-route evidence. E09 and RPA-HEADLESS remain open.",
            "K4 uses a harness-only dispatcher hold to keep a real application run queued; "
            "fresh real transports discover and resume it.",
        ],
        "walk": "C10-WALK",
        "started_at": started,
        "script": SCRIPT_IDS,
    }
    failures: list[str] = []
    outcomes: dict[str, Any] = {}
    equivalence: dict[str, Any] = {}
    try:
        if only in (None, "hub"):
            seed_a = Seeded(work / "store-a")
            rec_a = Recorder("A", "hub", seed_a, out / "evidence" / "hub")
            hub_phase(seed_a, out, rec_a)
            outcomes["hub"] = snapshot(seed_a)
            (out / "outcome-hub.json").write_text(
                json.dumps(outcomes["hub"], indent=2, sort_keys=True), encoding="utf-8"
            )
            doc["hub"] = {
                "store": "A",
                "transport": "hub (browser + agent HTTP)",
                "steps": rec_a.steps,
            }
            failures += rec_a.failures
            doc["browser_boundary"], problems = boundary_evidence(out)
            failures += problems
        if only in (None, "mcp"):
            note = hub_stopped_note(out)
            equivalence["the hub was stopped during the MCP half"] = note
            if note == "NOT STOPPED":
                failures.append("the hub still answered during the MCP half")
            seed_b = Seeded(work / "store-b")
            rec_b = Recorder("B", "mcp", seed_b, out / "evidence" / "mcp")
            asyncio.run(mcp_phase(seed_b, rec_b))
            outcomes["mcp"] = snapshot(seed_b)
            (out / "outcome-mcp.json").write_text(
                json.dumps(outcomes["mcp"], indent=2, sort_keys=True), encoding="utf-8"
            )
            doc["mcp"] = {
                "store": "B",
                "transport": "MCP stdio (official SDK client), hub stopped",
                "steps": rec_b.steps,
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
        if show:  # the checkpoint runner keeps stdout as evidence
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
