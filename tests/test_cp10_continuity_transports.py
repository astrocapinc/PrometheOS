"""CP10.4 continuity through every transport (C10-PARITY, C10-CONTINUITY; S26).

The same S26 walk runs on three fresh stores: through the service, through MCP (official SDK
client; the owner's steps happen at the computer, as in the walk) and through the local HTTP API
(agent bearer token for the agent, the paired session for the owner). The normalised outcomes
(records, states, relations, flags, queue and authority results, with IDs and times normalised)
must be equal, denied requests included. The CLI is checked on the read operations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
from cp1_support import AGENT, OWNER, request
from cp10_continuity_support import Scenario
from hub_support import running_hub
from mcp import Client
from test_cp1_cli import Cli

from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server

VOLATILE = {
    "project_seq",
    "generated_at",
    "request_id",
    "rev",
    "seq",
    "created_seq",
    "since",
    "recorded_at",
    "last_reported_at",
    "registered_at",
    "silent_since",
    "captured_at",
    "expires_at",
    "created_at",
    "replayed",
    "approval_request_id",
    "plan_sha256",
    "owner_route",
    "due_at",
    "actor",
}  # fmt: skip


def strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


def stable(value: Any) -> Any:
    """Sort every list of objects by its JSON text so store-specific ID order cannot matter."""
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [stable(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True)) if items else items
    return value


# ----- the agent's three routes ------------------------------------------------------------


class ServiceAgent:
    name = "service"

    def __init__(self, s: Scenario) -> None:
        self.s = s

    def _run(self, fn: Any) -> Any:
        try:
            return fn()
        except PrometheosError as exc:
            return {"error": exc.to_response().model_dump(mode="json")["error"]}

    def apply(self, ops: list[dict[str, Any]]) -> Any:
        raw = request(self.s.pid, ops, self.s.env.seq())
        return self._run(lambda: self.s.svc.apply(self.s.pid, raw, AGENT))

    def register(self, title: str, rid: str) -> Any:
        return self._run(lambda: self.s.register(title, rid=rid))

    def report(self, run: str, state: str, receipts: list[str], rid: str) -> Any:
        return self._run(
            lambda: self.s.svc.execution_report(
                self.s.pid, AGENT, run, state=state, receipt_version_ids=receipts, request_id=rid
            )
        )

    def executions(self) -> Any:
        return self._run(lambda: self.s.svc.execution_list(self.s.pid, AGENT))

    def awaiting(self) -> Any:
        return self._run(lambda: self.s.svc.awaiting_list(self.s.pid, AGENT))

    def resolve(self, target: str, resolution: str, text: str, rid: str) -> Any:
        return self._run(
            lambda: self.s.svc.awaiting_resolve(self.s.pid, AGENT, target, resolution, text, rid)
        )

    def coverage(self) -> Any:
        return self._run(lambda: self.s.svc.artifact_coverage(self.s.pid, AGENT, limit=200))

    def now(self) -> Any:
        return self._run(lambda: self.s.svc.now(self.s.pid, AGENT))

    def links(self, record_id: str) -> Any:
        return self._run(lambda: self.s.svc.links(self.s.pid, AGENT, record_id))

    def owner_apply(self, ops: list[dict[str, Any]]) -> Any:
        return self.s.env.apply_protected(ops, caller=OWNER)

    def owner_resolve(self, target: str, resolution: str, text: str, rid: str) -> Any:
        from test_cp10_continuity import resolve_as_owner

        return resolve_as_owner(self.s, target, resolution, text, rid)


class McpAgent(ServiceAgent):
    name = "mcp"

    def _tool(self, name: str, args: dict[str, Any]) -> Any:
        async def run() -> Any:
            async with Client(build_server(self.s.svc, self.s.pid)) as client:
                return await client.call_tool(name, args)

        result = anyio.run(run)
        if result.is_error:
            text = result.content[0].text
            return json.loads(text[text.index("{") :])
        return result.structured_content

    def apply(self, ops: list[dict[str, Any]]) -> Any:
        raw = json.loads(request(self.s.pid, ops, self.s.env.seq()))
        return self._tool("apply", {"request": raw})

    def register(self, title: str, rid: str) -> Any:
        return self._tool(
            "execution_register",
            {
                "title": title,
                "host_label": "lab workstation",
                "reporter": "sim agent",
                "request_id": rid,
                "command_summary": "simulate --regime variable-demand",
                "working_root_alias": "research",
                "external_ref": "sweep-1",
                "expected_outputs": ["results/sweep.csv"],
            },
        )

    def report(self, run: str, state: str, receipts: list[str], rid: str) -> Any:
        return self._tool(
            "execution_report",
            {
                "execution_id": run,
                "state": state,
                "receipt_version_ids": receipts,
                "request_id": rid,
            },
        )

    def executions(self) -> Any:
        return self._tool("execution_list", {})

    def awaiting(self) -> Any:
        return self._tool("awaiting_interpretation", {})

    def resolve(self, target: str, resolution: str, text: str, rid: str) -> Any:
        return self._tool(
            "awaiting_resolve",
            {"target_id": target, "resolution": resolution, "text": text, "request_id": rid},
        )

    def coverage(self) -> Any:
        return self._tool("artifact_coverage", {"limit": 200})

    def now(self) -> Any:
        return self._tool("now_get", {})

    def links(self, record_id: str) -> Any:
        return self._tool("knowledge_links", {"record_id": record_id})


class HttpAgent(ServiceAgent):
    name = "http"

    def __init__(self, s: Scenario, hub: Any, token: str) -> None:
        super().__init__(s)
        self.hub = hub
        self.headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        reply = self.hub.request(
            method, path, body, cookie=None, csrf=None, origin=None, headers=self.headers
        )
        return reply.json()

    def apply(self, ops: list[dict[str, Any]]) -> Any:
        raw = json.loads(request(self.s.pid, ops, self.s.env.seq()))
        return self._call("POST", "/api/v1/transactions", raw)

    def register(self, title: str, rid: str) -> Any:
        return self._call(
            "POST",
            "/api/v1/executions",
            {
                "title": title,
                "host_label": "lab workstation",
                "reporter": "sim agent",
                "command_summary": "simulate --regime variable-demand",
                "working_root_alias": "research",
                "external_ref": "sweep-1",
                "expected_outputs": ["results/sweep.csv"],
                "request_id": rid,
            },
        )

    def report(self, run: str, state: str, receipts: list[str], rid: str) -> Any:
        return self._call(
            "POST",
            f"/api/v1/executions/{run}/report",
            {"state": state, "receipt_version_ids": receipts, "request_id": rid},
        )

    def executions(self) -> Any:
        return self._call("GET", "/api/v1/executions")

    def awaiting(self) -> Any:
        return self._call("GET", "/api/v1/awaiting-interpretation")

    def resolve(self, target: str, resolution: str, text: str, rid: str) -> Any:
        return self._call(
            "POST",
            f"/api/v1/awaiting-interpretation/{target}/resolve",
            {"resolution": resolution, "text": text, "request_id": rid},
        )

    def coverage(self) -> Any:
        return self._call("GET", "/api/v1/artifacts/coverage?limit=200")

    def now(self) -> Any:
        return self._call("GET", "/api/v1/now")

    def links(self, record_id: str) -> Any:
        return self._call("GET", f"/api/v1/knowledge/links/{record_id}")

    def owner_apply(self, ops: list[dict[str, Any]]) -> Any:
        raw = json.loads(request(self.s.pid, ops, self.s.env.seq()))
        preview = self.hub.request("POST", "/api/v1/decisions/preview", {"request": raw}).json()
        done = self.hub.request(
            "POST", "/api/v1/decisions/apply", {"request": raw, "digest": preview["digest"]}
        )
        assert done.status == 200, done.body
        return done.json()["result"]

    def owner_resolve(self, target: str, resolution: str, text: str, rid: str) -> Any:
        done = self.hub.request(
            "POST",
            f"/api/v1/awaiting-interpretation/{target}/resolve",
            {"resolution": resolution, "text": text, "request_id": rid},
        )
        assert done.status == 200, done.body
        return done.json()["result"]


# ----- the walk ---------------------------------------------------------------------------


def errors_of(value: Any) -> Any:
    """The authority outcome of a denied request: its code and the closed details' shape."""
    error = value["error"]
    return {"code": error["code"], "operation": error.get("details", {}).get("required_scope")}


