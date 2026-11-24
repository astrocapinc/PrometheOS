"""C19-WALK: the CP19 end-to-end walk on the built desktop app (spec/10 CP19.4; decision 0055).

    uv run python tools/walk_cp19.py [--out DIR] [--print-transcript] [--only desktop|mcp] [--keep]

1. **Desktop leg (store A).** The built app (``desktop/app/dist/win-unpacked/PrometheOS.exe``
   with its bundled core; build it first with ``npm --prefix desktop/app run dist``) serves a
   fresh store. A Playwright spec (``desktop/app/walk/walk19.spec.ts``, Playwright's Electron
   driver) does the owner's steps in the app: it starts hidden and is opened from its tray
   icon, pairs through the hub's own route, opens quick capture from the global hotkey (pressed
   with SendInput) and saves a note, sees the agent's approval request as a system
   notification, reviews and grants it in the window, then quits and restarts. Between those,
   an agent session over the real ``prometheos mcp serve --stdio`` process (the official MCP
   SDK client) suggests a work item, asks to acknowledge its event (``approval_required``),
   resumes the granted request, and a fresh session reads the result.
2. **MCP leg (store B).** On a second fresh store, with the desktop app stopped (no process of
   the built app runs and the leg-A hub's port refuses connections), the same script runs
   through MCP alone: the owner's steps go through the service in the harness (a synthetic
   owner channel, as in C10-WALK and C18-WALK), every agent step is the same MCP call.
3. The two stores' normalised outcomes (notebook entries, work items, events, detector state,
   approvals, runs; IDs and times normalised), every step's outcome and the normalised answer
   of every agent step must be equal.

Output in ``--out`` (a new folder): ``transcript.json`` and ``transcript.md``,
``outcome-desktop.json`` and ``outcome-mcp.json``, ``evidence/`` (raw responses), the app's
screenshots, ``desktop-steps.jsonl`` (what the window and the shell's state showed) and the
Playwright log. Exit 0 only when every assertion passed and the outcomes are equal.

Synthetic data only; nothing is sent anywhere. The app's window and hotkey are real: the walk
shows a window on this desktop for a few minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

import walk_cp18  # noqa: E402
from walk_cp10 import canon, check, digest, first_difference  # noqa: E402
from walk_cp18 import McpChat, outcome_of  # noqa: E402

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

APP = ROOT / "desktop" / "app"
EXE = APP / "dist" / "win-unpacked" / "PrometheOS.exe"
ASAR = APP / "dist" / "win-unpacked" / "resources" / "app.asar"
PLAYWRIGHT_CLI = APP / "node_modules" / "@playwright" / "test" / "cli.js"
PRESS_HOTKEY = ROOT / "desktop" / "spike" / "press_hotkey.py"
ZONE = "Asia/Riyadh"

NOTE_TITLE = "Walk note from the hotkey"
NOTE_TEXT = "Captured in the desktop window (C19-WALK)."
SUGGESTION = "Collect the figure scripts into one folder"
ACK_NOTE = "Seen: the suggestion waits in Work for the researcher."
APPROVAL_NOTICE = "An approval was requested for event_acknowledge"

# Every step of the script, in order. ``owner`` steps run in the desktop app on store A and
# through the service (a synthetic owner channel) on store B; ``agent`` steps are the same MCP
# call on both stores.
STEPS: list[dict[str, Any]] = [
    {
        "id": "W1.1",
        "actor": "owner",
        "operation": "open the app from its tray icon; it pairs",
        "service": "read the project (no app on this leg)",
    },
    {
        "id": "W2.1",
        "actor": "owner",
        "operation": "quick capture through the hotkey: save a note",
        "service": "apply the window's capture request through the hub's service",
    },
    {"id": "W3.1", "actor": "agent", "operation": "suggest a work item (apply, proposed)"},
    {"id": "W3.2", "actor": "agent", "operation": "ask to acknowledge its event (approval)"},
    {"id": "W3.3", "actor": "agent", "operation": "boundary: resume before the owner decides"},
    {
        "id": "W4.1",
        "actor": "owner",
        "operation": "the approval request is announced",
        "service": "list the open events",
    },
    {
        "id": "W4.2",
        "actor": "owner",
        "operation": "review the request's plan and grant it",
        "service": "read the owner view and decide on its plan digest",
    },
    {"id": "W5.1", "actor": "agent", "operation": "resume the granted request"},
    {
        "id": "W6.1",
        "actor": "owner",
        "operation": "quit and restart; nothing is lost",
        "service": "reopen the store with a second service",
    },
    {"id": "W6.2", "actor": "agent", "operation": "a new session reads the request and events"},
]
SCRIPT_IDS = [s["id"] for s in STEPS]
AGENT_IDS = [s["id"] for s in STEPS if s["actor"] == "agent"]
DESKTOP_OWNER_IDS = [s["id"] for s in STEPS if s["actor"] == "owner"]


class Store(walk_cp18.Store):
    """A fresh project store (no research root needed), plus the walk's names for its IDs."""

    def __init__(self, work: Path) -> None:
        from prometheos.app.reads import Caller
        from prometheos.app.service import Service

        self.work, self.due = work, {}
        self.home = work / "home"
        self.svc = Service(home=self.home, actor="hub")
        # The hub's owner caller (hub/app.py OWNER): the owner's capture on store B is the same
        # request at the same scope as the window's.
        self.owner = Caller(actor="hub-owner", scope="owner")
        self.pid: str = self.svc.init_project(
            title="Synthetic study (C19-WALK)",
            objective="Show the desktop app, its notifications and the agent's approval flow.",
            timezone=ZONE,
        )["project_id"]
        self.names: dict[str, str] = {}

    def name_events(self) -> None:
        """Every event by its type and order (the walk raises at most one of each)."""
        seen: dict[str, int] = {}
        for event in sorted(self.events(), key=lambda e: e["occurred_at"]):
            n = seen[event["type"]] = seen.get(event["type"], 0) + 1
            self.name(event["event_id"], f"event:{event['type']}:{n}")

    def entries(self) -> list[dict[str, Any]]:
        listed = self.svc.list_records(self.pid, self.owner, kind="entry", limit=200)
        return list(listed["items"])

    def approvals(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows("SELECT * FROM approval_requests")]


