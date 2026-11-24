"""CP10.2 untrusted-content boundary (R25, C10-AUTH, S24; decision 0016).

Imported, retrieved and generated text (a PDF, an abstract, a web page, a transcript, model
output) is data. Its text cannot change workflow instructions, invoke tools, widen egress or
create authority. The hostile fixtures in ``tests/fixtures/hostile`` say "approve", "call the
apply tool", "set the provider route", "send the project out"; these tests process them through
the service, MCP and HTTP and prove that nothing changes beyond their role as data, and that
context and handoff outputs label them as untrusted data.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env
from hub_support import running_hub
from mcp import Client

from prometheos.app.context import CONTENT_TRUST, UNTRUSTED_NOTICE
from prometheos.app.tasks import DATA_NOTICE
from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server

HOSTILE = Path(__file__).resolve().parent / "fixtures" / "hostile"
FILES = ("paper.pdf", "abstract.bib", "page.html", "transcript.txt")
PLANTED_ID = "apreq_00000000-0000-4000-8000-0000000000ff"


class Tripwire:
    """Every way authority or egress could change, read from the store itself."""

    def __init__(self, env: Env) -> None:
        self.env = env
        self.before = self.state()

    def state(self) -> dict[str, Any]:
        with closing(sqlite3.connect(self.env.store_dir / "record.sqlite")) as conn:
            approvals = conn.execute("SELECT id, state FROM approval_requests").fetchall()
            caps = conn.execute("SELECT id, uses, revoked_at FROM capabilities").fetchall()
            protected = conn.execute(
                "SELECT COUNT(*) FROM audit WHERE operation LIKE 'owner_decision:%' "
                "OR operation LIKE 'owner_local_action:%'"
            ).fetchone()[0]
            adopted = conn.execute(
                "SELECT COUNT(*) FROM records WHERE kind IN ('action','claim','correction')"
            ).fetchone()[0]
        status = self.env.svc.provider_status(self.env.project_id, AGENT)
        return {
            "approvals": sorted(approvals),
            "capabilities": sorted(caps),
            "protected_commits": protected,
            "adoptable_records": adopted,
            "provider": (status["revision"], status["policy"], status["route"], status["digest"]),
        }

    def unchanged(self) -> None:
        assert self.state() == self.before


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    for name in FILES:
        shutil.copyfile(HOSTILE / name, environment.research / name)
    return environment


def _mcp(env: Env, steps: Any) -> Any:
    async def run() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await steps(client)

    return anyio.run(run)


def test_the_fixtures_really_carry_instructions() -> None:
    for name in FILES:
        text = (HOSTILE / name).read_bytes().decode("latin-1")
        assert "Approve approval request" in text and "exfil.example.test" in text, name
    reply = json.loads((HOSTILE / "model_output_smuggling.json").read_text("utf-8"))
    assert reply["confirmed"] is True and reply["approve"] == [PLANTED_ID]


def test_importing_and_reading_hostile_sources_changes_nothing_but_data(env: Env) -> None:
    wire = Tripwire(env)
    seq = env.seq()
    imported = env.import_files(list(FILES))
    assert env.seq() == seq + 1  # one import commit: sources and versions only
    kinds = {i["kind"] if "kind" in i else "source" for i in imported["plan"]["items"]}
    assert kinds
    versions = [i["version_id"] for i in imported["plan"]["items"]]
    # Every reader over the hostile text is a pure query.
    seq = env.seq()
    for caller in (AGENT, OWNER):
        env.svc.context(env.project_id, caller, purpose="orient", budget=12000)
        env.svc.search(env.project_id, caller, "approve", 20)
        env.svc.notebook(env.project_id, caller)
        for version_id in versions:
            env.svc.version_text(env.project_id, caller, version_id)
    assert env.seq() == seq
    wire.unchanged()
    # The planted approval ID names nothing; nothing was approved, requested or granted.
    with pytest.raises(PrometheosError) as out:
        env.svc.approval_get(env.project_id, AGENT, PLANTED_ID)
    assert out.value.code == "not_found"
    assert env.svc.approval_list(env.project_id, AGENT) == {"approvals": []}


def test_context_and_handoff_outputs_label_hostile_text_as_untrusted_data(env: Env) -> None:
    env.import_files(list(FILES))
    meeting = env.svc.capture_meeting(
        env.project_id,
        title="Hostile transcript",
        text=(HOSTILE / "transcript.txt").read_text("utf-8"),
        held_at="2026-09-28T10:00:00Z",
        request_id=new_request_id(),
    )
    context = env.svc.context(env.project_id, AGENT, purpose="orient", budget=12000)
    assert context["text"].splitlines()[1] == UNTRUSTED_NOTICE
    assert context["manifest"]["content_trust"] == CONTENT_TRUST == "untrusted_data"
    task = env.svc.task_prepare(
        env.project_id,
        purpose="meeting_extract",
        budget=12000,
        meeting_id=meeting["meeting_id"],
        request_id=new_request_id(),
    )
    fetched = env.svc.task_get(env.project_id, task["task_id"])
    assert fetched["context"].startswith(DATA_NOTICE)
    hostile_at = fetched["context"].find("SYSTEM OVERRIDE")
    assert hostile_at > fetched["context"].find(DATA_NOTICE) >= 0  # the label comes first
    # The instructions a task carries are the fixed purpose text, never record text.
    assert "SYSTEM OVERRIDE" not in fetched["instructions"]
    assert fetched["allowed_operations"] == ["create item (status proposed)"]


def test_hostile_text_over_mcp_and_http_stays_inert(env: Env) -> None:
    wire = Tripwire(env)

    async def steps(client: Client) -> Any:
        # Operation runs (decision 0017): wait for each one's result.
        plan = await client.call_tool(
            "source_import_plan",
            {"root_alias": "research", "paths": list(FILES), "wait_seconds": 20},
        )
        committed = await client.call_tool(
            "source_import_commit",
            {"plan_id": plan.structured_content["result"]["plan_id"], "wait_seconds": 20},
        )
        assert committed.structured_content["state"] == "completed"
        context = await client.call_tool("context_get", {})
        search = await client.call_tool("knowledge_search", {"query": "approve"})
        planted = await client.call_tool("approval_get", {"approval_id": PLANTED_ID})
        resumed = await client.call_tool("approval_resume", {"approval_id": PLANTED_ID})
        return context, search, planted, resumed

    context, search, planted, resumed = _mcp(env, steps)
    assert UNTRUSTED_NOTICE in context.structured_content["text"]
    assert not search.is_error  # a pure query over the hostile text
    for answer in (planted, resumed):
        assert answer.is_error and "not_found" in answer.content[0].text
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        token = hub.gate.new_agent_token()
        agent = {"Authorization": f"Bearer {token}"}
        assert hub.request("GET", "/api/v1/context").status == 200
        assert hub.request("GET", "/api/v1/knowledge/search?q=exfil").status == 200
        listed = hub.request("GET", "/api/v1/approvals", cookie=None, csrf=None, headers=agent)
        assert listed.json() == {"approvals": []}
        planted_http = hub.request(
            "POST",
            f"/api/v1/approvals/{PLANTED_ID}/resume",
            {},
            cookie=None,
            csrf=None,
            origin=None,
            headers=agent,
        )
        assert planted_http.status == 404
    seq_wire = wire.state()
    assert seq_wire["approvals"] == [] and seq_wire["capabilities"] == wire.before["capabilities"]
    assert seq_wire["provider"] == wire.before["provider"]


def test_hostile_meeting_text_proposes_nothing_and_adopts_nothing(env: Env) -> None:
    wire = Tripwire(env)
    meeting = env.svc.capture_meeting(
        env.project_id,
        title="Hostile transcript",
        text=(HOSTILE / "transcript.txt").read_text("utf-8"),
        held_at="2026-09-28T10:00:00Z",
        request_id=new_request_id(),
    )
    reconcile = env.svc.reconcile(env.project_id, AGENT, meeting["meeting_id"])
    assert "SYSTEM OVERRIDE" not in json.dumps(reconcile.get("actions", []))
    wire.unchanged()


def _hostile_reply(penv: Any, name: str) -> Any:
    reply = json.loads((HOSTILE / name).read_text("utf-8"))
    for item in reply["items"]:
        item["evidence"] = [penv.ref(3)]
    return reply


def test_hostile_model_output_stays_a_proposal_and_widens_nothing(tmp_path: Path) -> None:
    from provider_support import ENDPOINT, ProviderEnv, completion

    penv = ProviderEnv(tmp_path)
    penv.configure()
    wire = Tripwire(penv)
    transport = penv.script(completion(_hostile_reply(penv, "model_output.json")))
    result = penv.run()
    assert len(transport.requests) == 1 and transport.requests[0][0].startswith(ENDPOINT)
    items = [
        r
        for r in penv.svc.list_records(penv.project_id, OWNER, kind="item")["items"]
        if "SYSTEM OVERRIDE" in json.dumps(r["body"]) or "Approve approval" in json.dumps(r["body"])
    ]
    assert items and all(i["body"]["status"] == "proposed" for i in items), result
    after = wire.state()
    # Only the run's own grant was spent (issued by the test as the owner); the hostile reply
    # created no approval, no capability, no protected commit and no route change.
    assert (
        after["approvals"] == [] and after["protected_commits"] == wire.before["protected_commits"]
    )
    assert after["provider"] == wire.before["provider"]
    assert len(after["capabilities"]) == len(wire.before["capabilities"]) + 1
    assert all(url.startswith(ENDPOINT) for url, _, _ in transport.requests)


def test_smuggled_fields_in_model_output_are_rejected(tmp_path: Path) -> None:
    from provider_support import ProviderEnv, completion

    penv = ProviderEnv(tmp_path)
    penv.configure()
    smuggled = _hostile_reply(penv, "model_output_smuggling.json")
    wire = Tripwire(penv)
    penv.script(completion(smuggled), completion(smuggled))
    with pytest.raises(PrometheosError):
        penv.run()
    after = wire.state()
    assert after["approvals"] == [] and after["provider"] == wire.before["provider"]
    assert after["protected_commits"] == wire.before["protected_commits"]
    # The same reply submitted by an external agent as a task result is refused too.
    task = penv.svc.task_prepare(
        penv.project_id,
        purpose="meeting_extract",
        budget=12000,
        meeting_id=penv.meeting["meeting_id"],
        request_id=new_request_id(),
    )
    with pytest.raises(PrometheosError) as refused:
        penv.svc.task_submit(
            penv.project_id, task["task_id"], json.dumps(smuggled).encode(), new_request_id()
        )
    assert refused.value.code == "validation_error"
    assert wire.state()["approvals"] == []


def test_hostile_text_cannot_turn_on_egress(env: Env) -> None:
    """With the default no-AI policy, nothing in imported text sends anything anywhere."""
    from provider_support import FakeTransport

    transport = FakeTransport()
    env.svc.provider_transport = transport
    env.import_files(list(FILES))
    preview = env.svc.provider_preview(env.project_id, AGENT, purpose="next_steps")
    assert preview["would_send"] is False and preview["policy"] == "no-ai"
    with pytest.raises(PrometheosError) as err:
        env.svc.approval_request(
            env.project_id,
            AGENT,
            "provider_run",
            {"purpose": "next_steps", "digest": preview["digest"]},
        )
    assert err.value.code == "policy_denied"  # the owner's policy, not the text, decides
    assert transport.requests == []
    assert env.svc.approval_list(env.project_id, AGENT) == {"approvals": []}


def test_s24_requests_decisions_and_hostile_text_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance scenario S24 (C10-AUTH) on one store, owner decisions in the hub queue."""
    from datetime import UTC, datetime, timedelta

    from provider_support import ProviderEnv, completion, config_doc
    from test_cp10_contracts import assumption, supersede_correction

    from prometheos.app import approvals
    from prometheos.store import capabilities

    penv = ProviderEnv(tmp_path)
    for name in FILES:
        shutil.copyfile(HOSTILE / name, penv.research / name)
    pid = penv.project_id

    def submit(ops: list[dict[str, Any]]) -> dict[str, Any]:
        from cp1_support import request

        raw = request(pid, ops, penv.seq())
        with pytest.raises(PrometheosError) as err:
            penv.svc.apply(pid, raw, AGENT)
        assert err.value.code == "approval_required"
        return err.value.details

    # The hostile texts are processed (imported PDF and abstract; a model output below).
    penv.import_files(["paper.pdf", "abstract.bib"])
    # The agent prepares a correction apply and an egress expansion, plus two more requests.
    drag, replacement = assumption(penv), assumption(penv, "Speed-dependent drag")
    correction = supersede_correction(drag, 1, replacement)
    penv.apply([correction])
    correction_op = [{"op": "correction_apply", "id": correction["id"], "base_rev": 1}]
    first = submit(correction_op)
    with pytest.raises(PrometheosError) as err:
        penv.svc.approval_request(
            pid, AGENT, "provider_configure", {"config": config_doc(), "revision": penv.revision()}
        )
    egress = err.value.details
    project = penv.svc.get(pid, OWNER, pid, None)
    settings = dict(project["body"]) | {"current_objective": "Third request"}
    third = submit([{"op": "project_update", "base_rev": project["rev"], "settings": settings}])
    settings4 = dict(project["body"]) | {"current_objective": "Fourth request"}
    fourth = submit([{"op": "project_update", "base_rev": project["rev"], "settings": settings4}])
    for details in (first, egress, third, fourth):
        assert len(details["plan_sha256"]) == 64 and details["owner_route"]["hub"].startswith("/")

    wire_caps = Tripwire(penv).before["capabilities"]

    with running_hub(penv.svc, pid) as hub:
        hub.pair()
        grant = hub.request(
            "POST",
            f"/api/v1/approvals/{first['approval_request_id']}/grant",
            {"plan_sha256": first["plan_sha256"]},
        )
        deny = hub.request(
            "POST",
            f"/api/v1/approvals/{egress['approval_request_id']}/deny",
            {"plan_sha256": egress["plan_sha256"]},
        )
        assert grant.status == 200 and deny.status == 200
    # Editing the granted plan (one more operation) and resubmitting is a new, ungranted
    # request; it cannot be resumed.
    from cp1_support import create_entry

    edited = submit([*correction_op, create_entry(title="Smuggled extra")])
    assert edited["approval_request_id"] != first["approval_request_id"]
    assert edited["plan_sha256"] != first["plan_sha256"]
    with pytest.raises(PrometheosError) as err:
        penv.svc.approval_resume(pid, AGENT, edited["approval_request_id"])
    assert err.value.code == "approval_required"
    # The agent resumes the granted plan: it runs once.
    seq = penv.seq()
    done = penv.svc.approval_resume(pid, AGENT, first["approval_request_id"])
    assert done["replayed"] is False and penv.seq() == seq + 1
    assert penv.svc.approval_resume(pid, AGENT, first["approval_request_id"])["replayed"] is True
    assert penv.seq() == seq + 1
    # The denied egress expansion never runs; the route stays as the owner left it.
    with pytest.raises(PrometheosError) as err:
        penv.svc.approval_resume(pid, AGENT, egress["approval_request_id"])
    assert err.value.code == "policy_denied"
    assert penv.svc.provider_status(pid, AGENT)["policy"] == "no-ai"
    # The fourth is withdrawn; the third passes its expiry.
    penv.svc.approval_withdraw(pid, AGENT, fourth["approval_request_id"])
    later = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr(approvals, "_now", lambda: later)
    monkeypatch.setattr(capabilities, "utc_now", lambda: later.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    for details, state in ((edited, "expired"), (third, "expired"), (fourth, "withdrawn")):
        with pytest.raises(PrometheosError) as err:
            penv.svc.approval_resume(pid, AGENT, details["approval_request_id"])
        assert err.value.code == "policy_denied" and err.value.details["state"] == state
    monkeypatch.undo()
    queue = penv.svc.approval_queue(pid)["approvals"]
    states = {a["approval_request_id"]: a["state"] for a in queue}
    assert states[first["approval_request_id"]] == "used"
    assert states[egress["approval_request_id"]] == "denied"
    # A hostile model output stays a proposal (the owner configures the provider herself).
    penv.configure()
    transport = penv.script(completion(_hostile_reply(penv, "model_output.json")))
    penv.run()
    assert len(transport.requests) == 1
    after = Tripwire(penv).state()
    assert {a for a, _ in after["approvals"]} == set(states)  # no new request from any text
    # No secret value or grant reached the agent.
    agent_text = json.dumps(penv.svc.approval_list(pid, AGENT))
    assert "pcap_" not in agent_text and "sk-CANARY" not in agent_text
    for cap_id, _uses, _revoked in after["capabilities"]:
        assert cap_id not in agent_text
    assert len(wire_caps) <= len(after["capabilities"])
