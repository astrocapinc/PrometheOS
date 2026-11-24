"""C22-WALK: the CP22 walk of the intelligence layer (spec/10 CP22.6; S71, S73; decisions 0099,
0100).

    uv run python tools/walk_cp22.py [--out DIR] [--print-transcript] [--only hub|mcp] [--keep]

The walk makes no model call: the provider transport replays recorded reply bodies
(``intelligence/replay.py``, behind ``PROMETHEOS_TEST_HOOKS=1``), the key is a fake value in the
process environment under the reference ``walk-key`` (read from the environment only, never the
OS credential store), and replayed replies are stored as recorded replies that confer no trust.

Each store is fresh: CP17's seeded corpus (``tools/adoption_corpus.py``, seed 17, with its golden
labels) is the ``research`` root, its ``sub`` folder is a second root ``interviews`` (left
restricted), and the owner has one open work item (deciding the loss table). The task is the
family assist's status statements (``k = 2``: two samples from two evaluated models must agree).

1. **Hub leg (store A).** ``prometheos start`` serves the home. A Playwright spec (the headless
   shell in the loopback-only boundary admitting the home's host name) does the owner's steps in
   the hub: with no key, the AI page and the adoption page each say the layer is off and what
   runs instead (K1); later the owner sees the proposals tagged on the adoption page and uses one
   as evidence on the AI page (K7), then revokes the policy (K8). The owner's terminal steps (K3:
   the provider configuration, lowering ``research`` to normal, granting a recorded policy with
   two evaluated models that lets agents run) go through the service as the CLI's owner act.
   Between them an agent session over the real ``prometheos mcp serve --stdio`` (official MCP SDK
   client):
   - K2 runs the task with no provider configured: ``capability_unavailable``, saying what runs
     instead (the rules' result and the coding agent's path), no call;
   - K4 runs it inside the policy on three files: each normal note gets two replayed samples from
     two models that agree, tagged as recorded replies (no trust); the file under the restricted
     root is denied and never sent (no receipt names it or carries its bundle's digest); the
     owner's work item stays open;
   - K5 asks to lower ``interviews`` (S73's widening): ``approval_required``;
   - K6 tries to revoke the policy: ``policy_denied``;
   - K9 after the owner's revocation runs again: outside the policy the one-model per-call route
     cannot do a two-model task, so ``capability_unavailable`` saying what to do instead, no call.
2. **MCP leg (store B).** The same script with the owner's hub steps through the service (the hub
   owner's act), every agent step the same MCP call.
3. After K7 the accepted statements match the corpus's golden labels for the loss table (v1 and
   v2 superseded, v3 given no status), the family is still undecided and the work item still
   open. The normalised stored outcomes (results, policy revisions, root sensitivities, call
   receipts, approval requests, accepted evidence, families, decisions, work items), every step's
   outcome and every agent answer must be equal.
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
from walk_cp10 import boundary_evidence, check, digest, first_difference  # noqa: E402
from walk_cp18 import McpChat, outcome_of  # noqa: E402

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
KEY_REF = "walk-key"
SEED = 17  # CP17's seeded corpus
TASK = "adoption.extract_status_statements"
LOSS, STATUS, PRIVATE = "notes/loss-notes.md", "notes/status-old.md", "sub/notes.txt"
FILES = [LOSS, STATUS, PRIVATE]
NORMAL = FILES[:2]
# The statements each note makes, as the corpus's golden labels have them (loss_table:
# v1 replaced by v2, v2 superseded by v3, v3 "not superseded" gives it no status; status-old
# claims the FINAL file is of record, which the contradiction trap keeps from deciding anything).
STATEMENTS = {
    LOSS: [("data/loss_v1.csv was replaced by data/loss_v2.csv", "data/loss_v1.csv", "superseded"),
           ("data/loss_v2.csv is superseded by data/loss_v3.csv", "data/loss_v2.csv",
            "superseded")],
    STATUS: [("results/sweep_FINAL.mat is the final result of record", "results/sweep_FINAL.mat",
              "current")],
}  # fmt: skip
WORK_ITEM = "Decide which loss table is current"
PAIR = {"model": "mistralai/mistral-small-4", "provider": "mistral", "company": "mistralai",
        "supported_parameters": ["temperature", "seed"], "lowest_effort": None,
        "price_input_per_mtok_usd": "0.15", "price_output_per_mtok_usd": "0.60",
        "metadata_as_of": "2026-10-30"}  # fmt: skip
SECOND = {"model": "google/gemini-3.1-flash-lite", "provider": "google-vertex",
          "company": "google", "supported_parameters": ["temperature", "seed"],
          "lowest_effort": None, "price_input_per_mtok_usd": "0.10",
          "price_output_per_mtok_usd": "0.40", "metadata_as_of": "2026-10-30"}  # fmt: skip
# The serving provider's name as OpenRouter reports it; it must match each pin (in the list).
SERVED = {PAIR["model"]: "Mistral", SECOND["model"]: "Google Vertex"}
POLICY = {"tasks": [{"task_kind": TASK, "models": [PAIR, SECOND],
                     "single_company_reason": None, "task_ceiling_usd": "0.02"}],
          "sensitivity_ceiling": "normal", "data_policy": "zdr_deny_collection",
          "run_cap_usd": "0.10", "monthly_cap_usd": "1", "agents_may_run": True}  # fmt: skip

STEPS: list[dict[str, Any]] = [
    {"id": "K1", "actor": "owner", "operation": "open the AI page and adoption: the layer is off "
     "and each says what runs instead", "service": "read the policy state"},
    {"id": "K2", "actor": "agent", "operation": "run the family assist with no provider"},
    {"id": "K3", "actor": "owner", "operation": "configure the provider, lower research to "
     "normal and grant a recorded policy (the terminal's owner acts)"},
    {"id": "K4", "actor": "agent", "operation": "run the family assist inside the policy"},
    {"id": "K5", "actor": "agent", "operation": "ask to lower the restricted root (widen)"},
    {"id": "K6", "actor": "agent", "operation": "try to revoke the policy"},
    {"id": "K7", "actor": "owner", "operation": "see the tagged proposals in adoption and use one "
     "as evidence in the hub",
     "service": "accept the proposal (the hub owner's act)"},
    {"id": "K8", "actor": "owner", "operation": "revoke the policy in the hub",
     "service": "revoke the policy (the hub owner's act)"},
    {"id": "K9", "actor": "agent", "operation": "run again after the revocation"},
]  # fmt: skip
SCRIPT_IDS = [s["id"] for s in STEPS]
AGENT_IDS = [s["id"] for s in STEPS if s["actor"] == "agent"]
BROWSER_IDS = ["K1", "K7", "K8"]


# ----- the store -----------------------------------------------------------------------------


class Store:
    def __init__(self, work: Path) -> None:
        from prometheos.app.reads import Caller
        from prometheos.app.service import Service

        self.work = work
        self.home = work / "home"
        self.research = work / "research"
        self.golden = generate(self.research, SEED)
        (self.research / "sub").mkdir(parents=True)
        (self.research / PRIVATE).write_text(
            "Interview notes: the participant's own words. data/loss_v3.csv is superseded.\n",
            "utf-8",
        )
        self.svc = Service(home=self.home, actor="cli")
        self.owner = Caller(actor="hub-owner", scope="owner")
        made = self.svc.init_project(
            title="Synthetic study (C22-WALK)",
            objective="Show the intelligence layer with no key and on recorded replies.",
            timezone="UTC",
            roots={"research": str(self.research), "interviews": str(self.research / "sub")},
        )
        self.pid: str = made["project_id"]
        self.store_dir = Path(made["store"])
        self.scan_gen = 0
        self.work_item = ""

    def owner_add_work_item(self) -> None:
        """The owner's own open work item (the hub's add: create and adopt in one reviewed request,
        applied with a synthetic owner decision bound to its digest, as C18-WALK does)."""
        from cp1_support import request

        from prometheos.domain.ids import new_id

        action_id = new_id("action")
        body = {"title": WORK_ITEM, "reason": "Added by the researcher in the hub",
                "due_at": None, "description": "Read the notes and settle data/loss_v*.csv.",
                "labels": ["adoption"], "source_refs": [], "owner_ref": None, "dependencies": [],
                "status": "proposed", "blocker": None, "completion_evidence": [],
                "commitment_item_id": None}  # fmt: skip
        ops = [
            {"op": "create", "kind": "action", "id": action_id, "base_rev": 0,
             "sensitivity": "normal", "occurred_at": None, "body": body},
            {"op": "transition", "id": action_id, "base_rev": 1, "to": "ready",
             "reason": "Added by the researcher in the hub", "evidence": []},
        ]  # fmt: skip
        seq = self.svc.project_info(self.pid, self.owner)["project_seq"]
        raw = request(self.pid, ops, seq)
        preview = self.svc.review_request(self.pid, raw, self.owner)
        grant = self.svc.grant(self.pid, operation="exact_request", digest=preview["digest"],
                               ttl_seconds=60, max_uses=1,
                               label="SYNTHETIC-TEST owner decision (C22-WALK)",
                               synthetic=True)  # fmt: skip
        self.svc.apply(self.pid, raw, self.owner, grant.secret)
        self.work_item = action_id

    def work_items(self) -> list[tuple[str, str]]:
        items = self.svc.list_records(self.pid, self.owner, kind="action", limit=200)["items"]
        return sorted((r["body"]["title"], r["body"]["status"]) for r in items)

    def item_open(self) -> bool:
        got = self.svc.get(self.pid, self.owner, self.work_item, None)
        body = got["body"]
        return bool(body["status"] == "ready" and not body["completion_evidence"])

    def decided(self) -> list[str]:
        return sorted(str(r["family_id"]) for r in self.rows("SELECT * FROM adoption_decisions"))

    def rows(self, sql: str) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.store_dir / "record.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def calls(self) -> int:
        return len(self.rows("SELECT id FROM provider_calls"))


def snapshot(st: Store) -> dict[str, Any]:
    """What the two legs must agree on (ids, digests, times and actors normalised)."""
    return {
        "results": sorted(
            (
                r["task_kind"],
                r["subject"],
                r["sample"],
                r["executor"],
                r["outcome"],
                json.loads(r["reasons"]),
                bool(r["hint_only"]),
                r["served_model"],
            )
            for r in st.rows("SELECT * FROM ai_results")
        ),
        "policy": [
            (r["state"], r["provider_revision"])
            for r in st.rows("SELECT state, provider_revision FROM ai_policy ORDER BY revision")
        ],
        "roots": sorted(
            (r["root_alias"], r["sensitivity"])
            for r in st.rows("SELECT root_alias, sensitivity FROM root_sensitivity")
        ),
        "calls": sorted(
            (r["purpose"], r["state"], r["model"], r["served_model"])
            for r in st.rows("SELECT purpose, state, model, served_model FROM provider_calls")
        ),
        "approvals": sorted(
            (r["operation"], r["state"])
            for r in st.rows("SELECT operation, state FROM approval_requests")
        ),
        "accepted_evidence": sorted(
            (
                r["task_kind"],
                json.loads(r["response"])["subject"],
                json.loads(r["validation"])["accepted"],
            )
            for r in st.rows("SELECT * FROM adoption_assist")
        ),
        "families": len(st.rows("SELECT * FROM adoption_families")),
        "decisions": len(st.decided()),
        "work_items": st.work_items(),
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


def normalise(value: Any) -> Any:
    """Answers without run ids, times, digests or request ids (they differ between stores)."""
    volatile = ("run_id", "approval_request_id", "expires_at", "plan_sha256", "input_sha256",
                "samples", "call_id", "owner_route", "provider_revision", "result_key",
                "inputs", "created_at", "granted_at", "granted_by")  # fmt: skip
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in sorted(value.items()) if k not in volatile}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    if isinstance(value, str):  # ids inside messages ("approval request apreq_…")
        return re.sub(r"\b(apreq|mapreq|airun|oprun)_[0-9a-f-]{8,}", r"\1_<id>", value)
    return value


# ----- the replayed replies and the owner's terminal acts --------------------------------------


def statements_of(bundle: Any, path: str) -> dict[str, Any]:
    """The note's golden statements in the task's wire form, each quote on its own line."""
    lines = {int(x["n"]): str(x["text"]) for x in bundle.input["lines"]}
    out = []
    for quote, referent, status in STATEMENTS[path]:
        line = next(n for n, text in lines.items() if quote in text)
        out.append({"quote": quote, "line": line, "referent": referent, "status": status,
                    "date": None})  # fmt: skip
    return {"result": {"statements": out}, "abstain": None}


def record_replies(st: Store, folder: Path) -> None:
    """For each normal note and each evaluated model one recorded reply, keyed by the bundle's
    digest and the model (decisions 0099, 0100); the restricted file has none."""
    from prometheos.adoption import assist
    from prometheos.store.project import ProjectStore

    folder.mkdir(parents=True, exist_ok=True)
    conn = ProjectStore(st.store_dir).connect()
    try:
        for path in NORMAL:
            bundle = assist._bundle(conn, st.research, st.scan_gen, "extract_status_statements",
                                    path)  # fmt: skip
            content = json.dumps(statements_of(bundle, path))
            for model, provider in SERVED.items():
                body = {"id": f"gen-{path}-{provider}", "model": model, "provider": provider,
                        "usage": {"prompt_tokens": 400, "completion_tokens": 60, "cost": 0.00004},
                        "choices": [{"message": {"content": content},
                                     "finish_reason": "stop"}]}  # fmt: skip
                name = f"{path.replace('/', '_')}-{provider.replace(' ', '_')}.json"
                (folder / name).write_text(json.dumps(
                    {"input_sha256": bundle.input_sha256, "model": model, "body": body}
                ), encoding="utf-8")  # fmt: skip
    finally:
        conn.close()


def owner_terminal_acts(st: Store) -> dict[str, Any]:
    """K3: what the owner does at the terminal (the CLI's owner act), the same on both legs."""
    from prometheos.app.authority import OwnerAct
    from prometheos.providers import config as provider_config
    from prometheos.store.project import ProjectStore

    act = OwnerAct(channel="cli-interactive", actor="cli-owner")
    store = ProjectStore(st.store_dir)
    conn = store.connect()
    try:
        _, revision = provider_config.load(conn)
    finally:
        conn.close()
    config = provider_config.ProviderConfig.model_validate({
        "policy": "remote-allowed", "endpoint": "https://openrouter.ai/api/v1",
        "model": PAIR["model"], "credential_ref": KEY_REF, "price_input_per_mtok_usd": "0.15",
        "price_output_per_mtok_usd": "0.60",
        "budget": {"max_input_chars": 60000, "max_output_tokens": 2048, "max_calls_per_run": 2,
                   "spend_cap_usd": None, "timeout_seconds": 60}})  # fmt: skip
    provider_config.save(store, config, revision)
    lowered = st.svc.ai_root_sensitivity_set(st.pid, "research", "normal", authority=act)
    binding = st.svc.ai_policy_preview(st.pid, POLICY)["binding_sha256"]
    granted = st.svc.ai_policy_grant(st.pid, POLICY, binding, authority=act)
    return {"lowered": lowered, "granted": granted}


# ----- the agent's steps (both legs) ------------------------------------------------------------


def _bundle_digest(st: Store, path: str) -> str:
    from prometheos.adoption import assist
    from prometheos.store.project import ProjectStore

    conn = ProjectStore(st.store_dir).connect()
    try:
        return str(assist._bundle(conn, st.research, st.scan_gen, "extract_status_statements",
                                  path).input_sha256)  # fmt: skip
    finally:
        conn.close()


async def agent_step(st: Store, rec: Recorder, chat: McpChat, step_id: str, tool: str,
                     args: dict[str, Any], expect: str,
                     extra: list[dict[str, Any]] | None = None,
                     says: tuple[str, ...] = ()) -> Any:  # fmt: skip
    calls_before = st.calls()
    ok, answer = await chat.call(tool, args)
    outcome = outcome_of(ok, answer)
    checks = [check(f"the agent's answer is {expect}", outcome == expect, outcome)]
    for words in says:
        checks.append(
            check(f"it says what to do or what runs instead ({words})", words in json.dumps(answer))
        )
    if expect != "ok":
        checks.append(check("nothing was sent", st.calls() == calls_before, st.calls()))
    rec.add(step_id, "mcp", answer, outcome, checks + (extra or []))
    return answer


async def agent_k2(st: Store, rec: Recorder, chat: McpChat) -> None:
    await agent_step(st, rec, chat, "K2", "intelligence_manage",
                     {"request": {"op": "run", "task_kind": TASK, "scan_gen": st.scan_gen,
                                  "paths": NORMAL}}, "error:capability_unavailable",
                     says=("configure the provider", "rules' own result",
                           "coding agent"))  # fmt: skip


async def agent_k4_to_k6(st: Store, rec: Recorder, chat: McpChat) -> None:
    ok, answer = await chat.call("intelligence_manage", {"request": {
        "op": "run", "task_kind": TASK, "scan_gen": st.scan_gen, "paths": FILES}})  # fmt: skip
    result = answer.get("result", answer) if ok else {}
    items = {i["subject"]: i for i in result.get("items", [])}
    rows = st.rows("SELECT subject, executor, model FROM ai_results")
    calls = st.rows("SELECT model, bundle_digest, input_refs FROM provider_calls")
    denied_digest = _bundle_digest(st, PRIVATE)
    rec.add("K4", "mcp", answer, outcome_of(ok, answer), [
        check("both normal notes got proposals", [items.get(f, {}).get("outcome")
              for f in NORMAL] == ["accepted", "accepted"], list(items)),
        check("each from two samples of two different models",
              all(sorted(r["model"] for r in rows if r["subject"] == f) ==
                  sorted(SERVED) for f in NORMAL) and len(rows) == 4, len(rows)),
        check("each sample and its provenance is tagged a recorded reply (no trust)",
              all(r["executor"] == "cassette" for r in rows)
              and all(p["executor"] == "cassette" for f in NORMAL if f in items
                      for p in items[f].get("provenance", [])), len(rows)),
        check("the file under the restricted root was denied",
              items.get(PRIVATE, {}).get("outcome") == "denied"),
        check("its text was never sent: no receipt names it or carries its bundle's digest",
              all(PRIVATE not in str(c["input_refs"]) and c["bundle_digest"] != denied_digest
                  for c in calls)),
        check("every call has a receipt with its bundle's digest, one per sample",
              all(c["bundle_digest"] for c in calls) and len(calls) == 4, len(calls)),
        check("no proposal closed the owner's work item", st.item_open()),
    ])  # fmt: skip
    await agent_step(st, rec, chat, "K5", "intelligence_manage",
                     {"request": {"op": "root_sensitivity", "root_alias": "interviews",
                                  "sensitivity": "normal"}}, "error:approval_required")  # fmt: skip
    await agent_step(st, rec, chat, "K6", "intelligence_manage",
                     {"request": {"op": "policy_revoke"}}, "error:policy_denied",
                     [check("the policy is still in force",
                            st.svc.ai_policy_show(st.pid)["in_force"] is True)])  # fmt: skip


async def agent_k9(st: Store, rec: Recorder, chat: McpChat) -> None:
    await agent_step(st, rec, chat, "K9", "intelligence_manage",
                     {"request": {"op": "run", "task_kind": TASK, "scan_gen": st.scan_gen,
                                  "paths": NORMAL[:1]}}, "error:capability_unavailable",
                     says=("2 different models", "coding agent"))  # fmt: skip


# ----- the owner's hub steps on the store's side ----------------------------------------------


def k1_checks(st: Store) -> list[dict[str, Any]]:
    shown = st.svc.ai_policy_show(st.pid)
    return [check("no policy is in force", shown["in_force"] is False and shown["policy"] is None),
            check("the owner's work item is open", st.item_open()),
            check("both roots read restricted", {r["root_alias"]: r["sensitivity"] for r in
                  st.svc.ai_root_sensitivity_show(st.pid)["roots"]} ==
                  {"research": "restricted", "interviews": "restricted"})]  # fmt: skip


def k7_checks(st: Store) -> list[dict[str, Any]]:
    rows = [json.loads(r[0]) for r in st.rows("SELECT response FROM adoption_assist")]
    golden = {(referent, status) for _, referent, status in STATEMENTS[LOSS]}
    loss = next(f for f in st.golden["families"] if f["id"] == "loss_table")
    accepted = {(x["referent"], x["status"]) for r in rows if r["subject"] == LOSS
                for x in r["output"]["statements"]}  # fmt: skip
    stated = {referent for referent, _ in accepted}
    return [
        check("the proposal entered the assist path as two samples of the owner's chosen note",
              len(rows) == 2 and {r["subject"] for r in rows} == {LOSS}, len(rows)),
        check("its statements are the golden labels: v1 and v2 superseded", accepted == golden,
              sorted(accepted)),
        check("the current loss table is given no status (the corpus's v3)",
              loss["current"] not in stated, loss["current"]),
        check("no family was decided by it", st.decided() == [], st.decided()),
        check("and the owner's work item is still open", st.item_open()),
    ]  # fmt: skip


def k8_checks(st: Store) -> list[dict[str, Any]]:
    return [check("the policy is revoked", [r["state"] for r in st.rows(
        "SELECT state FROM ai_policy")] == ["revoked"])]  # fmt: skip


def k3_checks(st: Store, done: dict[str, Any]) -> list[dict[str, Any]]:
    return [check("research is normal and interviews still restricted",
                  {r["root_alias"]: r["sensitivity"] for r in
                   st.svc.ai_root_sensitivity_show(st.pid)["roots"]} ==
                  {"research": "normal", "interviews": "restricted"}),
            check("the recorded policy is in force",
                  st.svc.ai_policy_show(st.pid)["in_force"])]  # fmt: skip


# ----- the MCP leg -----------------------------------------------------------------------------


async def mcp_leg(st: Store, rec: Recorder) -> None:
    from prometheos.app.authority import OwnerAct

    hub_owner = OwnerAct(channel="hub-owner", actor="hub-owner")
    rec.add("K1", "owner-service", st.svc.ai_policy_show(st.pid), "ok", k1_checks(st))
    async with McpChat(st) as chat:  # type: ignore[arg-type]
        await agent_k2(st, rec, chat)
    done = owner_terminal_acts(st)
    rec.add("K3", "owner-service", done, "ok", k3_checks(st, done))
    async with McpChat(st) as chat:  # type: ignore[arg-type]
        await agent_k4_to_k6(st, rec, chat)
    accepted = st.svc.ai_accept(st.pid, TASK, st.scan_gen, LOSS, authority=hub_owner)
    rec.add("K7", "owner-service", accepted, "ok", k7_checks(st))
    revoked = st.svc.ai_policy_revoke(st.pid, authority=hub_owner)
    rec.add("K8", "owner-service", revoked, "ok", k8_checks(st))
    async with McpChat(st) as chat:  # type: ignore[arg-type]
        await agent_k9(st, rec, chat)


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


def browser_checks(step_id: str, shown: dict[str, Any]) -> list[dict[str, Any]]:
    if not shown:
        return [check("the browser spec recorded this step", False)]
    if step_id == "K1":
        return [check("the page says the layer is off", shown.get("off") is True),
                check("and that tasks run their rules or the agent's path instead",
                      shown.get("says_instead") is True),
                check("both roots show restricted",
                      shown.get("restricted_roots") == 2),
                check("adoption says the layer is off and what runs instead",
                      shown.get("adoption_off") is True
                      and shown.get("adoption_instead") is True)]  # fmt: skip
    if step_id == "K7":
        return [check("adoption shows the proposals tagged as recorded replies",
                      shown.get("adoption_tagged") is True),
                check("the AI page tags the clicked note's proposal as a recorded reply",
                      shown.get("tagged") is True),
                check("the hub confirmed the evidence", shown.get("used") is True)]  # fmt: skip
    if step_id == "K8":
        return [check("the page says the layer is off again", shown.get("off") is True)]
    return []


async def hub_story(st: Store, rec: Recorder, leg: Leg, owner: dict[str, Any]) -> None:
    await leg.wait("ready", 300)
    leg.send("open-off")
    await leg.wait("seen-off", 120)
    owner["K1"] = k1_checks(st)
    async with McpChat(st) as chat:  # type: ignore[arg-type]
        await agent_k2(st, rec, chat)
    done = owner_terminal_acts(st)
    rec.add("K3", "owner-service", done, "ok", k3_checks(st, done))
    async with McpChat(st) as chat:  # type: ignore[arg-type]
        await agent_k4_to_k6(st, rec, chat)
    leg.send("use-evidence")
    await leg.wait("used", 120)
    owner["K7"] = k7_checks(st)
    leg.send("revoke")
    await leg.wait("revoked", 120)
    owner["K8"] = k8_checks(st)
    async with McpChat(st) as chat:  # type: ignore[arg-type]
        await agent_k9(st, rec, chat)
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
              "WALK_PW_OUT": tempfile.mkdtemp(prefix="prometheos-walk22-pw-"),
              "BOUNDARY_EVIDENCE": str(out / "browser-boundary.json"),
              "PLAYWRIGHT_JSON_OUTPUT_FILE": str(out / "playwright-report.json"),
              "PROMETHEOS_BOUNDARY_HOME_HOST": host}  # fmt: skip
    owner: dict[str, list[dict[str, Any]]] = {}
    code_exit = None
    try:
        with open(out / "playwright.log", "w", encoding="utf-8") as log:
            browser = subprocess.Popen(
                [*playwright, "-c", "playwright.walk22.config.ts", "--reporter=line,json"],
                cwd=ROOT / "hub", env=pw_env, stdout=log, stderr=subprocess.STDOUT, text=True,
            )  # fmt: skip
            try:
                asyncio.run(hub_story(st, rec, Leg(signals, browser), owner))
                code_exit = browser.wait(timeout=300)
            except Exception as exc:
                rec.failures.append(f"hub: {exc}")
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
    for step_id in BROWSER_IDS:
        asserted = owner.get(step_id, []) + browser_checks(step_id, seen.get(step_id, {}))
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