def walk(s: Scenario, agent: ServiceAgent) -> dict[str, Any]:
    trace: dict[str, Any] = {}
    # K2: an agent proposes a correction and tries to apply it.
    apply_op, _correction = s.supersede_correction()
    denied = agent.apply([apply_op])
    trace["agent_apply"] = errors_of(denied)
    trace["flags_before"] = [r["record_id"] for r in s.flags()]
    # The owner applies it with a grant; the effect, flags and anchors share one commit.
    result = agent.owner_apply([apply_op])
    flagged = {row["record_id"]: row for row in s.flags()}
    trace["flags_after"] = {
        s.names.get(k, k): {
            "reason": s.normalise(v["reason"]),
            "same_commit": v["created_seq"] == result["project_seq"],
        }
        for k, v in flagged.items()
    }
    trace["anchors"] = {
        name: s.body(rid)["status"]
        for name, rid in (
            ("uses_assumption", s.anchor),
            ("uses_claim", s.anchor_claim),
            ("control", s.anchor_control),
        )
    }
    trace["superseded_by"] = s.normalise(s.body(s.drag)["superseded_by"])
    trace["links_claim_a"] = s.normalise(strip(agent.links(s.claim_a)["relies_on"]))
    # K3: a registered run completes with a receipt; the queue holds it and the bare receipt.
    registered = agent.register("Walk run", new_request_id())
    walked = s.name(registered["results"][0]["id"], "walk_run")
    receipt = s.receipt("Walk receipt")
    s.name(receipt["version_id"], "walk_receipt")
    agent.report(walked, "reported_running", [], new_request_id())
    agent.report(walked, "reported_completed", [receipt["version_id"]], new_request_id())
    trace["awaiting"] = stable(s.normalise(strip(agent.awaiting())))
    trace["executions"] = stable(s.normalise(strip(agent.executions())))
    trace["now"] = stable(s.normalise(strip(agent.now())))
    # The agent cannot dismiss; the owner can, with a reason.
    trace["agent_resolve"] = errors_of(
        agent.resolve(s.bare_receipt["version_id"], "dismissed", "Not needed.", new_request_id())
    )
    reason = "Superseded by the walk run; nothing in it is used."
    done = agent.owner_resolve(s.bare_receipt["version_id"], "dismissed", reason, new_request_id())
    entry = s.body(done["results"][0]["id"])
    trace["dismissal"] = {k: entry[k] for k in ("review", "category", "text", "origin")}
    trace["awaiting_after"] = stable(s.normalise(strip(agent.awaiting())))
    # Artifacts: the reference-only file is removed.
    trace["coverage_before"] = stable(s.normalise(strip(agent.coverage())))
    (s.env.research / "sub" / "notes.txt").unlink()
    trace["coverage_after"] = stable(s.normalise(strip(agent.coverage())))
    return trace


