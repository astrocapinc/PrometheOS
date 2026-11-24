"""A disposable world and three agent routes for the CP10.6 parity tests (synthetic only).

``World`` is a PrometheOS home with one research root and the fixtures every operation family
needs: an imported manuscript and venue guide with a paper, a skill package folder and a bare
skill folder inside the root, a meeting, a prepared task, an installed package with a recipe run
and a recording session created at agent rank. ``Routes`` calls one registry operation through
the service, the MCP family tools (official SDK client) and the local HTTP API (agent bearer
token), and returns the same outcome shape for each so they can be compared.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import anyio
from audio_support import MIC, AudioEnv, ScriptedBackend, ScriptedSource
from cp1_support import AGENT, OWNER, OWNER_ACT
from cp4_support import RECIPE, import_package, install, skill_md, write_package
from cp7_support import PaperProject
from hub_support import Hub, running_hub
from mcp import Client

from prometheos import agentops
from prometheos.audio import recorder
from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server

JOB = {
    "handler": "paper_check",
    "parameters": {"paper_id": "PAPER"},
    "schedule": {"type": "interval", "minutes": 60, "start_utc": "2026-10-01T00:00:00Z"},
    "enabled": True,
    "budget": {
        "max_inputs": 50,
        "max_output_chars": 4000,
        "max_provider_calls": 0,
        "max_concurrent_runs": 1,
        "spend_cap_estimate_usd": None,
    },
    "last_slot": None,
}


class World:
    def __init__(self, tmp: Path) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.env = AudioEnv(tmp)
        self.svc, self.pid, self.root = self.env.svc, self.env.project_id, self.env.research
        paper = PaperProject(self.env)
        self.paper_id = paper.paper()
        self.manuscript_version: str = paper.manuscript["version_id"]
        self.venue_version: str = paper.venue["version_id"]
        self.plant_version: str = paper.plant["version_id"]
        write_package(self.root / "pkgs" / "synthetic-evidence")
        write_package(self.root / "pkgs" / "second-evidence", name="second-evidence")
        skill = self.root / "skills-src" / "bare-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(skill_md("bare-skill"), encoding="utf-8")
        (self.root / "project-a").mkdir()
        (self.root / "handoff").mkdir()
        (self.root / "exports").mkdir()
        self.meeting: str = self.svc.capture_meeting(
            self.pid,
            OWNER,
            title="Supervision",
            text=(self.root / "meeting.txt").read_text(encoding="utf-8"),
            held_at="2026-09-28T10:00:00Z",
            request_id=new_request_id(),
        )["meeting_id"]
        self.task: str = self.svc.task_prepare(
            self.pid,
            purpose="next_steps",
            budget=4000,
            meeting_id=None,
            request_id=new_request_id(),
        )["task_id"]
        # An installed package with a recipe run (the packages family and workflows).
        import_package(self.svc, write_package(tmp / "pkg-src", recipes=[RECIPE]))
        target = tmp / "installed-into"
        target.mkdir()
        installed = install(self.svc, "synthetic-evidence@1.0.0", "claude-code", target, self.pid)
        projection = self.root / "installed-a"
        projection.mkdir()
        install(self.svc, "synthetic-evidence@1.0.0", "claude-code", projection, None)
        self.package_record: str = installed["record"]["results"][0]["id"]
        self.run: str = self.svc.workflow_start(
            self.pid,
            package_id=self.package_record,
            recipe_id="evidence-pass",
            inputs={"question": "Does the plant hold?"},
            request_id=new_request_id(),
        )["results"][0]["id"]
        # Two recording sessions, created and not captured (no device is opened): one started at
        # agent rank, which an agent may control, and one the owner started, which it may not.
        backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 5)})

        def session(caller: Any, title: str) -> str:
            created = self.svc.recording_start(
                self.pid,
                caller,
                recorder.SessionRequest(
                    title, ("mic-1",), consent_acknowledged=True, sensitivity="normal"
                ),
                backend,
                authority=OWNER_ACT,
            )
            return str(created["session_id"])

        self.session: str = session(AGENT, "Agent standup")
        self.owner_session: str = session(OWNER, "Owner standup")
        self.ended_session: str = session(AGENT, "Cancelled standup")
        self.env.execute(
            "UPDATE recording_sessions SET state = 'cancelled' WHERE id = ?",
            (self.ended_session,),
        )
        # A transcript (synthetic engine) of a normal-sensitivity recording, for span reads.
        audio = self.env.import_audio(sensitivity="normal")
        done = self.svc.transcribe_audio(
            self.pid, OWNER, audio["version_id"], self.env.synthetic_config()
        )
        self.transcript: str = done["result_version_id"]
        self.job_body = json.loads(json.dumps(JOB).replace("PAPER", self.paper_id))

    # ----- ID normalisation ---------------------------------------------------------------
    def names(self) -> dict[str, str]:
        return {
            self.paper_id: "paper",
            self.paper_id.split("_", 1)[1][:8]: "paper-short",
            self.manuscript_version: "manuscript_version",
            self.venue_version: "venue_version",
            self.meeting: "meeting",
            self.task: "task",
            self.package_record: "package_record",
            self.run: "run",
            self.session: "session",
            self.owner_session: "owner_session",
            self.ended_session: "ended_session",
            self.transcript: "transcript",
        }


ID = re.compile(
    r"[a-z]+_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
VOLATILE = {
    "project_seq",
    "generated_at",
    "request_id",
    "created_at",
    "updated_at",
    "recorded_at",
    "expires_at",
    "checked_at",
    "imported_at",
    "started_at",
    "elapsed_seconds",
    "heartbeat_at",
    "level",
    "plan_id",
    "bundle_sha256",
    "sha256",
    "plan_sha256",
    "import_plan_sha256",
    "digest",
    "project_id",
    "abandoned",
    "findings_digest",
    "plan_digest",
    "store",
    "home",
    "platform",
    "python",
    "version",
    "now",
    "next_due",
    "due_utc",
    "candidate_id",  # CP11 S11.2: a digest of the pair's (world-specific) work IDs
    "decision_id",
    "content_sha256",  # CP11 S11.4: a matrix export's digest covers its world-specific IDs
}


def normalise(world: World, value: Any, drop: set[str] | None = None) -> Any:
    """Known IDs become names, other IDs and times placeholders; volatile keys are dropped."""
    skip = VOLATILE | (drop or set())

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in sorted(node.items()) if k not in skip}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            for known, label in sorted(world.names().items(), key=lambda kv: -len(kv[0])):
                node = node.replace(known, f"<{label}>")
            node = ID.sub("<id>", node)
            return TIME.sub("<time>", node)
        return node

    return walk(value)


Outcome = dict[str, Any]


def ok(result: Any) -> Outcome:
    return {"ok": True, "result": result}


def failed(code: str, details: Any = None) -> Outcome:
    return {"ok": False, "code": code, "details": details}


class Routes:
    """One agent, three routes. ``call(op_id, params)`` returns an :data:`Outcome`."""

    def __init__(self, world: World, hub: Hub, token: str) -> None:
        self.world = world
        self.hub = hub
        self.headers = {"Authorization": f"Bearer {token}"}
        agentops.load()

    # ----- the service (reference: handler plus application service) ----------------------
    def service(self, op_id: str, params: dict[str, Any]) -> Outcome:
        a = agentops.OPS[op_id]
        from prometheos.app.reads import Caller

        ctx = agentops.Ctx(self.world.svc, self.world.pid, Caller(actor="test"))
        try:
            return ok(agentops.execute(a, ctx, params))
        except PrometheosError as exc:
            return failed(exc.code, exc.details)

    # ----- MCP (official SDK client) -------------------------------------------------------
    def mcp(self, op_id: str, params: dict[str, Any]) -> Outcome:
        a = agentops.OPS[op_id]
        assert a.tool and a.sub, op_id

        async def run() -> Any:
            async with Client(build_server(self.world.svc, self.world.pid)) as client:
                return await client.call_tool(a.tool, {"request": {"op": a.sub, **params}})

        result = anyio.run(run)
        if result.is_error:
            text = result.content[0].text
            envelope = json.loads(text[text.index("{") :])["error"]
            return failed(envelope["code"], envelope.get("details"))
        return ok(result.structured_content["result"])

    # ----- HTTP (agent bearer token) -------------------------------------------------------
    def http(self, op_id: str, params: dict[str, Any]) -> Outcome:
        from prometheos import registry

        a = agentops.OPS.get(op_id)
        route = a.http if a is not None and a.http else registry.by_id()[op_id].http[0]
        method, _, path = route.partition(" ")
        data = dict(params)
        if op_id in ("recording_recover", "recording_discard"):
            data["action"] = op_id.removeprefix("recording_")
        for name in re.findall(r"\{(\w+)\}", path):
            path = path.replace("{" + name + "}", str(data.pop(name)))
        body = None
        if method == "GET" or op_id in QUERY_ONLY:
            if data:
                from urllib.parse import urlencode

                path += "?" + urlencode(
                    {k: json.dumps(v) if isinstance(v, bool) else v for k, v in data.items()}
                )
        else:
            body = data
        reply = self.hub.request(
            method, path, body, cookie=None, csrf=None, origin=None, headers=self.headers
        )
        payload = reply.json()
        if reply.status >= 400:
            error = payload["error"]
            return failed(error["code"], error.get("details"))
        if op_id in UNWRAP:
            payload = payload[UNWRAP[op_id]]
        return ok(payload)


# Routes whose HTTP form differs from the MCP one only in shape: the options travel in the
# query string, or the reply wraps the operation's result.
QUERY_ONLY = {"recording_discard", "recording_control"}
UNWRAP = {"tex_engine_plan": "plan"}


def running_routes(world: World) -> Any:
    from contextlib import contextmanager

    @contextmanager
    def manager() -> Any:
        with running_hub(world.svc, world.pid) as hub:
            hub.pair()
            yield Routes(world, hub, hub.gate.new_agent_token())

    return manager()