def snapshot(st: Store) -> dict[str, Any]:
    """C18's normalised outcome (work items, events, detector state, approvals, runs, outputs),
    the notebook's entries, and the store's relations, flags and authority results."""
    out = walk_cp18.snapshot(st)
    out["entries"] = sorted(
        (
            st.norm(
                {
                    "label": st.names.get(r["id"], "<other>"),
                    "rev": r["rev"],
                    **r["body"],
                }
            )
            for r in st.entries()
        ),
        key=canon,
    )
    # The store's own rows (review s3d-a F1): records with their sensitivity and lifecycle,
    # revisions, bodies, events with who acknowledged them, relations, flags, the audit and
    # the authority results. Times become "set or not"; IDs, digests and UUIDs placeholders.
    out["records"] = table(st, "records")
    out["revisions"] = table(st, "revisions")
    out["bodies"] = table(st, "bodies")
    out["event_rows"] = table(st, "events")
    out["relations"] = table(st, "relations")
    out["refs"] = table(st, "refs")
    out["view_flags"] = table(st, "view_flags")
    out["audit"] = table(st, "audit")
    out["authority"] = {
        "approval_requests": table(st, "approval_requests"),
        "capabilities": table(st, "capabilities", drop=("secret_sha256",)),
    }
    return out


def table(st: Store, name: str, drop: tuple[str, ...] = ()) -> list[Any]:
    """Every row of a table, normalised and order-free; JSON text columns are parsed."""
    rows = []
    for row in st.rows(f"SELECT * FROM {name}"):  # noqa: S608 (the walk's own table names)
        item: dict[str, Any] = {}
        for key in row.keys():
            value = row[key]
            if key in drop or key == "project_id":
                continue
            if key.endswith("_at"):
                item[key] = value is not None
                continue
            if isinstance(value, str) and value[:1] in "[{":
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            item[key] = value
        rows.append(st.norm(item))
    return sorted(rows, key=canon)


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
        for suffix, value in ((".json", response), (".normalised.json", normalised)):
            (self.evidence / f"{step_id}{suffix}").write_text(
                json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
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
                "operation": meta.get("service", meta["operation"])
                if via == "owner-service"
                else meta["operation"],
                "request": self.st.norm(request),
                "outcome": outcome,
                "response_sha256": digest(response),
                "normalised_response_sha256": digest(normalised),
                "store_assertions": assertions,
            }
        )

    def ordered(self) -> list[dict[str, Any]]:
        return sorted(self.steps, key=lambda s: SCRIPT_IDS.index(s["id"]))


# ----- the owner's steps: the store's side (both legs) --------------------------------------


def fresh_checks(st: Store, before: str) -> list[dict[str, Any]]:
    return [
        check("opening the app changed nothing in the store", st.digest() == before),
        check(
            "the store is fresh: no entries, work items, events or approvals",
            not st.entries() and not st.events() and not st.approvals(),
        ),
    ]


def capture_request(st: Store, entry_id: str) -> bytes:
    """The window's capture request (hub/src/pages/Capture.tsx), as it sends it."""
    from prometheos.domain.ids import new_request_id

    body = {"title": NOTE_TITLE, "text": NOTE_TEXT, "category": "note", "evidence": [],
            "claim_refs": [], "author_ref": None, "origin": "typed", "quantities": [],
            "review": "reviewed"}  # fmt: skip
    seq = st.svc.project_info(st.pid, st.owner)["project_seq"]
    request = {"schema_version": 1, "project_id": st.pid, "request_id": new_request_id(),
               "expected_seq": seq,
               "operations": [{"op": "create", "kind": "entry", "id": entry_id, "base_rev": 0,
                               "sensitivity": "normal", "occurred_at": None,
                               "body": body}]}  # fmt: skip
    return json.dumps(request).encode("utf-8")


