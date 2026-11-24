"""C18-WALK: the CP18 end-to-end walk (spec/10 CP18.4; checkpoints.json C18-WALK).

    uv run python tools/walk_cp18.py [--out DIR] [--print-transcript] [--only hub|mcp] [--keep]

On the built app with a fresh store, the researcher's work items and the agent's session:

1. **Hub half (store A).** The real hub (the assets shipped in the package, started as
   ``prometheos hub serve`` starts: run recovery, then one detector pass) serves store A. A
   Playwright spec (``hub/walk18/walk.spec.ts``, Playwright's Chromium headless shell inside the
   loopback-only boundary of decision 0028, proved first by ``hub/walk18/00-boundary.spec.ts``)
   drives the owner: four work items added on the Work page (three with deadlines, one
   without), then "Run due checks now" in Now's inbox, where the deadline events appear. While
   the hub keeps running, an agent session over the real ``prometheos mcp serve --stdio``
   process (the official MCP SDK client) reads the queue, summarises it and asks one question;
   the owner accepts one item; the agent starts it, records its output and completes it with
   that output as evidence. The owner then sees the item done in the hub and acknowledges its
   deadline event. A second, fresh agent session reads the queue again.
2. **MCP half (store B).** On a second fresh store, with the hub stopped, the same script runs
   through MCP alone: the owner's steps go through the service in the harness (a synthetic
   owner channel, as in C10-WALK), the deadline events come from the MCP server's own start
   pass, and every agent step is the same MCP call.
3. The two stores' normalised outcomes (work items, events, detector state, approvals, runs,
   recorded outputs; IDs and times normalised), every step's outcome and the normalised answer
   of every agent step must be equal.

Output in ``--out`` (a new folder): ``transcript.json`` and ``transcript.md`` (each step, the
operation, response digests, store assertions, and the agent's message and the owner's answer
in full), ``outcome-hub.json`` and ``outcome-mcp.json``, ``evidence/`` (the raw responses),
``browser-boundary.json``, ``playwright-report.json`` and the browser's screenshots. Exit 0
only when every assertion passed and the outcomes are equal.

Deadlines are fixed once per walk (now + N days + 12 hours, to the minute), so both stores hold
the same instants and the queue's "due in N days" reasons agree for twelve hours either side.
Synthetic data only; nothing is sent anywhere.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

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

ZONE = "Asia/Riyadh"
# label, title, why it matters, days until the deadline (None: no deadline), description, labels
ITEMS: list[tuple[str, str, str, int | None, str | None, list[str]]] = [
    ("p1", "Submit paper P1 to journal X", "Agreed with my supervisor", 10,
     "Final checks on figures 3-5, then the journal portal.\n"
     "Cover letter: state the novelty in two sentences.", ["paper P1", "submission"]),
    ("referee", "Referee report for journal Y", "Second-round review", 5,
     "Check the authors' revised Fig. 4 against my first-round comments.", ["review"]),
    ("reply", "Reply to the co-author about Fig. 4", "They are waiting for my answer", 1, None, []),
    ("tidy", "Tidy the figure scripts", "Before the data release", None, None, ["code"]),
]  # fmt: skip
# The queue's order: by deadline, items without one last.
BY_DEADLINE = [i[1] for i in sorted(ITEMS, key=lambda i: (i[3] is None, i[3] or 0))]
# The tightest threshold each deadline has reached (decision 0035 item 5).
THRESHOLD = {"p1": 14, "referee": 7, "reply": 2}
ACCEPTED = "p1"
OUTPUT_PATH = "submission/P1-cover-letter.md"
OUTPUT_TEXT = (
    "# Cover letter for paper P1 (synthetic draft)\n\n"
    "Dear Editor,\n\n"
    "We submit 'A constrained controller for a synthetic flywheel plant' for consideration.\n"
    "Its novelty: a comparison within stated regimes, and an explicit list of its limits.\n"
)
ANSWER = 'Yes, please help with "Submit paper P1 to journal X".'
ACK_NOTE = "Submitted; the cover letter is recorded."
HUB_ADD_REASON = "Added by the researcher in the hub"  # the hub's adoption reason (Work.tsx)

# Every step of the script, in order. ``owner`` steps run in the browser on the hub half and
# through the service (a synthetic owner channel) on the MCP half; ``agent`` steps are the same
# MCP call on both halves; ``chat`` is the conversation itself (no operation).
STEPS: list[dict[str, Any]] = [
    {"id": "W1.1", "actor": "owner", "operation": "add work item P1 (deadline in 10 days)"},
    {"id": "W1.2", "actor": "owner", "operation": "add work item (deadline in 5 days)"},
    {"id": "W1.3", "actor": "owner", "operation": "add work item (deadline in 1 day)"},
    {"id": "W1.4", "actor": "owner", "operation": "add work item (no deadline)"},
    {"id": "W2.1", "actor": "owner", "operation": "deadline events appear"},
    {"id": "W3.1", "actor": "agent", "operation": "session start; work_queue"},
    {"id": "W3.2", "actor": "agent", "operation": "summary and one question (work-items skill)"},
    {"id": "W3.3", "actor": "chat", "operation": "the researcher accepts one item"},
    {"id": "W4.1", "actor": "agent", "operation": "apply transition ready -> in_progress"},
    {"id": "W4.2", "actor": "agent", "operation": "source_import_plan and commit (its output)"},
    {"id": "W4.3", "actor": "agent", "operation": "apply transition in_progress -> done"},
    {"id": "W4.4", "actor": "agent", "operation": "work_queue"},
    {"id": "W5.1", "actor": "owner", "operation": "sees the item done"},
    {"id": "W5.2", "actor": "owner", "operation": "acknowledges its deadline event with a note"},
    {"id": "W5.3", "actor": "agent", "operation": "new session; work_queue"},
]
SCRIPT_IDS = [s["id"] for s in STEPS]
AGENT_IDS = [s["id"] for s in STEPS if s["actor"] == "agent"]

SHA256 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)")


def deadlines(now: datetime | None = None) -> dict[str, str]:
    """The walk's deadline instants (UTC, to the minute), shared by both halves."""
    base = (now or datetime.now(UTC)).replace(second=0, microsecond=0) + timedelta(hours=12)
    return {
        label: (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for label, _t, _r, days, _d, _l in ITEMS
        if days is not None
    }


def local(iso: str) -> tuple[str, str]:
    """A UTC instant as the date and time the owner types in the project's zone."""
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(ZONE))
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")