def prepare_environment(work: Path) -> Path:
    """The test switch, the replay folder and the fake key, for this process and its children
    (the hub, the MCP servers). Never the OS credential store: a reference no one else uses."""
    replies = work / "replies"
    replies.mkdir(parents=True, exist_ok=True)
    os.environ["PROMETHEOS_TEST_HOOKS"] = "1"
    os.environ["PROMETHEOS_TEST_REPLAY"] = str(replies)
    os.environ["PROMETHEOS_SECRET_WALK_KEY"] = "sk-or-walk-not-a-real-key"  # noqa: S105 (fake)
    return replies


def run(out: Path | None, only: str | None, keep: bool, show: bool) -> int:
    out = (out or Path(tempfile.mkdtemp(prefix="prometheos-walk22-out-"))).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into a folder that is not empty: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="prometheos-walk22-"))
    replies = prepare_environment(work)
    doc: dict[str, Any] = {
        "walk": "C22-WALK",
        "script": STEPS,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus": {"generator": "adoption-corpus", "seed": SEED},
    }
    failures: list[str] = []
    outcomes: dict[str, Any] = {}
    try:
        for leg in ("hub", "mcp"):
            if only not in (None, leg):
                continue
            st = Store(work / f"store-{leg}")
            st.scan_gen = int(st.svc.adoption_scan(st.pid, st.owner, root_alias="research",
                                                   mode="wait")["scan_gen"])  # fmt: skip
            record_replies(st, replies)
            st.owner_add_work_item()
            rec = Recorder(leg, out / "evidence" / leg)
            if leg == "hub":
                try:
                    doc["hub_leg"] = hub_leg(st, out, rec)
                except RuntimeError as exc:
                    rec.failures.append(f"hub: {exc}")
            else:
                asyncio.run(mcp_leg(st, rec))
            outcomes[leg] = snapshot(st)
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
        (out / "transcript.json").write_text(json.dumps(doc, indent=2, default=str),
                                             encoding="utf-8")  # fmt: skip
        lines = ["# C22-WALK transcript", "", doc["summary"], ""]
        for leg in ("hub", "mcp"):
            for s in (doc.get(leg) or {}).get("steps", []):
                lines.append(f"- {leg} {s['id']} [{s['outcome']}] {s['operation']}")
                lines += [f"  - [{'pass' if a['passed'] else 'FAIL'}] {a['claim']}"
                          for a in s["store_assertions"]]  # fmt: skip
        lines += ["", *[f"- FAIL {f}" for f in failures]]
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