def capture_checks(st: Store) -> list[dict[str, Any]]:
    entries = [e for e in st.entries() if e["body"]["title"] == NOTE_TITLE]
    for entry in entries:
        st.name(entry["id"], "captured_note")
    body = entries[0]["body"] if entries else {}
    return [
        check("one note was saved to the notebook", len(entries) == 1, len(entries)),
        check(
            "it holds the owner's text, typed and reviewed",
            (body.get("text"), body.get("origin"), body.get("review"))
            == (NOTE_TEXT, "typed", "reviewed"),
            {k: body.get(k) for k in ("text", "origin", "review")},
        ),
        check("saving a note raised no event", not st.events()),
    ]


def announced_checks(st: Store) -> list[dict[str, Any]]:
    st.name_events()
    open_events = st.svc.event_list(st.pid, st.owner, state="open", limit=200)["items"]
    asked = [e for e in open_events if e["type"] == "approval.requested"]
    return [
        check("the approval request is an open research event", len(asked) == 1, len(asked)),
        check(
            "its reason names the operation and says the researcher decides",
            bool(asked) and asked[0]["reason"].startswith(APPROVAL_NOTICE),
            asked[0]["reason"] if asked else None,
        ),
    ]


def granted_checks(st: Store, approval_id: str) -> list[dict[str, Any]]:
    row = next((a for a in st.approvals() if a["id"] == approval_id), {})
    st.name(row.get("capability_id") or "", "ack_capability")
    event = next(
        e for e in st.events() if st.names.get(e["event_id"]) == "event:work_item.created:1"
    )
    return [
        check("the request is granted", row.get("state") == "granted", row.get("state")),
        check("granting did not acknowledge the event itself", event["acknowledged_at"] is None),
    ]


# ----- the agent (MCP), shared by both legs --------------------------------------------------


async def agent_suggests(st: Store, rec: Recorder, chat: McpChat) -> str:
    """W3.1 and W3.2. Answers the approval request's ID."""
    from prometheos.domain.ids import new_id, new_request_id

    action_id = st.name(new_id("action"), "suggested_item")
    body = {"title": SUGGESTION, "reason": "The scripts are spread over three folders.",
            "source_refs": [], "owner_ref": None, "due_at": None, "dependencies": [],
            "status": "proposed", "blocker": None, "completion_evidence": [],
            "commitment_item_id": None, "description": None, "labels": []}  # fmt: skip
    ok, info = await chat.call("project_info", {})
    suggest = {"schema_version": 1, "project_id": st.pid, "request_id": new_request_id(),
               "expected_seq": info["project_seq"],
               "operations": [{"op": "create", "kind": "action", "id": action_id,
                               "base_rev": 0, "sensitivity": "normal", "occurred_at": None,
                               "body": body}]}  # fmt: skip
    ok, applied = await chat.call("apply", {"request": suggest})
    st.name_events()
    created = [e for e in st.events("work_item.created") if e["acknowledged_at"] is None]
    rec.add(
        "W3.1",
        "mcp",
        suggest,
        applied,
        outcome_of(ok, applied),
        [
            check(
                "the suggestion is stored as a proposed work item",
                st.action("suggested_item")["body"]["status"] == "proposed",
            ),
            check("it raised one open work_item.created event", len(created) == 1, len(created)),
            check("nothing was asked of the owner yet", not st.approvals()),
        ],
    )
    event_id = created[0]["event_id"] if created else ""
    request_id = new_request_id()
    st.name(request_id, "ack_request")
    args = {"event_ids": [event_id], "note": ACK_NOTE, "request_id": request_id}
    ok, asked = await chat.call("event_acknowledge", args)
    approval_id = str(asked.get("details", {}).get("approval_request_id", "")) if not ok else ""
    st.name(approval_id, "ack_approval")
    st.name_events()
    rows = st.approvals()
    rec.add(
        "W3.2",
        "mcp",
        args,
        asked,
        outcome_of(ok, asked),
        [
            check(
                "the agent was answered approval_required",
                outcome_of(ok, asked) == "error:approval_required",
                outcome_of(ok, asked),
            ),
            check(
                "one pending request for event_acknowledge is stored",
                [(r["operation"], r["state"]) for r in rows] == [("event_acknowledge", "pending")],
                [(r["operation"], r["state"]) for r in rows],
            ),
            check(
                "the event is not acknowledged before the owner decides",
                all(e["acknowledged_at"] is None for e in st.events("work_item.created")),
            ),
        ],
    )
    # W3.3, a boundary control: before the owner decides, the request does not run, and the
    # agent has no tool that decides one.
    before = st.digest()
    ok, early = await chat.call("approval_resume", {"approval_id": approval_id})
    tools = sorted(t.name for t in (await chat.client.list_tools()).tools)
    pending = [r["state"] for r in st.approvals()] == ["pending"]
    rec.add(
        "W3.3",
        "mcp",
        {"approval_id": approval_id},
        {"resume": early if ok else {"ok": False, "error": early}, "tools": tools},
        outcome_of(ok, early),
        boundary_checks(outcome_of(ok, early), st.digest() == before, pending, tools),
    )
    return approval_id


# The agent's approval tools: it reads, resumes, withdraws or stops its own requests and never
# decides one (A02, decision 0016). Any other approval tool, or any tool that grants, denies,
# decides or revokes, fails the boundary control (an allowlist; review s3d-a F7).
AGENT_APPROVAL_TOOLS = frozenset(
    {"approval_get", "approval_list", "approval_resume", "approval_withdraw", "approval_stop"}
)
DECIDING = ("grant", "deny", "decide", "revoke")