def item_body(label: str, due: dict[str, str]) -> dict[str, Any]:
    _label, title, reason, _days, description, labels = next(i for i in ITEMS if i[0] == label)
    return {
        "title": title, "reason": reason, "due_at": due.get(label), "description": description,
        "labels": labels, "source_refs": [], "owner_ref": None, "dependencies": [],
        "status": "proposed", "blocker": None, "completion_evidence": [],
        "commitment_item_id": None,
    }  # fmt: skip


# ----- a fresh store ------------------------------------------------------------------------


class Store:
    """A fresh project store with an empty research root, plus the walk's names for its IDs."""

    def __init__(self, work: Path, due: dict[str, str]) -> None:
        from prometheos.app.reads import Caller
        from prometheos.app.service import Service

        self.work, self.due = work, due
        self.research = work / "research"
        self.research.mkdir(parents=True)
        self.home = work / "home"
        self.svc = Service(home=self.home, actor="hub")
        self.owner = Caller(actor="hub", scope="owner")
        self.pid: str = self.svc.init_project(
            title="Synthetic study (C18-WALK)",
            objective="Compare the candidate controller with the reference within stated regimes.",
            timezone=ZONE,
            roots={"research": str(self.research)},
        )["project_id"]
        self.names: dict[str, str] = {}

    def name(self, record_id: str, label: str) -> str:
        if record_id:
            self.names[record_id] = label
        return record_id

    def ids(self) -> dict[str, str]:
        return {label: rid for rid, label in self.names.items()}

    def norm(self, value: Any) -> Any:
        """IDs to their walk names, deadlines to their labels, other times, UUIDs and digests to
        placeholders. Order is kept: the queue's order is part of the answer."""
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        for rid in sorted(self.names, key=len, reverse=True):
            text = text.replace(rid, f"<{self.names[rid]}>")
        for label, iso in self.due.items():
            text = text.replace(iso, f"<due:{label}>")
        text = STAMP.sub("<time>", text)
        text = SHA256.sub("<sha256>", text)
        text = UUID.sub("<uuid>", text)
        return json.loads(text)

    def db_path(self) -> Path:
        return Path(self.svc.open(self.pid).db_path)

    def digest(self) -> str:
        """The whole store's content (a read-only dump): reading must leave it unchanged."""
        uri = f"file:{self.db_path().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            return hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()

    def rows(self, sql: str) -> list[sqlite3.Row]:
        uri = f"file:{self.db_path().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(sql))

    def action(self, label: str) -> dict[str, Any]:
        record: dict[str, Any] = self.svc.get(self.pid, self.owner, self.ids()[label], None)
        return record

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        listed: dict[str, Any] = self.svc.event_list(
            self.pid, self.owner, event_type=kind, state="all", limit=200
        )
        return list(listed["items"])

    def owner_add(self, label: str) -> dict[str, Any]:
        """The hub's add, in the harness: create the proposed action and adopt it (ready) in one
        reviewed request, applied with a synthetic owner decision bound to its digest."""
        from cp1_support import request

        from prometheos.domain.ids import new_id

        action_id = self.name(new_id("action"), label)
        ops = [
            {"op": "create", "kind": "action", "id": action_id, "base_rev": 0,
             "sensitivity": "normal", "occurred_at": None, "body": item_body(label, self.due)},
            {"op": "transition", "id": action_id, "base_rev": 1, "to": "ready",
             "reason": HUB_ADD_REASON, "evidence": []},
        ]  # fmt: skip
        seq = self.svc.project_info(self.pid, self.owner)["project_seq"]
        raw = request(self.pid, ops, seq)
        preview = self.svc.review_request(self.pid, raw, self.owner)
        grant = self.svc.grant(
            self.pid,
            operation="exact_request",
            digest=preview["digest"],
            ttl_seconds=60,
            max_uses=1,
            label="SYNTHETIC-TEST owner decision (C18-WALK)",
            synthetic=True,
        )
        result: dict[str, Any] = self.svc.apply(self.pid, raw, self.owner, grant.secret)
        return result