def trace_for(tmp: Path, kind: str) -> dict[str, Any]:
    tmp.mkdir(parents=True)
    s = Scenario(tmp).build()
    if kind == "service":
        return walk(s, ServiceAgent(s))
    if kind == "mcp":
        return walk(s, McpAgent(s))
    with running_hub(s.svc, s.pid) as hub:
        hub.pair()
        return walk(s, HttpAgent(s, hub, hub.gate.new_agent_token()))


def test_s26_walk_is_equivalent_through_service_mcp_and_http(tmp_path: Path) -> None:
    traces = {kind: trace_for(tmp_path / kind, kind) for kind in ("service", "mcp", "http")}
    reference = traces["service"]
    # What the walk established, once, on the reference route.
    assert reference["agent_apply"]["code"] == "approval_required"
    assert reference["agent_resolve"]["code"] == "approval_required"
    assert reference["flags_before"] == []
    assert set(reference["flags_after"]) == {
        "claim_a", "action", "anchor_uses_assumption", "anchor_uses_claim"
    }  # fmt: skip
    assert all(v["same_commit"] for v in reference["flags_after"].values())
    assert reference["anchors"] == {
        "uses_assumption": "needs_review",
        "uses_claim": "needs_review",
        "control": "current",
    }
    assert {i["target_kind"] for i in reference["awaiting"]["items"]} == {"execution", "receipt"}
    assert [i["target_kind"] for i in reference["awaiting_after"]["items"]] == ["execution"]
    assert reference["coverage_before"]["items"] and reference["coverage_after"]["items"]
    statuses = {
        (i["title"], i["availability"], i["backup"]["status"])
        for i in reference["coverage_after"]["items"]
        if i["storage"] == "external"
    }
    assert statuses == {("notes.txt", "unavailable", "not_backed_up")}
    for kind in ("mcp", "http"):
        for key, value in reference.items():
            assert traces[kind][key] == value, f"{kind} differs from the service at {key}"


# ----- the CLI ----------------------------------------------------------------------------


def test_cli_registers_reports_lists_and_shows_now(tmp_path: Path) -> None:
    s = Scenario(tmp_path).build()
    cli = Cli(tmp_path)
    base = ("--project", s.pid)
    rid = new_request_id()
    code, out, _ = cli(
        "execution", "register", "--title", "CLI run", "--host", "box", "--reporter", "cli agent",
        "--root", "research", "--expected-output", "out.csv", "--request-id", rid, *base,
    )  # fmt: skip
    assert code == 0 and out["replayed"] is False, out
    run = out["results"][0]["id"]
    code, again, _ = cli(
        "execution", "register", "--title", "CLI run", "--host", "box", "--reporter", "cli agent",
        "--root", "research", "--expected-output", "out.csv", "--request-id", rid, *base,
    )  # fmt: skip
    assert code == 0 and again["replayed"] is True and again["results"] == out["results"]
    code, out, _ = cli("execution", "report", run, "--state", "reported_completed", *base)
    assert code == 0, out
    code, listed, _ = cli("execution", "list", "--awaiting", *base)
    assert code == 0 and [e["id"] for e in listed["executions"]] == [run]
    code, queue, _ = cli("execution", "awaiting", *base)
    assert code == 0 and {i["target_id"] for i in queue["items"]} >= {run}
    # Resolving without an owner decision is approval_required (exit 4); nothing leaves.
    code, denied, _ = cli(
        "execution", "resolve", run, "--resolution", "dismissed", "--text", "not needed", *base
    )
    assert code == 4 and denied["error"]["code"] == "approval_required"
    code, view, _ = cli("now", *base)
    assert code == 0
    assert run in [i["id"] for i in view["awaiting_interpretation"]["items"]]
    assert len(view["approvals"]["items"]) == 1
    code, coverage, _ = cli("artifact", "coverage", *base)
    assert code == 0 and coverage["totals"]["reference_only"] == 1
    code, bad, _ = cli("execution", "report", run, "--state", "registered", *base)
    assert code != 0 and bad["error"]["code"] == "validation_error"