def boundary_checks(
    outcome: str, unchanged: bool, pending: bool, tools: list[str]
) -> list[dict[str, Any]]:
    return [
        check("resuming an undecided request is refused", outcome.startswith("error:"), outcome),
        check("the refusal changed nothing in the store", unchanged),
        check("the request still waits for the owner", pending),
        check(
            "the agent has no tool that decides a request",
            bool(tools)
            and {t for t in tools if t.startswith("approval_")} <= AGENT_APPROVAL_TOOLS
            and not [t for t in tools if any(word in t for word in DECIDING)],
            [t for t in tools if t.startswith("approval_")],
        ),
    ]


async def agent_resumes(st: Store, rec: Recorder, chat: McpChat, approval_id: str) -> None:
    ok, resumed = await chat.call("approval_resume", {"approval_id": approval_id})
    st.name_events()
    row = next((a for a in st.approvals() if a["id"] == approval_id), {})
    event = next(
        (e for e in st.events() if st.names.get(e["event_id"]) == "event:work_item.created:1"),
        {},
    )
    rec.add(
        "W5.1",
        "mcp",
        {"approval_id": approval_id},
        resumed,
        outcome_of(ok, resumed),
        [
            check("the request ran once and is used", row.get("state") == "used", row.get("state")),
            check(
                "the event is acknowledged with the agent's note",
                event.get("acknowledged_at") is not None
                and event.get("acknowledged_note") == ACK_NOTE,
                {k: event.get(k) for k in ("acknowledged_at", "acknowledged_note")},
            ),
        ],
    )


async def agent_rereads(st: Store, rec: Recorder, approval_id: str) -> None:
    """W6.2: a fresh session (a new server start pass) reads the request and the events."""
    before = st.digest()
    async with McpChat(st) as chat:
        ok_a, request = await chat.call("approval_get", {"approval_id": approval_id})
        ok_e, events = await chat.call("event_list", {"state": "all", "limit": 50})
    st.name_events()
    rec.add(
        "W6.2",
        "mcp",
        {"approval_get": {"approval_id": approval_id}, "event_list": {"state": "all"}},
        {"approval": request, "events": events},
        "ok" if ok_a and ok_e else "error",
        [
            check(
                "the request reads as used, with its result",
                ok_a and request.get("state") == "used",
                request.get("state") if ok_a else request,
            ),
            check(
                "the session's start pass and reads changed nothing",
                st.digest() == before,
            ),
            check(
                "no event repeats across the restart",
                ok_e
                and len({e["event_id"] for e in events["items"]})
                == len(events["items"])
                == len(st.events()),
            ),
        ],
    )


# ----- the MCP leg ---------------------------------------------------------------------------


async def mcp_leg(st: Store, rec: Recorder) -> None:
    from prometheos.app.authority import OwnerAct
    from prometheos.app.service import Service
    from prometheos.domain.ids import new_id

    # The hub's own owner act (hub/app.py HUB_OWNER_ACT), given in the harness: the decision's
    # recorded author ("acknowledged_by ... via" in the agent's result) is the same on both legs.
    owner_act = OwnerAct(channel="hub-owner", actor="hub-owner")

    before = st.digest()
    info = st.svc.project_info(st.pid, st.owner)
    rec.add("W1.1", "owner-service", {}, info, "ok", fresh_checks(st, before))
    entry_id = new_id("entry")
    raw = capture_request(st, entry_id)
    # The service `prometheos hub serve` builds (cli.py: Service(actor="cli")) applies the
    # window's request, so its audit row names the same actor as on the desktop leg.
    saved = Service(home=st.home, actor="cli").apply(st.pid, raw, st.owner)
    rec.add("W2.1", "owner-service", json.loads(raw), saved,
            "ok" if saved.get("ok") else "error", capture_checks(st))  # fmt: skip
    async with McpChat(st) as chat:
        approval_id = await agent_suggests(st, rec, chat)
        listed = st.svc.event_list(st.pid, st.owner, state="open", limit=200)
        rec.add("W4.1", "owner-service", {"state": "open"}, listed, "ok", announced_checks(st))
        view = st.svc.approval_owner_view(st.pid, approval_id)
        decided = st.svc.approval_decide(
            st.pid, approval_id, grant=True, plan_sha256=view["plan_sha256"], owner=owner_act
        )
        granted = granted_checks(st, approval_id)
        rec.add("W4.2", "owner-service", {"approval_id": approval_id, "grant": True},
                {"view": view, "decided": decided}, "ok", granted)  # fmt: skip
        await agent_resumes(st, rec, chat, approval_id)
    before = st.digest()
    # No app to quit on this leg: a second service over the same home reopens the store.
    seq = Service(home=st.home, actor="hub").project_info(st.pid, st.owner)["project_seq"]
    rec.add(
        "W6.1",
        "owner-service",
        {"reopen": True},
        {"project_seq": seq},
        "ok",
        [check("reopening the store loses and changes nothing", st.digest() == before)],
    )
    await agent_rereads(st, rec, approval_id)