# ----- the normalised outcome of a store ----------------------------------------------------


def snapshot(st: Store) -> dict[str, Any]:
    """Work items, events, detector state, approvals, runs and recorded outputs, normalised."""
    out: dict[str, Any] = {}
    actions = st.svc.list_records(
        st.pid, st.owner, kind="action", limit=200, include_archived=True
    )["items"]
    out["work_items"] = sorted(
        (
            {
                "label": st.names.get(r["id"], "<other>"),
                "rev": r["rev"],
                **{
                    k: r["body"][k]
                    for k in ("title", "reason", "status", "due_at", "description", "labels")
                },
                "completion_evidence": r["body"]["completion_evidence"],
            }
            for r in actions
        ),
        key=lambda a: a["label"],
    )
    out["events"] = [
        {
            "type": r["type"],
            "subject": json.loads(r["subject_ref"]),
            "data": json.loads(r["data"]),
            "project_seq": r["project_seq"],
            "acknowledged": r["acknowledged_at"] is not None,
            "note": r["acknowledged_note"],
        }
        for r in st.rows("SELECT * FROM events")
    ]
    out["detector_state"] = [
        [r["detector"], r["condition_key"], r["event_id"] is not None]
        for r in st.rows("SELECT detector, condition_key, event_id FROM detector_state")
    ]
    out["approvals"] = [
        [r["operation"], r["state"], r["caller_rank"]]
        for r in st.rows("SELECT operation, state, caller_rank FROM approval_requests")
    ]
    out["runs"] = [
        [r["operation"], r["state"], r["caller_rank"]]
        for r in st.rows("SELECT operation, state, caller_rank FROM operation_runs")
    ]
    sources = st.svc.list_records(st.pid, st.owner, kind="source", limit=200)["items"]
    versions = st.svc.list_records(st.pid, st.owner, kind="version", limit=200)["items"]
    out["outputs"] = {
        "sources": [r["body"].get("path") or r["body"].get("title", "") for r in sources],
        "versions": [
            [r["body"]["sha256"], r["body"]["byte_length"], r["body"]["availability"]]
            for r in versions
        ],
    }
    out["project_seq"] = st.svc.project_info(st.pid, st.owner)["project_seq"]
    return settle(st.norm(out))


