"""C11-NOACCOUNT: the literature library works with no account, no provider key and no network
(CP11 S11.5; decision 0069 section 2.2; scenario S32).

The check runs on a fresh PrometheOS home with the key and account variables cleared. The hub and
the MCP server run as subprocesses under a test-only guard (``tests/noaccount_guard/
sitecustomize.py``, put first on their ``PYTHONPATH``; never part of the product) that refuses and
logs every connection and name lookup that is not loopback. On each route, the hub's HTTP API
with a paired owner session and the MCP server through the official SDK client, the check:

- imports PDFs and a bibliography from a research folder;
- searches the page text;
- builds a matrix with a grounded cell (a verified quotation, offsets and digest);
- links an action to it through ``source_refs``;
- exports the brief and a BibTeX file;

and asserts that every step succeeded and that the guard logged no outbound attempt. The guard is
proved too: a deliberate outbound connect, from a subprocess under the same guard, is logged and
refused, and loopback traffic is neither. A guard that never loaded would make "no attempt"
meaningless, so each subprocess must have written its ``installed`` line.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
import synthetic_bib as bibs
import synthetic_pdf as pdfs
from hub_support import Hub

from prometheos.app.service import Service
from prometheos.domain.ids import new_id, new_request_id

pytestmark = pytest.mark.noaccount

REPO = Path(__file__).resolve().parents[1]
GUARD = Path(__file__).resolve().parent / "noaccount_guard"
SENTENCE = "The drag coefficient was 0.0042 at 10 Pa."
COLUMNS = [
    {"id": "drag", "label": "Drag coefficient", "value_kind": "quantity", "instructions": ""},
    {"id": "finding", "label": "Key finding", "value_kind": "text", "instructions": ""},
]
KEY_PATTERNS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "ZOTERO")


def clean_environment(home: Path, guard_log: Path) -> dict[str, str]:
    """No accounts, no provider keys: every PROMETHEOS_* variable and every key-looking one is
    removed (the home is the only PrometheOS variable left), and the guard is switched on."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("PROMETHEOS_") and not any(p in k.upper() for p in KEY_PATTERNS)
    }
    env["PROMETHEOS_HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join([str(GUARD), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["NOACCOUNT_GUARD_LOG"] = str(guard_log)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def guard_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def attempts(path: Path) -> list[dict[str, Any]]:
    """Everything the guard logged but its own start-up line (a failed patch counts: the guard
    must be whole for "no attempt" to mean anything)."""
    return [line for line in guard_lines(path) if line["kind"] != "installed"]


def installed(path: Path) -> int:
    return len([line for line in guard_lines(path) if line["kind"] == "installed"])


class Project:
    """A fresh home and project with a research folder of synthetic PDFs and a bibliography."""

    def __init__(self, tmp: Path) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.home = tmp / "home"
        self.research = tmp / "research"
        pdfs.write_folder(
            self.research / "papers",
            {"drag.pdf": pdfs.paper(pdfs.DRAG), "kers.pdf": pdfs.paper(pdfs.KERS)},
        )
        bibs.write_folder(self.research / "papers", {"refs.bib": bibs.REFS_BIB.encode("utf-8")})
        service = Service(home=self.home, actor="test")
        self.pid: str = service.init_project(
            title="No-account check (C11-NOACCOUNT)",
            objective="Show the library works with no account and no network.",
            timezone="Europe/London",
            roots={"research": str(self.research)},
        )["project_id"]
        self.log = tmp / "guard.jsonl"
        self.env = clean_environment(self.home, self.log)


# ----- the guard itself ---------------------------------------------------------------------------


def run_guarded(code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@contextmanager
def loopback_listener() -> Iterator[int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)

    def accept() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield int(server.getsockname()[1])
    finally:
        server.close()
        thread.join(timeout=5)


def test_the_guard_detects_an_outbound_connect_and_leaves_loopback_alone(tmp_path: Path) -> None:
    """The check's own mutation: a deliberate outbound attempt is refused and logged; loopback
    traffic and Unix-style local use are not."""
    log = tmp_path / "guard.jsonl"
    env = clean_environment(tmp_path / "home", log)
    with loopback_listener() as port:
        done = run_guarded(
            f"""
            import socket, urllib.request, asyncio
            def attempt(label, call):
                try:
                    call()
                    print(label, "connected")
                except OSError as exc:
                    print(label, "refused", type(exc).__name__)
            attempt("loopback", lambda: socket.create_connection(("127.0.0.1", {port}), 5).close())
            attempt("localhost", lambda: socket.create_connection(("localhost", {port}), 5).close())
            attempt("outbound-ip", lambda: socket.create_connection(("203.0.113.7", 80), 2))
            attempt("raw-connect", lambda: socket.socket().connect(("203.0.113.5", 80)))
            def connect_ex():
                code = socket.socket().connect_ex(("203.0.113.4", 80))
                if code == 0:
                    return
                raise OSError(code, "refused")
            attempt("connect-ex", connect_ex)
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            attempt("udp-sendto", lambda: udp.sendto(b"x", ("203.0.113.3", 53)))
            attempt("udp-sendto-flags", lambda: udp.sendto(b"x", 0, ("203.0.113.8", 53)))
            if hasattr(udp, "sendmsg"):  # not on Windows
                attempt("udp-sendmsg", lambda: udp.sendmsg([b"x"], [], 0, ("203.0.113.2", 53)))
            async def by_sock_connect():
                import asyncio as aio
                loop = aio.get_running_loop()
                raw = socket.socket()
                raw.setblocking(False)
                try:
                    await loop.sock_connect(raw, ("203.0.113.6", 80))
                finally:
                    raw.close()
            attempt("asyncio-sock-connect", lambda: asyncio.run(by_sock_connect()))
            quiet = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            quiet.bind(("127.0.0.1", 0))
            attempt("udp-loopback", lambda: udp.sendto(b"x", quiet.getsockname()))
            attempt("outbound-name", lambda: socket.create_connection(("example.invalid", 80), 2))
            attempt("urllib", lambda: urllib.request.urlopen("http://example.invalid/", timeout=2))
            async def by_asyncio():
                await asyncio.open_connection("203.0.113.9", 443)
            attempt("asyncio", lambda: asyncio.run(by_asyncio()))
            """,
            env,
        )
    assert done.returncode == 0, done.stderr
    outcome = dict(line.split(" ", 2)[:2] for line in done.stdout.splitlines())
    refused = (
        "outbound-ip", "raw-connect", "connect-ex", "outbound-name", "urllib", "asyncio",
        "asyncio-sock-connect", "udp-sendto", "udp-sendto-flags",
    ) + (("udp-sendmsg",) if hasattr(socket.socket, "sendmsg") else ())  # fmt: skip
    local = ("loopback", "localhost", "udp-loopback")
    assert outcome == dict.fromkeys(local, "connected") | dict.fromkeys(refused, "refused")
    seen = attempts(log)
    assert {(a["kind"], a["target"]) for a in seen} >= {
        ("dns", "203.0.113.7"),  # a lookup, even of an address literal, is an outbound step
        ("dns", "example.invalid"),
        ("connect", "203.0.113.5:80"),  # a raw connect that skipped the lookup
        ("connect_ex", "203.0.113.4:80"),
        ("sock_connect", "203.0.113.6:80"),  # asyncio's own connect path
        ("create_connection", "203.0.113.9"),
        ("sendto", "203.0.113.3:53"),  # a UDP datagram
        ("sendto", "203.0.113.8:53"),
    } | ({("sendmsg", "203.0.113.2:53")} if hasattr(socket.socket, "sendmsg") else set()), seen
    assert installed(log) == 1
    assert not any(a["target"].startswith("127.") or a["target"] == "localhost" for a in seen)


def test_a_guard_that_is_not_switched_on_does_nothing(tmp_path: Path) -> None:
    env = clean_environment(tmp_path / "home", tmp_path / "guard.jsonl")
    env.pop("NOACCOUNT_GUARD_LOG")
    done = run_guarded(
        "import socket; print(socket.socket.connect.__module__ if hasattr(socket.socket.connect, "
        "'__module__') else 'native')",
        env,
    )
    assert done.returncode == 0 and "sitecustomize" not in done.stdout
    assert not (tmp_path / "guard.jsonl").exists()


def test_the_environment_has_no_account_or_key_variable(tmp_path: Path) -> None:
    secrets = {
        "OPENAI_API_KEY": "sk-test", "ANTHROPIC_API_KEY": "sk-test", "PROMETHEOS_SECRET_X": "x",
        "PROMETHEOS_ZOTERO_PORT": "1", "ZOTERO_API_KEY": "z", "GITHUB_TOKEN": "t",
    }  # fmt: skip
    saved = {k: os.environ.get(k) for k in secrets}
    os.environ.update(secrets)
    try:
        env = clean_environment(tmp_path / "home", tmp_path / "g.jsonl")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert not set(secrets) & set(env)
    assert [k for k in env if k.upper().startswith("PROMETHEOS_")] == ["PROMETHEOS_HOME"]


# ----- through the hub (HTTP, a paired owner session) --------------------------------------------


@contextmanager
def hub_process(project: Project) -> Iterator[Hub]:
    info = project.tmp / "hub-info.json"
    log = open(project.tmp / "hub.log", "w", encoding="utf-8")
    server = subprocess.Popen(
        [
            sys.executable,
            str(REPO / "tools" / "walk_hub18.py"),
            "--home",
            str(project.home),
            "--project",
            project.pid,
            "--info",
            str(info),
            "--tokens",
            "1",
        ],
        cwd=REPO,
        env=project.env,
        stdout=log,
        stderr=log,
    )
    try:
        deadline = time.monotonic() + 90
        while not info.exists():
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(
                    "the hub did not start: " + (project.tmp / "hub.log").read_text()
                )
            time.sleep(0.2)
        time.sleep(0.5)
        data = json.loads(info.read_text(encoding="utf-8"))
        hub = Hub(port=int(data["port"]), gate=None, token=data["pairing_tokens"][0])  # type: ignore[arg-type]
        hub.pair()
        yield hub
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


def api(hub: Hub, method: str, path: str, body: Any = None) -> Any:
    reply = hub.request(method, f"/api/v1{path}", body)
    assert reply.status == 200, (method, path, reply.status, reply.body[:400])
    return reply.json()


def finish(hub: Hub, run: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while run["state"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.5)
        run = api(hub, "GET", f"/runs/{run['run_id']}")
    assert run["state"] == "completed", run
    return dict(run["result"])


def action_transaction(pid: str, seq: int, matrix_id: str, rev: int) -> tuple[dict[str, Any], str]:
    action_id = new_id("action")
    return {
        "schema_version": 1, "project_id": pid, "request_id": new_request_id(), "expected_seq": seq,
        "operations": [
            {
                "op": "create", "kind": "action", "id": action_id, "base_rev": 0,
                "sensitivity": "normal", "occurred_at": None,
                "body": {
                    "title": "Check the drag coefficient against a second source",
                    "reason": "The matrix cites one paper.", "source_refs": [
                        {"id": matrix_id, "rev": rev}
                    ],
                    "owner_ref": None, "due_at": None, "dependencies": [], "status": "proposed",
                    "blocker": None, "completion_evidence": [], "commitment_item_id": None,
                    "description": None, "labels": [],
                },
            }
        ],
    }, action_id  # fmt: skip


def test_the_library_flow_needs_no_account_and_makes_no_outbound_connection_via_the_hub(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path / "hub")
    with hub_process(project) as hub:
        planned = finish(
            hub,
            api(hub, "POST", "/library/plans", {"root_alias": "research", "wait_seconds": 20}),
        )
        assert planned["counts"]["create"] == 3 and planned["counts"]["entries_create"] >= 5
        committed = finish(hub, api(hub, "POST", f"/library/plans/{planned['plan_id']}/commit", {}))
        assert committed["counts"]["committed"] == 3 and committed["counts"]["skipped"] == 0
        works = api(hub, "GET", "/library/works?limit=200")["works"]
        drag = next(w for w in works if w["title"].startswith("Aerodynamic"))
        assert drag["access_state"] == "full_text" and len(works) >= 8
        hits = api(hub, "GET", "/library/search?" + urlencode({"query": "drag coefficient"}))[
            "hits"
        ]
        assert any(h["work_id"] == drag["work_id"] and h["page"] == 2 for h in hits)
        made = api(
            hub, "POST", "/matrix",
            {"title": "Flywheel losses", "columns": COLUMNS, "work_ids": [drag["work_id"]]},
        )  # fmt: skip
        text = api(hub, "GET", f"/library/works/{drag['work_id']}/read?page=2&max_chars=8000")[
            "text"
        ]
        at = text.index(SENTENCE)
        read = api(
            hub, "GET",
            f"/library/works/{drag['work_id']}/read?page=2&start={at}&end={at + len(SENTENCE)}",
        )  # fmt: skip
        api(
            hub, "POST", f"/matrix/{made['matrix_id']}/propose",
            {
                "work_id": drag["work_id"], "column_id": "drag", "value_text": "0.0042",
                "evidence": [read["evidence"]], "quantities": [{"decimal": "0.0042", "unit": ""}],
            },
        )  # fmt: skip
        shown = api(hub, "GET", f"/matrix/{made['matrix_id']}")
        cell = next(c for r in shown["rows"] for c in r["cells"] if c["column_id"] == "drag")
        assert cell["state"] == "proposed" and cell["value_text"] == "0.0042"
        assert cell["evidence"][0]["where"]["page"] == 2
        seq = api(hub, "GET", "/project")["project_seq"]
        body, action_id = action_transaction(
            project.pid, seq, made["matrix_id"], shown["matrix"]["rev"]
        )
        api(hub, "POST", "/transactions", body)
        action = api(hub, "GET", f"/records/{action_id}")
        assert action["body"]["source_refs"] == [
            {"id": made["matrix_id"], "rev": shown["matrix"]["rev"]}
        ]
        brief = api(hub, "POST", f"/matrix/{made['matrix_id']}/export", {"format": "markdown"})
        assert "0.0042" in brief["content"] and brief["brief"]["version_id"]
        bibtex = api(hub, "POST", "/library/export", {"format": "bibtex"})
        assert bibtex["count"] >= 8 and "@article" in bibtex["content"]
    assert installed(project.log) == 1  # the guard was loaded in the hub process
    assert attempts(project.log) == []


# ----- through MCP (the official SDK client) -----------------------------------------------------


async def call(client: Any, tool: str, args: dict[str, Any]) -> Any:
    result = await client.call_tool(tool, args)
    assert not result.is_error, (tool, args, result.content[0].text[:500])
    return result.structured_content


async def finished(client: Any, view: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while view["state"] in ("queued", "running") and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        view = await call(client, "run_get", {"run_id": view["run_id"]})
    assert view["state"] == "completed", view
    return dict(view["result"])


async def mcp_flow(project: Project) -> None:
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "prometheos", "mcp", "serve", "--stdio", "--project", project.pid],
        env=project.env,
    )
    async with Client(params) as client:
        plan = await finished(
            client,
            await call(
                client, "library_import_plan", {"root_alias": "research", "wait_seconds": 20}
            ),
        )
        assert plan["counts"]["create"] == 3
        done = await finished(
            client,
            await call(
                client, "library_import_commit", {"plan_id": plan["plan_id"], "wait_seconds": 20}
            ),
        )
        assert done["counts"]["committed"] == 3 and done["counts"]["skipped"] == 0
        listed = (await call(client, "library", {"request": {"op": "works", "limit": 200}}))[
            "result"
        ]
        drag = next(w for w in listed["works"] if w["title"].startswith("Aerodynamic"))
        assert drag["access_state"] == "full_text" and len(listed["works"]) >= 8
        found = (
            await call(
                client, "library", {"request": {"op": "search", "query": "drag coefficient"}}
            )
        )["result"]
        assert any(h["work_id"] == drag["work_id"] and h["page"] == 2 for h in found["hits"])
        page = (
            await call(
                client, "library",
                {
                    "request": {
                        "op": "read", "work_id": drag["work_id"], "page": 2, "max_chars": 8000
                    }
                },
            )
        )["result"]  # fmt: skip
        at = page["text"].index(SENTENCE)
        span = (
            await call(
                client, "library",
                {
                    "request": {
                        "op": "read", "work_id": drag["work_id"], "page": 2, "start": at,
                        "end": at + len(SENTENCE),
                    }
                },
            )
        )["result"]  # fmt: skip
        made = (
            await call(
                client, "matrix_manage",
                {
                    "request": {
                        "op": "create", "title": "Flywheel losses", "columns": COLUMNS,
                        "work_ids": [drag["work_id"]],
                    }
                },
            )
        )["result"]  # fmt: skip
        await call(
            client, "matrix_manage",
            {
                "request": {
                    "op": "propose", "matrix_id": made["matrix_id"], "work_id": drag["work_id"],
                    "column_id": "drag", "value_text": "0.0042", "evidence": [span["evidence"]],
                    "quantities": [{"decimal": "0.0042", "unit": ""}],
                }
            },
        )  # fmt: skip
        shown = (
            await call(
                client, "matrix", {"request": {"op": "show", "matrix_id": made["matrix_id"]}}
            )
        )["result"]
        cell = next(c for r in shown["rows"] for c in r["cells"] if c["column_id"] == "drag")
        assert cell["state"] == "proposed" and cell["evidence"][0]["where"]["page"] == 2
        info = await call(client, "project_info", {})
        body, action_id = action_transaction(
            project.pid, info["project_seq"], made["matrix_id"], shown["matrix"]["rev"]
        )
        await call(client, "apply", {"request": body})
        stored = await call(client, "record_get", {"record_id": action_id})
        assert stored["body"]["source_refs"] == [
            {"id": made["matrix_id"], "rev": shown["matrix"]["rev"]}
        ]
        brief = (
            await call(
                client, "matrix_manage",
                {"request": {"op": "export", "matrix_id": made["matrix_id"], "format": "markdown"}},
            )
        )["result"]  # fmt: skip
        assert "0.0042" in brief["content"] and brief["brief"]["version_id"]
        bibtex = (await call(client, "library", {"request": {"op": "export", "format": "bibtex"}}))[
            "result"
        ]
        assert bibtex["count"] >= 8 and "@article" in bibtex["content"]


def test_the_library_flow_needs_no_account_and_makes_no_outbound_connection_via_mcp(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path / "mcp")
    asyncio.run(mcp_flow(project))
    assert installed(project.log) == 1  # the guard was loaded in the MCP server process
    assert attempts(project.log) == []