# ----- the desktop leg -----------------------------------------------------------------------


class Leg:
    """The hand-over with the Playwright spec: it writes signals into one folder, so do we."""

    def __init__(self, signals: Path, browser: subprocess.Popen[str]) -> None:
        self.signals, self.browser = signals, browser

    async def wait(self, name: str, seconds: float = 420) -> dict[str, Any]:
        path = self.signals / f"{name}.json"
        deadline = time.monotonic() + seconds
        while not path.exists():
            if self.browser.poll() is not None:
                raise RuntimeError(f"the desktop spec ended before it sent {name}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the desktop spec did not send {name} in time")
            await asyncio.sleep(0.25)
        await asyncio.sleep(0.1)  # the writer has closed it
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data

    def send(self, name: str, data: Any = None) -> None:
        temp = self.signals / f"{name}.tmp"
        temp.write_text(json.dumps(data or {}), encoding="utf-8")
        temp.replace(self.signals / f"{name}.json")


async def desktop_story(
    st: Store, rec: Recorder, leg: Leg, owner: dict[str, Any], before: str
) -> None:
    """The store's side of the owner's window steps, and the agent's steps between them.
    ``before`` is the store's digest taken before the app first started."""
    await leg.wait("owner-opened", 180)
    owner["W1.1"] = fresh_checks(st, before)
    leg.send("opened-checked")
    await leg.wait("owner-captured")
    owner["W2.1"] = capture_checks(st)
    async with McpChat(st) as chat:
        approval_id = await agent_suggests(st, rec, chat)
        plan = next((a["plan_sha256"] for a in st.approvals() if a["id"] == approval_id), "")
        leg.send("agent-requested", {"approval_id": approval_id, "plan_sha256": plan})
        owner["W4.1"] = announced_checks(st)  # the spec asserts the notification itself
        await leg.wait("owner-granted")
        owner["W4.2"] = granted_checks(st, approval_id)
        await agent_resumes(st, rec, chat, approval_id)
    leg.send("agent-resumed")
    await leg.wait("owner-quitting")
    before = st.digest()
    leg.send("store-before-restart")
    await leg.wait("owner-restarted", 180)
    owner["W6.1"] = [
        check("quitting and restarting the app changed nothing in the store", st.digest() == before)
    ]
    await agent_rereads(st, rec, approval_id)
    leg.send("agent-reread")