# The snapshot's sets: rows whose stored order (and raw random IDs) mean nothing. They are sorted
# only AFTER normalisation, so two stores whose IDs differ list them in the same order (int5
# attempt 1 sorted detector state by raw IDs first and compared two orders of the same rows).
UNORDERED = ("events", "detector_state", "approvals", "runs")


def settle(normalised: dict[str, Any]) -> dict[str, Any]:
    """Sort the snapshot's sets by their normalised content (order-free comparison)."""
    out = dict(normalised)
    for key in UNORDERED:
        out[key] = sorted(out[key], key=canon)
    out["outputs"] = {k: sorted(v, key=canon) for k, v in out["outputs"].items()}
    return out


# ----- the transcript ----------------------------------------------------------------------


class Recorder:
    def __init__(self, store: str, transport: str, st: Store, evidence: Path) -> None:
        self.store, self.transport, self.st = store, transport, st
        self.evidence = evidence
        evidence.mkdir(parents=True, exist_ok=True)
        self.steps: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self.texts: dict[str, str] = {}

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


# ----- the agent (MCP) ---------------------------------------------------------------------


class McpChat:
    """One agent session: a real ``prometheos mcp serve --stdio`` process (its start runs the
    detector pass once) behind the official MCP SDK client."""

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
        return True, result.structured_content

    async def finished(self, ok: bool, view: Any) -> tuple[bool, Any]:
        """Follow an operation run to its end with ``run_get`` (as an agent would)."""
        deadline = time.monotonic() + 120
        while ok and view.get("state") in ("queued", "running") and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            ok, view = await self.call("run_get", {"run_id": view["run_id"]})
        return ok, view


def outcome_of(ok: bool, answer: Any) -> str:
    return "ok" if ok else f"error:{answer.get('code', '?')}"


def when(iso: str, zone: str) -> str:
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(zone))
    return f"{moment:%a %d %b %Y, %H:%M} ({zone})"


def summary(queue: dict[str, Any]) -> str:
    """What a session following the bundled work-items skill says: the open items by deadline,
    the events with their reasons, and one question. Built from the queue alone."""
    zone = queue["timezone"] or "UTC"
    items = [e for e in queue["entries"] if e["entry"] == "work_item"]
    events = [e for e in queue["entries"] if e["entry"] == "event"]
    lines = [f"You have {len(items)} open work items, by deadline:", ""]
    for n, e in enumerate(items, 1):
        due = f"due {when(e['due_at'], zone)}" if e["due_at"] else "no deadline"
        extra = [f"labels: {', '.join(e['labels'])}"] if e["labels"] else []
        if e["description_chars"]:
            extra.append(f"a description of {e['description_chars']} characters")
        more = f"; {'; '.join(extra)}" if extra else ""
        lines.append(f"{n}. {e['title']} ({e['status'].replace('_', ' ')}; {due}{more})")
    if events:
        lines += ["", "Things that need your attention:"]
        lines += [f"- {e.get('subject_title') or 'Project'}: {e['reason']}" for e in events]
    lines += ["", "Is there one of these you would like my help with now, and if so, which one?"]
    return "\n".join(lines) + "\n"


def message_assertions(message: str, queue: dict[str, Any]) -> list[dict[str, Any]]:
    """The C18-QUEUE text rules on the agent's own message (tests/test_cp18_queue_text.py)."""
    from test_cp18_queue_text import violations

    problems = violations(message)
    items = [e for e in queue["entries"] if e["entry"] == "work_item"]
    return [
        check(
            "the message lists every open item",
            all(e["title"] in message for e in items),
            [e["title"] for e in items],
        ),
        check(
            "it ends with exactly one question to the researcher",
            message.rstrip().endswith("?") and message.count("?") == 1,
        ),
        check(
            "it has no start directive and no urgency (C18-QUEUE text rules)",
            not problems,
            problems,
        ),
    ]


def queue_parts(queue: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries = queue.get("entries", [])
    return (
        [e for e in entries if e["entry"] == "work_item"],
        [e for e in entries if e["entry"] == "event"],
    )


def deadline_notices(st: Store) -> dict[str, int]:
    found: dict[str, int] = {}
    for e in st.events("deadline.approaching"):
        label = st.names.get(e["subject_ref"]["id"], "<other>")
        found[f"{label}:{e['data']['days_before']}"] = (
            found.get(f"{label}:{e['data']['days_before']}", 0) + 1
        )
    return found


EXPECTED_NOTICES = {f"{label}:{days}": 1 for label, days in THRESHOLD.items()}


def notices_check(st: Store, claim: str) -> dict[str, Any]:
    found = deadline_notices(st)
    return check(claim, found == EXPECTED_NOTICES, found)


async def first_session(st: Store, rec: Recorder, *, hub: bool) -> None:
    """W2.1 (MCP half only), W3.1-W4.4: the agent's session, the researcher's answer, the work."""
    from cp7_support import text_evidence

    from prometheos.domain.ids import new_request_id

    async with McpChat(st) as chat:
        if not hub:
            # The MCP server's start pass (decision 0035 item 6) found the deadlines.
            listed = st.svc.event_list(
                st.pid, st.owner, event_type="deadline.approaching", state="open", limit=200
            )
            rec.add(
                "W2.1",
                "owner-service",
                {"type": "deadline.approaching", "state": "open"},
                listed,
                "ok",
                [
                    notices_check(
                        st, "each deadline fired its tightest threshold once (14, 7, 2 days)"
                    ),
                    check(
                        "the item without a deadline has no deadline event",
                        "tidy" not in {k.split(":")[0] for k in deadline_notices(st)},
                    ),
                ],
            )
        before = st.digest()
        ok, queue = await chat.call("work_queue", {})
        after = st.digest()
        items, events = queue_parts(queue if ok else {})
        rec.add(
            "W3.1",
            "mcp",
            {},
            queue,
            outcome_of(ok, queue),
            [
                check("reading the queue changed nothing in the store", before == after),
                check(
                    "the queue lists the four open items, by deadline (no deadline last)",
                    [e["title"] for e in items] == BY_DEADLINE,
                    [e["title"] for e in items],
                ),
                check(
                    "it lists the three deadline events with their reasons",
                    sorted(
                        (st.names.get(e["subject_ref"]["id"]), e["data"]["days_before"])
                        for e in events
                        if e["type"] == "deadline.approaching"
                    )
                    == sorted(THRESHOLD.items()),
                ),
                check(
                    "the owner's own additions raise no open event",
                    not [e for e in events if e["type"].startswith("work_item.")],
                ),
                notices_check(st, "the server start pass added no duplicate deadline event"),
                check("no item was claimed or started", all(e["status"] == "ready" for e in items)),
            ],
        )
        message = summary(queue)
        rec.texts["W3.2"] = message
        rec.add(
            "W3.2",
            "mcp-client",
            {"skill": "work-items"},
            {"message": message},
            "ok",
            [
                *message_assertions(message, queue),
                check("the store is unchanged while the agent asks", st.digest() == before),
            ],
        )
        rec.texts["W3.3"] = ANSWER
        rec.add(
            "W3.3",
            "chat",
            {},
            {"answer": ANSWER},
            "ok",
            [check("nothing changed until the researcher answered", st.digest() == before)],
        )

        # W4: the accepted item, ready -> in_progress -> done with its output as evidence.
        title = next(i[1] for i in ITEMS if i[0] == ACCEPTED)
        item = next(e for e in items if e["title"] == title)
        action_id = item["ref"]
        ok, info = await chat.call("project_info", {})
        start = {
            "schema_version": 1, "project_id": st.pid, "request_id": new_request_id(),
            "expected_seq": info["project_seq"],
            "operations": [{"op": "transition", "id": action_id, "base_rev": item["rev"],
                            "to": "in_progress", "reason": "The researcher accepted it",
                            "evidence": []}],
        }  # fmt: skip
        ok, started = await chat.call("apply", {"request": start})
        rec.add(
            "W4.1",
            "mcp",
            start,
            started,
            outcome_of(ok, started),
            [
                check(
                    "the accepted item is in progress",
                    st.action(ACCEPTED)["body"]["status"] == "in_progress",
                ),
                check(
                    "no other item changed",
                    all(st.action(i[0])["body"]["status"] == "ready" for i in ITEMS[1:]),
                ),
            ],
        )

        # The agent writes its output with its own tools, then records it in PrometheOS.
        target = st.research / OUTPUT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(OUTPUT_TEXT.encode("utf-8"))
        plan_args = {
            "root_alias": "research",
            "paths": [OUTPUT_PATH],
            "retention": "copy",
            "wait_seconds": 20,
        }
        ok_plan, planned = await chat.finished(*await chat.call("source_import_plan", plan_args))
        plan_id = ((planned.get("result") or {}).get("plan_id", "")) if ok_plan else ""
        ok_commit, committed = await chat.finished(
            *await chat.call("source_import_commit", {"plan_id": plan_id, "wait_seconds": 20})
        )
        result = (committed.get("result") or {}) if ok_commit else {}
        recorded = [r["id"] for r in result.get("results", [])]
        version_id = next((i for i in recorded if i.startswith("version_")), "")
        source_id = next((i for i in recorded if i.startswith("source_")), "")
        st.name(version_id, "output_version")
        st.name(source_id, "output_source")
        st.name(plan_id, "output_plan")
        stored = st.svc.get(st.pid, st.owner, version_id, None) if version_id else {"body": {}}
        sha = hashlib.sha256(OUTPUT_TEXT.encode("utf-8")).hexdigest()
        rec.add(
            "W4.2",
            "mcp",
            {"plan": plan_args, "commit": {"plan_id": plan_id}},
            {"plan": planned, "commit": committed},
            "ok" if ok_plan and ok_commit else "error",
            [
                check(
                    "both runs completed",
                    planned.get("state") == "completed" and committed.get("state") == "completed",
                    [planned.get("state"), committed.get("state")],
                ),
                check(
                    "the output is a retained version pinned by its digest",
                    stored["body"].get("sha256") == sha
                    and stored["body"].get("availability") == "retained",
                    {k: stored["body"].get(k) for k in ("sha256", "availability", "storage")},
                ),
                check("the commit recorded it", bool(result)),
            ],
        )
        ok, info = await chat.call("project_info", {})
        evidence = text_evidence({"source_id": source_id, "version_id": version_id}, 1)
        finish = {
            "schema_version": 1, "project_id": st.pid, "request_id": new_request_id(),
            "expected_seq": info["project_seq"],
            "operations": [{"op": "transition", "id": action_id, "base_rev": item["rev"] + 1,
                            "to": "done", "reason": "Cover letter drafted and recorded",
                            "evidence": [evidence]}],
        }  # fmt: skip
        ok, done = await chat.call("apply", {"request": finish})
        body = st.action(ACCEPTED)["body"]
        rec.add(
            "W4.3",
            "mcp",
            finish,
            done,
            outcome_of(ok, done),
            [
                check("the item is done", body["status"] == "done"),
                check(
                    "its completion evidence points at the recorded output",
                    [e["version_id"] for e in body["completion_evidence"]] == [version_id],
                ),
                check(
                    "the owner's deadline, description and labels are unchanged",
                    (body["due_at"], body["description"], body["labels"])
                    == (st.due[ACCEPTED], ITEMS[0][4], ITEMS[0][5]),
                ),
            ],
        )
        ok, queue = await chat.call("work_queue", {})
        items, events = queue_parts(queue if ok else {})
        rec.add(
            "W4.4",
            "mcp",
            {},
            queue,
            outcome_of(ok, queue),
            [
                check(
                    "the done item left the queue",
                    [e["title"] for e in items] == [t for t in BY_DEADLINE if t != ITEMS[0][1]],
                    [e["title"] for e in items],
                ),
                check(
                    "its deadline event stays until the owner acknowledges it",
                    any(
                        e["type"] == "deadline.approaching" and e["subject_ref"]["id"] == action_id
                        for e in events
                    ),
                ),
            ],
        )


async def second_session(st: Store, rec: Recorder) -> None:
    """W5.3: a fresh agent session (a new server start pass) reads the queue."""
    async with McpChat(st) as chat:
        ok, queue = await chat.call("work_queue", {})
    items, events = queue_parts(queue if ok else {})
    p1 = st.ids().get(ACCEPTED, "")
    rec.add(
        "W5.3",
        "mcp",
        {},
        queue,
        outcome_of(ok, queue),
        [
            check(
                "the acknowledged deadline event left the queue",
                not [
                    e
                    for e in events
                    if e["subject_ref"]["id"] == p1 and e["type"] == "deadline.approaching"
                ],
            ),
            check(
                "the other deadline events remain",
                sorted(
                    st.names.get(e["subject_ref"]["id"])
                    for e in events
                    if e["type"] == "deadline.approaching"
                )
                == ["referee", "reply"],
            ),
            check("three open items remain", len(items) == 3),
            notices_check(st, "no deadline event repeats across restarts"),
        ],
    )


# ----- the MCP half --------------------------------------------------------------------------


def owner_add_step(st: Store, rec: Recorder, step_id: str, label: str) -> None:
    result = st.owner_add(label)
    body = st.action(label)["body"]
    rec.add(
        step_id,
        "owner-service",
        item_body(label, st.due),
        st.action(label),
        "ok" if result.get("ok") else "error",
        [
            check("the item is stored ready", body["status"] == "ready"),
            check("its deadline is stored in UTC", body["due_at"] == st.due.get(label)),
        ],
    )


async def mcp_phase(st: Store, rec: Recorder) -> None:
    from cp1_support import OWNER_ACT

    for n, (label, *_rest) in enumerate(ITEMS, 1):
        owner_add_step(st, rec, f"W1.{n}", label)
    await first_session(st, rec, hub=False)
    body = st.action(ACCEPTED)["body"]
    rec.add(
        "W5.1",
        "owner-service",
        {"record": ACCEPTED},
        st.action(ACCEPTED),
        "ok",
        [
            check("the owner sees the item done", body["status"] == "done"),
            check("with one cited output", len(body["completion_evidence"]) == 1),
        ],
    )
    event = next(
        e for e in st.events("deadline.approaching") if e["subject_ref"]["id"] == st.ids()[ACCEPTED]
    )
    st.name(event["event_id"], "p1_deadline_event")
    acked = st.svc.event_acknowledge(st.pid, [event["event_id"]], ACK_NOTE, authority=OWNER_ACT)
    stored = next(e for e in st.events() if e["event_id"] == event["event_id"])
    rec.add(
        "W5.2",
        "owner-service",
        {"event_ids": ["p1_deadline_event"], "note": ACK_NOTE},
        acked,
        "ok",
        [
            check(
                "the event is acknowledged with the owner's note",
                stored["acknowledged_at"] is not None and stored["acknowledged_note"] == ACK_NOTE,
            )
        ],
    )
    await second_session(st, rec)


# ----- the hub half (Playwright against the real hub, the agent over MCP) ------------------


def hub_fixture(st: Store) -> dict[str, Any]:
    items = []
    for label, title, reason, _days, description, labels in ITEMS:
        due = st.due.get(label)
        date, clock = local(due) if due else ("", "")
        items.append(
            {
                "label": label, "title": title, "reason": reason, "date": date, "time": clock,
                "due_at": due, "description": description, "labels": labels,
                "threshold": THRESHOLD.get(label),
            }
        )  # fmt: skip
    return {"zone": ZONE, "items": items, "accepted": ACCEPTED, "ack_note": ACK_NOTE}


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
            "WALK_PW_OUT": str(Path(tempfile.mkdtemp(prefix="prometheos-walk18-pw-"))),
            "BOUNDARY_EVIDENCE": str(out / "browser-boundary.json"),
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(out / "playwright-report.json"),
            "WALK_FIXTURE": json.dumps(hub_fixture(st)),
        }
        pw_log = open(out / "playwright.log", "w", encoding="utf-8")
        browser = subprocess.Popen(
            [*playwright, "-c", "playwright.walk18.config.ts", "--reporter=line,json"],
            cwd=ROOT / "hub",
            env=env,
            stdout=pw_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # The owner's first steps run in the browser; then the agent's session runs while the
        # hub keeps serving, and the browser waits for it before the owner's last steps.
        deadline = time.monotonic() + 420
        while not (signals / "owner-1.done").exists():
            if browser.poll() is not None or time.monotonic() > deadline:
                break
            time.sleep(0.25)
        if (signals / "owner-1.done").exists():
            for step in json.loads(steps_path.read_text(encoding="utf-8")):
                for name, record_id in step.get("created", {}).items():
                    st.name(record_id, name)
            try:
                asyncio.run(first_session(st, rec, hub=True))
            except Exception as exc:
                (signals / "agent.failed").write_text(repr(exc), encoding="utf-8")
                raise
            (signals / "agent.done").write_text("done", encoding="utf-8")
        try:
            code = browser.wait(timeout=600)
        finally:
            pw_log.close()
        if code != 0:
            rec.failures.append(f"hub: the Playwright walk failed (exit {code})")
            print((out / "playwright.log").read_text(encoding="utf-8")[-4000:])
        if steps_path.exists():
            for step in json.loads(steps_path.read_text(encoding="utf-8")):
                for name, record_id in step.get("created", {}).items():
                    st.name(record_id, name)
        if code == 0:
            asyncio.run(second_session(st, rec))
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
    lines = ["# C18-WALK transcript", "", doc["summary"], ""]
    lines += [f"Deadlines (UTC, shared by both stores): {doc['deadlines']}", ""]
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
        texts = data.get("conversation", {})
        if texts:
            lines += ["### The conversation", "", "The agent (W3.2):", ""]
            lines += [f"> {line}" if line else ">" for line in texts["W3.2"].rstrip().split("\n")]
            lines += ["", f"The researcher (W3.3): {texts['W3.3']}", ""]
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


def run(out: Path | None, only: str | None, keep: bool, show: bool) -> int:
    from cp1_support import AGENT  # noqa: F401  (imported to fail early when tests/ is absent)

    out = (out or Path(tempfile.mkdtemp(prefix="prometheos-walk18-out-"))).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into a folder that is not empty: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prometheos-walk18-"))
    due = deadlines()
    doc: dict[str, Any] = {
        "walk": "C18-WALK",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "script": SCRIPT_IDS,
        "deadlines": due,
        "limitations": [
            "The MCP half's owner steps (W1.x, W5.1, W5.2) run through the service in the "
            "harness, a synthetic owner channel: W1.x applies the hub's exact add request with a "
            "SYNTHETIC-TEST owner decision bound to its digest. The hub half's owner steps are "
            "the real browser.",
            "The agent is a scripted MCP client following the bundled work-items skill; its "
            "message (W3.2) is built from the queue and checked with the C18-QUEUE text rules. "
            "Real agent behaviour is observed in E18.",
        ],
    }
    failures: list[str] = []
    outcomes: dict[str, Any] = {}
    equivalence: dict[str, Any] = {}
    try:
        if only in (None, "hub"):
            store_a = Store(work / "store-a", due)
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
                "conversation": rec_a.texts,
            }
            failures += rec_a.failures
            doc["browser_boundary"], problems = boundary_evidence(out)
            failures += problems
        if only in (None, "mcp"):
            note = hub_stopped_note(out)
            equivalence["the hub was stopped during the MCP half"] = note
            if note == "NOT STOPPED":
                failures.append("the hub still answered during the MCP half")
            store_b = Store(work / "store-b", due)
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
                "conversation": rec_b.texts,
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