def built_app() -> dict[str, Any]:
    """The built app the walk drives, refused if it is missing or older than the shell's code."""
    if not EXE.is_file() or not ASAR.is_file():
        raise RuntimeError(f"build the app first (npm --prefix desktop/app run dist): {EXE}")
    asar = ASAR.read_bytes()
    # app.asar stores its files uncompressed: the shipped main.js must be the checkout's.
    if (APP / "main.js").read_bytes() not in asar:
        raise RuntimeError("the built app's main.js is not the checkout's: rebuild the app")
    # The bundled core's prometheos package must be the checkout's src, file by file (review
    # s3d-a F5): a stale core with a fresh main.js would otherwise pass.
    package = APP / "dist" / "win-unpacked" / "resources" / "core" / "Lib" / "site-packages"
    stale = [
        path.relative_to(ROOT / "src").as_posix()
        for path in sorted((ROOT / "src" / "prometheos").rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and (
            not (package / path.relative_to(ROOT / "src")).is_file()
            or (package / path.relative_to(ROOT / "src")).read_bytes() != path.read_bytes()
        )
    ]
    # And the other way (review s3d-a R2-F1): a module removed from src but still in the core.
    stale += [
        f"{path.relative_to(package).as_posix()} (not in src)"
        for path in sorted((package / "prometheos").rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and not (ROOT / "src" / path.relative_to(package)).is_file()
    ]
    if stale:
        raise RuntimeError(f"the built app's core is not the checkout's src: {stale[:5]}")
    manifest = APP / "dist" / "win-unpacked" / "resources" / "core" / "MANIFEST.json"
    return {
        "exe": str(EXE),
        "exe_sha256": hashlib.sha256(EXE.read_bytes()).hexdigest(),
        "app_asar_sha256": hashlib.sha256(asar).hexdigest(),
        "core_manifest": json.loads(manifest.read_text(encoding="utf-8")),
    }


def app_processes(home: Path | None = None, project_id: str = "") -> list[dict[str, Any]]:
    """Processes of the built app and its core. With ``home`` (and ``project_id``) only those of
    this walk: a command line naming the store's home or project (review s3d-a F7), so a
    cleanup never stops another instance of the app. Electron's helper processes (GPU,
    renderer, utility) are found by their own command line too: each carries
    a ``--user-data-dir`` inside the store's home, also after the main process has exited
    (decision 0056; parent IDs are not used, since Windows reuses them)."""
    script = (
        "Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine"
        " | ConvertTo-Json -Compress"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, timeout=120)  # fmt: skip
    rows = json.loads(out.stdout or "[]")
    rows = rows if isinstance(rows, list) else [rows]
    exe = str(EXE).lower()
    core = str(APP / "dist" / "win-unpacked" / "resources" / "core").lower()
    found = []
    for row in rows:
        path = (row.get("ExecutablePath") or "").lower()
        line = row.get("CommandLine") or ""
        ours = home is None or str(home) in line or bool(project_id and project_id in line)
        if (path == exe or path.startswith(core)) and ours:
            found.append({"pid": row["ProcessId"], "exe": row.get("ExecutablePath")})
        elif home is not None and str(home) in line:
            found.append({"pid": row["ProcessId"], "exe": row.get("ExecutablePath")})
    return [p for p in found if p["pid"] != os.getpid()]


def describe(exc: BaseException) -> str:
    """An exception's message, with an exception group's own causes rather than its summary
    (review s3d-a R2-F2: "unhandled errors in a TaskGroup" hid the spec's failure)."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(describe(inner) for inner in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def stop_processes(found: list[dict[str, Any]]) -> None:
    for proc in found:
        subprocess.run(["taskkill", "/PID", str(proc["pid"]), "/T", "/F"], capture_output=True)


def desktop_leg(st: Store, out: Path, rec: Recorder) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None or not PLAYWRIGHT_CLI.is_file():
        raise RuntimeError("Node and desktop/app/node_modules are needed for the desktop leg")
    app = built_app()
    stray = app_processes()
    if stray:
        raise RuntimeError(f"the built app is already running; stop it first: {stray}")
    signals = out / "signals"
    signals.mkdir()
    steps_path = out / "desktop-steps.jsonl"
    info = {"home": str(st.home), "project_id": st.pid, "exe": str(EXE), "signals": str(signals),
            "shots": str(out / "screenshots"), "steps": str(steps_path),
            "press_hotkey": str(PRESS_HOTKEY), "python": sys.executable}  # fmt: skip
    (out / "desktop-info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    env = {**os.environ, "WALK_INFO": str(out / "desktop-info.json"),
           "WALK_PW_OUT": tempfile.mkdtemp(prefix="prometheos-walk19-pw-")}  # fmt: skip
    env.pop("PROMETHEOS_PYTHON", None)
    owner: dict[str, list[dict[str, Any]]] = {}
    before = st.digest()
    with open(out / "playwright.log", "w", encoding="utf-8") as log:
        browser = subprocess.Popen(
            [node, str(PLAYWRIGHT_CLI), "test", "--config", "walk/playwright.config.ts"],
            cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
        )  # fmt: skip
        try:
            asyncio.run(desktop_story(st, rec, Leg(signals, browser), owner, before))
            code = browser.wait(timeout=300)
        except Exception as exc:
            rec.failures.append(f"desktop: {describe(exc)}")
            code = None
        finally:
            if browser.poll() is None:
                browser.kill()
    if code != 0:
        rec.failures.append(f"desktop: the Playwright spec failed (exit {code})")
        print((out / "playwright.log").read_text(encoding="utf-8")[-4000:])
    seen: dict[str, dict[str, Any]] = {}
    if steps_path.exists():
        for line in steps_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                step = json.loads(line)
                seen[step["id"]] = step
    for step_id in DESKTOP_OWNER_IDS:
        if step_id in seen or step_id in owner:
            shown = seen.get(step_id, {})
            asserted = owner.get(step_id, []) + window_checks(step_id, shown)
            rec.add(step_id, "desktop", {}, shown, "ok" if shown else "error", asserted)
    leftovers = app_processes(st.home, st.pid)
    stop_processes(leftovers)
    if leftovers:
        rec.failures.append(f"desktop: processes outlived the app's quit: {leftovers}")
    # Every hub the leg started (the first launch's and the restarted app's; F4).
    urls = [u for u in (seen.get("W1.1", {}).get("url"), seen.get("W6.1", {}).get("url")) if u]
    return {**app, "urls": urls, "leftover_processes": leftovers}


def window_checks(step_id: str, shown: dict[str, Any]) -> list[dict[str, Any]]:
    """What the window and the shell's state showed (recorded by the spec), as assertions."""
    if not shown:
        return [check("the desktop spec recorded this step", False)]
    if step_id == "W1.1":
        return [
            check(
                "the app started hidden, with its tray icon",
                bool(shown.get("hidden_at_start") and shown.get("tray")),
                shown,
            ),
            check(
                "the tray click showed the window", shown.get("visible_after_tray_click") is True
            ),
            check("the window is paired with the hub", shown.get("paired") is True),
            check(
                "without the test switch the built app has no test hooks, and the hub page "
                "none of Node's or the shell's globals",
                shown.get("hooks_without_switch") == "undefined"
                and shown.get("page_globals_without_switch") == " ".join(["undefined"] * 4)
                and shown.get("hooks_with_switch") == "function",
                {
                    k: shown.get(k)
                    for k in (
                        "hooks_without_switch",
                        "page_globals_without_switch",
                        "hooks_with_switch",
                    )
                },
            ),
            check(
                "the shell's guard: a main-process load and a page navigation to another "
                "loopback origin are blocked, and its server sees no request",
                "ERR_BLOCKED_BY_CLIENT" in str(shown.get("probe_main_load"))
                and shown.get("probe_navigation_stayed") is True
                and shown.get("probe_hits") == 0,
                {
                    k: shown.get(k)
                    for k in ("probe_main_load", "probe_navigation_stayed", "probe_hits")
                },
            ),
            check(
                "the hub is served over loopback",
                str(shown.get("url", "")).startswith("http://127.0.0.1:"),
                shown.get("url"),
            ),
        ]
    if step_id == "W2.1":
        return [
            check("the global hotkey fired once", shown.get("hotkey_fired") == 1, shown),
            check("the window showed the note saved", shown.get("saved") is True),
        ]
    if step_id == "W4.1":
        notes = shown.get("notifications", [])
        return [
            check(
                "a system notification announced the approval request",
                any(str(n).startswith(APPROVAL_NOTICE) for n in notes),
                notes,
            ),
            check(
                "the agent's suggestion was announced once",
                sum(SUGGESTION in str(n) for n in notes) == 1,
                notes,
            ),
        ]
    if step_id == "W4.2":
        return [
            check(
                "the owner reviewed and granted it in the window",
                shown.get("granted_in_window") is True,
            ),
            check(
                "the window showed the request's exact plan digest before the grant",
                shown.get("plan_digest_shown") is True,
                shown.get("plan_sha256"),
            ),
        ]
    if step_id == "W6.1":
        stop = shown.get("last_stop") or {}
        return [
            check(
                "the app stopped its core gracefully",
                stop.get("graceful") is True and stop.get("code") == 0,
                stop,
            ),
            check("the window's size was kept", shown.get("bounds_kept") is True),
            check(
                "no event was announced again after the restart",
                shown.get("notifications_after_restart") == [],
                shown.get("notifications_after_restart"),
            ),
        ]
    return [check("a known step", False, step_id)]


def ports_refused(urls: list[str]) -> str:
    """Nothing answers on any port a leg-A hub used while the MCP leg runs (review s3d-a F4)."""
    if not urls:
        return "not checked"
    notes = [port_refused(url) for url in urls]
    if any(n == "NOT STOPPED" for n in notes):
        return "NOT STOPPED"
    if all(n.startswith("confirmed") for n in notes):
        return f"confirmed ({len(urls)} hub ports refuse connections)"
    return "not checked"


def port_refused(url: str) -> str:
    """Nothing answers on a leg-A hub's port while the MCP leg runs."""
    try:
        port = int(url.rstrip("/").rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return "not checked"
    with socket.socket() as probe:
        probe.settimeout(1.0)
        refused = probe.connect_ex(("127.0.0.1", port)) != 0
    return "confirmed (the hub's port refuses connections)" if refused else "NOT STOPPED"


# ----- orchestration ---------------------------------------------------------------------------


def candidate() -> dict[str, Any]:
    """The tree the walk ran on (review s3d-a F5): Git's HEAD and the candidate tree digest."""
    import tree_manifest

    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=False)  # fmt: skip
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                           capture_output=True, text=True, check=False)  # fmt: skip
    return {
        "head": head.stdout.strip() or None,
        "dirty": bool(dirty.stdout.strip()),
        "tree_sha256": tree_manifest.tree_sha256(ROOT),
    }


def judge(doc: dict[str, Any], outcomes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Compare the two legs: stored outcomes, the scripted steps, each step's outcome and the
    normalised answer of every agent step."""
    equivalence: dict[str, Any] = {}
    failures: list[str] = []
    difference = first_difference(outcomes["desktop"], outcomes["mcp"])
    equivalence["stored outcomes"] = "equal" if difference is None else difference
    if difference is not None:
        failures.append(f"stored outcomes differ: {difference}")
    one = {s["id"]: s for s in doc["desktop"]["steps"]}
    two = {s["id"]: s for s in doc["mcp"]["steps"]}
    missing = [i for i in SCRIPT_IDS if i not in one or i not in two]
    equivalence["scripted steps present on both legs"] = (
        "all" if not missing else f"missing {missing}"
    )
    if missing:
        failures.append(f"steps missing: {missing}")
    both = [i for i in SCRIPT_IDS if i in one and i in two]
    differing = {
        i: (one[i]["outcome"], two[i]["outcome"])
        for i in both
        if one[i]["outcome"] != two[i]["outcome"]
    }
    equivalence["step outcomes (ok / error:code)"] = (
        "equal" if not differing else f"differ {differing}"
    )
    if differing:
        failures.append(f"step outcomes differ: {differing}")
    compared = [i for i in both if i in AGENT_IDS]
    unequal = [
        i for i in compared
        if one[i]["normalised_response_sha256"] != two[i]["normalised_response_sha256"]
    ]  # fmt: skip
    equivalence["normalised answers of every agent step"] = (
        f"equal for {len(compared)} steps" if not unequal else f"differ at {unequal}"
    )
    if unequal:
        failures.append(f"agent answers differ at {unequal}")
    return equivalence, failures


def transcript_markdown(doc: dict[str, Any]) -> str:
    lines = ["# C19-WALK transcript", "", doc["summary"], ""]
    app = doc.get("built_app")
    if app:
        lines += [f"Built app: `{app['exe']}` (sha256 `{app['exe_sha256'][:16]}`, app.asar "
                  f"`{app['app_asar_sha256'][:16]}`); hubs at {app.get('urls')}", ""]  # fmt: skip
    cand = doc.get("candidate") or {}
    lines += [f"Candidate: HEAD {cand.get('head')}, dirty {cand.get('dirty')}, tree "
              f"`{str(cand.get('tree_sha256'))[:16]}`", ""]  # fmt: skip
    for leg in ("desktop", "mcp"):
        data = doc.get(leg)
        if not data:
            continue
        lines += [f"## {leg} leg ({data['transport']}, store {data['store']})", ""]
        lines += ["| Step | Actor | Via | Operation | Outcome | Response digest (normalised) |",
                  "|---|---|---|---|---|---|"]  # fmt: skip
        for s in data["steps"]:
            lines.append(
                f"| {s['id']} | {s['actor']} | {s['via']} | {s['operation']} | {s['outcome']} | "
                f"`{s['normalised_response_sha256'][:16]}` |"
            )
        lines.append("")
        for s in data["steps"]:
            for a in s["store_assertions"]:
                lines.append(f"- {s['id']} [{'pass' if a['passed'] else 'FAIL'}] {a['claim']}")
        lines.append("")
    lines += ["## Equivalence", ""]
    lines += [f"- {key}: {value}" for key, value in doc["equivalence"].items()]
    if doc.get("failures"):
        lines += ["", "## Failures", ""] + [f"- {f}" for f in doc["failures"]]
    return "\n".join(lines) + "\n"


def run(out: Path | None, only: str | None, keep: bool, show: bool) -> int:
    out = (out or Path(tempfile.mkdtemp(prefix="prometheos-walk19-out-"))).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into a folder that is not empty: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prometheos-walk19-"))
    doc: dict[str, Any] = {
        "walk": "C19-WALK",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "script": STEPS,
        "limitations": [
            "The MCP leg's owner steps run through the service in the harness, a synthetic owner "
            "channel: the capture is the window's exact request at the hub's owner scope, the "
            "grant an owner decision (the hub's owner act, given in the harness) on the plan "
            "digest the owner view shows. The desktop leg's owner steps are the real app.",
            "The tray click is the tray icon's click event emitted in the app's own process (an "
            "OS tray click cannot be scripted); the hotkey is a real key press (SendInput).",
            "The agent is a scripted MCP client; real hosts are the external gates E01 and E02.",
            "On the desktop leg the agent's MCP server runs from the checkout's environment "
            "(.venv), as on the MCP leg; the app's own sidecar runs the bundled core, which the "
            "walk checks is the checkout's src file by file.",
        ],
        "candidate": candidate(),
    }
    failures: list[str] = []
    outcomes: dict[str, Any] = {}
    equivalence: dict[str, Any] = {}
    urls: list[str] = []
    try:
        if only in (None, "desktop"):
            store_a = Store(work / "store-a")
            rec_a = Recorder("A", "desktop", store_a, out / "evidence" / "desktop")
            try:
                built = desktop_leg(store_a, out, rec_a)
                doc["built_app"], urls = built, built["urls"]
            except RuntimeError as exc:
                rec_a.failures.append(f"desktop: {exc}")
            outcomes["desktop"] = snapshot(store_a)
            (out / "outcome-desktop.json").write_text(
                json.dumps(outcomes["desktop"], indent=2, sort_keys=True), encoding="utf-8"
            )
            doc["desktop"] = {
                "store": "A",
                "transport": "desktop app (owner in the window) + agent over MCP stdio",
                "steps": rec_a.ordered(),
            }
            failures += rec_a.failures
        if only in (None, "mcp"):
            if only is None:
                running = app_processes()
                refused = ports_refused(urls)
                equivalence["the desktop app was stopped during the MCP leg"] = (
                    f"{refused}; no process of the built app runs"
                    if not running
                    else f"NOT STOPPED: {running}"
                )
                if running or not refused.startswith("confirmed"):
                    failures.append("the desktop app still ran during the MCP leg")
            store_b = Store(work / "store-b")
            rec_b = Recorder("B", "mcp", store_b, out / "evidence" / "mcp")
            asyncio.run(mcp_leg(store_b, rec_b))
            outcomes["mcp"] = snapshot(store_b)
            (out / "outcome-mcp.json").write_text(
                json.dumps(outcomes["mcp"], indent=2, sort_keys=True), encoding="utf-8"
            )
            doc["mcp"] = {
                "store": "B",
                "transport": "MCP stdio (official SDK client), desktop app stopped",
                "steps": rec_b.ordered(),
            }
            failures += rec_b.failures
        if "desktop" in outcomes and "mcp" in outcomes:
            judged, problems = judge(doc, outcomes)
            equivalence = judged | equivalence
            failures += problems
        doc["equivalence"] = equivalence
        doc["failures"] = failures
        if failures:
            doc["summary"] = f"FAIL: {len(failures)} problem(s); see failures."
        elif only is None:
            doc["summary"] = ("PASS: every step's assertions held and the two stores reached "
                              "equivalent outcomes.")  # fmt: skip
        else:
            doc["summary"] = f"PASS: {only} leg only; assertions held. Equivalence was not checked."
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
    parser.add_argument("--out", type=Path, help="a new folder for the evidence (default: temp)")
    parser.add_argument("--only", choices=("desktop", "mcp"), help="run one leg (debugging)")
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
