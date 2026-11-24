"""CP10.2 round 3 regressions (review cp10-2-a-r2; decision 0016 "Round 3").

R2-T: every route the agent token reaches is called with restricted canaries in the store
(an entry, a source, a bound workspace file, an owner-rank approval request) and nothing
restricted comes back; a static check that admitted handlers never pass the owner caller or
issue a grant outside an owner branch; an agent cannot stop an owner-rank recording (N17) or
read the owner queue (N23). R2-5: the push recheck at resume (N5), the token file restricted
before the token is written (N12), the service-level owner check on recording start (N15).
R2-1 keyed owner-view binding; R2-2 an unrelated edit keeps a push grant; R2-3 the CLI start
prompt escapes agent text; R2-4 the server refuses a stale grant; R2-6/R2-7.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, FULL, OWNER, OWNER_ACT, create_entry, request
from hub_support import running_hub
from test_cp10_authority import grant, refused, rows

from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError
from prometheos.hub.app import AGENT_ROUTES

CANARY = "ZQXCANARY"
REPO = Path(__file__).resolve().parents[1]


# ----- R2-T: every admitted route, restricted canaries -----------------------------------------


class Canaried:
    """A linked paper workspace plus restricted canaries in every place an agent route reads."""

    def __init__(self, tmp: Path) -> None:
        from test_cp9_review_cp9a_r2 import _restrict_in_clone
        from test_cp9_sync import Linked

        self.linked = Linked(tmp)
        self.env = self.linked.env
        self.svc, self.pid, self.paper_id = self.linked.svc, self.linked.pid, self.linked.paper_id
        hidden = create_entry(
            sensitivity="restricted", title=f"{CANARY}-ENTRY frobnicate", text="frobnicate here"
        )
        self.env.apply([hidden], caller=OWNER)
        self.hidden_id = hidden["id"]
        (self.env.research / "secret.txt").write_text(f"{CANARY}-BYTES frobnicate\n", "utf-8")
        plan = self.svc.import_plan(
            self.pid,
            OWNER,
            root_alias="research",
            paths=["secret.txt"],
            retention="copy",
            sensitivity="restricted",
        )
        self.svc.import_commit(self.pid, plan["plan_id"], OWNER)
        item = plan["items"][0]
        self.source_id, self.version_id = item["source_id"], item["version_id"]
        bib = self.linked.folder / "refs.bib"
        bib.write_text(bib.read_text("utf-8") + f"% {CANARY}-BIB\n", "utf-8", newline="\n")
        _restrict_in_clone(self.linked, "refs.bib")
        project = self.svc.get(self.pid, OWNER, self.pid, None)
        settings = dict(project["body"]) | {"current_objective": f"{CANARY}-OWNER-PLAN"}
        raw = request(
            self.pid,
            [{"op": "project_update", "base_rev": project["rev"], "settings": settings}],
            self.env.seq(),
        )
        with pytest.raises(PrometheosError) as err:
            self.svc.apply(self.pid, raw, OWNER)
        self.owner_request = err.value.details["approval_request_id"]
        # CP10.3 round 2 (F9): real runs on the run routes. The agent's compile run is over a
        # workspace holding the restricted refs.bib (it must never name or hash it); the
        # owner's import runs over secret.txt are above the agent's rank.
        import hashlib

        from prometheos.app import runs

        run_env = runs.Env(self.svc, self.pid, self.svc.open(self.pid))
        agent_run, _ = runs.create(
            run_env, AGENT, "paper_compile", {"paper_id": self.paper_id, "fetch": False}
        )
        self.agent_run = agent_run.id
        self.owner_run = self.svc.run_list(self.pid, OWNER, operation="source_import_commit")[
            "runs"
        ][0]["run_id"]
        bib_sha = hashlib.sha256(bib.read_bytes()).hexdigest()
        secret_sha = hashlib.sha256((self.env.research / "secret.txt").read_bytes()).hexdigest()
        self.markers = (CANARY, self.source_id, bib_sha, secret_sha, self.owner_run)
        # CP10.5 (decision 0021): a whole-store backup holding every canary, so the agent's
        # verify and managed-new restore run for real. The healthy-store switch is refused;
        # a separate integration world covers the damaged-source switch request.
        self.backup_name = Path(self.svc.backup(self.pid, None, FULL)["backup"]).name


@pytest.fixture(scope="module")
def canaried(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Canaried, Any, str]]:
    world = Canaried(tmp_path_factory.mktemp("canaries"))
    with running_hub(world.svc, world.pid) as hub:
        yield world, hub, hub.gate.new_agent_token()


QUERIES = {
    "/api/v1/knowledge/search": "?q=frobnicate",
    "/api/v1/papers/{paper_id}/workspace/file": "?path=refs.bib",
    "/api/v1/papers/{paper_id}/synctex/forward": "?file=main.tex&line=1",
    "/api/v1/papers/{paper_id}/synctex/inverse": "?page=1&x=1&y=1",
    # CP10.6 (decision 0022): routes generated from the agent operation table
    "/api/v1/papers/{paper_id}/handoff/plan": "?root_alias=research",
    "/api/v1/transcripts/{version_id}/span": "?start_ms=0&end_ms=1000",
}


def _body(path: str, world: Canaried) -> Any:
    if path == "/api/v1/sources/plans":
        # An operation run: wait for it, so its outcome is in the reply (decision 0017).
        return {"root_alias": "research", "paths": ["secret.txt"], "wait_seconds": 20}
    if path == "/api/v1/provider/preview":
        return {"purpose": "next_steps"}
    if path == "/api/v1/decisions/preview":
        return {"request": json.loads(request(world.pid, [create_entry()], world.env.seq()))}
    if path == "/api/v1/papers/{paper_id}/workspace/file":
        return {"path": "refs.bib", "text": "x", "base_sha256": None}
    # This sweep's project stays healthy so all normal routes remain reachable. Restore
    # exercises the allowed managed-new destination; its healthy-store switch is refused.
    # The integration regression separately proves the damaged-store switch request.
    target = "<PrometheOS home>/restored/canary-sweep"
    if path == "/api/v1/maintenance/verify":
        return {"backup": world.backup_name}
    if path == "/api/v1/maintenance/restore":
        return {"backup": world.backup_name}
    if path == "/api/v1/maintenance/switch":
        return {"backup": world.backup_name, "target": target}
    if path == "/api/v1/papers/{paper_id}/export":  # a bundle written for the agent's view
        return {"root_alias": "research", "path": "canary-export.zip"}
    if path == "/api/v1/skills/inspect" or path == "/api/v1/skills/import-plan":
        return {"root_alias": "research", "path": "secret.txt"}
    return {}


@pytest.mark.parametrize("route", sorted(AGENT_ROUTES))
def test_no_agent_route_returns_anything_restricted(
    canaried: tuple[Canaried, Any, str], route: str
) -> None:
    world, hub, token = canaried
    method, template = route.split(" ", 1)
    fill = {
        "record_id": world.hidden_id,
        "version_id": world.version_id,
        "meeting_id": world.hidden_id,
        "paper_id": world.paper_id,
        "task_id": world.hidden_id,
        "job_id": world.hidden_id,
        "approval_id": world.owner_request,
        "run_id": world.agent_run,
        "session_id": "x",
        "ref": "x",
        "plan_id": "x",
        "notification_id": "1",
        "action": "stop",
        "name": "entry_body",
    }
    path = template
    for key, value in fill.items():
        path = path.replace("{" + key + "}", value)
    path += QUERIES.get(template, "")
    body = None if method == "GET" else _body(template, world)
    reply = hub.request(
        method,
        path,
        body,
        cookie=None,
        csrf=None,
        origin=None,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert reply.status != 401, route  # the token was accepted (the route is admitted)
    text = reply.body.decode("utf-8", "replace")
    # CP10.4 (review F5/R7): an owner-rank approval request's ID is a marker on every route that
    # was not asked about that request by its own ID (those echo the ID they were given).
    markers = (
        world.markers if "{approval_id}" in template else (*world.markers, world.owner_request)
    )
    for marker in markers:
        assert marker not in text, (route, reply.status, text[:400])
    if template == "/api/v1/maintenance/restore":
        import sqlite3
        from contextlib import closing

        from prometheos.app.maintenance import Maintenance

        assert reply.status == 200, text
        target = Maintenance(world.svc).resolve_location(reply.json()["target_store_label"])
        assert target.is_relative_to(world.env.home) and target != world.env.store_dir
        with closing(sqlite3.connect(target / "record.sqlite")) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            row = conn.execute(
                "SELECT r.sensitivity, b.encoded FROM records r "
                "JOIN revisions v ON v.record_id=r.id AND v.rev=r.current_rev "
                "JOIN bodies b ON b.id=v.body_id WHERE r.id=?",
                (world.hidden_id,),
            ).fetchone()
            assert row[0] == "restricted" and CANARY in row[1]
    if template == "/api/v1/papers/{paper_id}/export":
        # The agent's bundle holds nothing the agent may not see: the restricted refs.bib is out.
        import zipfile

        archive = world.env.research / "canary-export.zip"
        if archive.exists():
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.namelist():
                    assert CANARY.encode() not in bundle.read(member), member


def test_the_owner_sees_the_canaries_the_agent_does_not(
    canaried: tuple[Canaried, Any, str],
) -> None:
    """The canaries are real: the paired owner reaches each one through the same routes."""
    world, hub, _token = canaried
    hub.pair()
    assert CANARY in hub.request("GET", f"/api/v1/versions/{world.version_id}/text").body.decode()
    assert CANARY in hub.request("GET", "/api/v1/knowledge/search?q=frobnicate").body.decode()
    assert CANARY in hub.request("GET", "/api/v1/context").body.decode()
    owner_file = hub.request("GET", f"/api/v1/papers/{world.paper_id}/workspace/file?path=refs.bib")
    assert CANARY in owner_file.body.decode()
    queue = hub.request("GET", "/api/v1/approvals").body.decode()
    assert f"{CANARY}-OWNER-PLAN" in queue
    plan = hub.request(
        "POST",
        "/api/v1/sources/plans",
        {"root_alias": "research", "paths": ["secret.txt"], "wait_seconds": 20},
    )
    assert plan.json()["state"] in ("completed", "failed")  # an operation run (decision 0017)
    assert world.source_id in plan.body.decode()


def test_the_agent_token_never_reads_an_owner_run(canaried: tuple[Canaried, Any, str]) -> None:
    """CP10.3 round 2 (F9): the owner's import run (restricted input) reads as absent."""
    world, hub, token = canaried
    agent = {"Authorization": f"Bearer {token}"}
    one = hub.request("GET", f"/api/v1/runs/{world.owner_run}", cookie=None, headers=agent)
    assert one.status == 404 and one.json()["error"]["code"] == "not_found"
    assert CANARY not in one.body.decode()
    listed = hub.request("GET", "/api/v1/runs?limit=200", cookie=None, headers=agent)
    assert listed.status == 200 and world.agent_run in listed.body.decode()
    for marker in world.markers:
        assert marker not in listed.body.decode()


def test_the_agent_token_never_reads_the_owner_queue(canaried: tuple[Canaried, Any, str]) -> None:
    world, hub, token = canaried
    agent = {"Authorization": f"Bearer {token}"}
    mine = hub.request("GET", "/api/v1/approvals", cookie=None, headers=agent)
    assert world.owner_request not in mine.body.decode()
    one = hub.request("GET", f"/api/v1/approvals/{world.owner_request}", cookie=None, headers=agent)
    assert one.status == 404


# ----- R2-T: static check of admitted handlers ----------------------------------------------------

FORBIDDEN_NAMES = {"OWNER", "HUB_OWNER_ACT"}
FORBIDDEN_CALLS = {"grant", "overleaf_push_grant", "approval_decide", "approval_revoke"}


def _route_of(decorator: ast.expr) -> str | None:
    if not (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and isinstance(decorator.func.value, ast.Name)
        and decorator.func.value.id == "app"
        and decorator.args
        and isinstance(decorator.args[0], ast.JoinedStr)
    ):
        return None
    parts = []
    for value in decorator.args[0].values:
        if isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name):
            parts.append("/api/v1" if value.value.id == "API" else "?")
        elif isinstance(value, ast.Constant):
            parts.append(str(value.value))
    return f"{decorator.func.attr.upper()} {''.join(parts)}"


def _is_owner_test(test: ast.expr) -> bool | None:
    """True for ``_is_owner(...)``, False for ``not _is_owner(...)``, None otherwise."""
    if isinstance(test, ast.Call) and getattr(test.func, "id", None) == "_is_owner":
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _is_owner_test(test.operand)
        return None if inner is None else not inner
    return None


def _owner_uses(stmts: list[ast.stmt], owner: bool) -> list[str]:
    """Owner-only references made outside an owner branch of an admitted handler."""
    found: list[str] = []
    for stmt in stmts:
        if isinstance(stmt, ast.If) and _is_owner_test(stmt.test) is not None:
            positive = _is_owner_test(stmt.test)
            found += _owner_uses(stmt.body, owner or bool(positive))
            found += _owner_uses(stmt.orelse, owner or not positive)
            if (
                positive is False
                and stmt.body
                and isinstance(stmt.body[-1], ast.Return | ast.Raise)
            ):
                owner = True  # the rest of the handler is the owner's path
            continue
        if owner:
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                found.append(f"{node.id} at line {node.lineno}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in FORBIDDEN_CALLS
                and getattr(node.value, "id", None) == "service"
            ):
                found.append(f"service.{node.attr} at line {node.lineno}")
    return found


def _admitted_handler_problems(source: str) -> dict[str, list[str]]:
    problems: dict[str, list[str]] = {}
    seen: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            route = _route_of(decorator)
            if route in AGENT_ROUTES:
                seen.add(route)
                uses = _owner_uses(node.body, False)
                if uses:
                    problems[route] = uses
    # CP10.6 (decision 0022): routes generated from the agent operation table are not
    # decorated handlers; they are guarded by test_generated_agent_routes_never_use_owner_state.
    from prometheos import agentops

    agentops.load()
    generated = {a.http for a in agentops.OPS.values() if a.http}
    missing = AGENT_ROUTES - seen - generated
    assert not missing, f"admitted routes without a handler found by the scan: {missing}"
    return problems


def test_admitted_handlers_use_the_owner_only_in_owner_branches() -> None:
    source = (REPO / "src" / "prometheos" / "hub" / "app.py").read_text(encoding="utf-8")
    assert _admitted_handler_problems(source) == {}


def test_generated_agent_routes_never_use_owner_state() -> None:
    """The agent operation table (MCP family tools and the hub's generated routes) is shared by
    agent callers and runs the transport's caller: no handler may name the owner caller, the
    hub's owner act, an owner decision or a grant (CP10.6, decision 0022 item 3)."""
    package = REPO / "src" / "prometheos" / "agentops"
    names = ("OwnerAct", "HUB_OWNER_ACT", "approval_decide", "approval_revoke", "full_data")
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN_NAMES | set(names), (path.name, node.id)
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_CALLS | set(names), (path.name, node.attr)
            if isinstance(node, ast.Constant) and node.value == "owner":
                raise AssertionError(f"{path.name} builds an owner scope at line {node.lineno}")
    # The generated-route closure in the hub uses the request's caller, never a fixed owner.
    hub = (REPO / "src" / "prometheos" / "hub" / "app.py").read_text(encoding="utf-8")
    start = hub.index("def _add_agent_route")
    end = hub.index("agentops.load()", start)
    closure = hub[start:end]
    assert "_agent_context(request)" in closure
    assert "OWNER" not in closure.replace("_is_owner", "") and "HUB_OWNER_ACT" not in closure
    context = hub[hub.index("def _agent_context") : start]
    assert "_who(request)" in context and "paired = _is_owner(request)" in context
    assert "authority=HUB_OWNER_ACT if paired else None" in context


def test_the_static_check_bites() -> None:
    source = (REPO / "src" / "prometheos" / "hub" / "app.py").read_text(encoding="utf-8")
    mutated = source.replace(
        "service.version_text(project_id, _current(), version_id, offset, limit)",
        "service.version_text(project_id, OWNER, version_id, offset, limit)",
    )
    assert mutated != source
    assert "GET /api/v1/versions/{version_id}/text" in _admitted_handler_problems(mutated)


# ----- N17, N15, N5, N12 ------------------------------------------------------------------------


def test_an_agent_cannot_stop_a_recording_of_an_owner_rank_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cp1_support import Env

    from prometheos.audio import capture, engines

    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    env = Env(tmp_path)
    params = {"title": "Owner meeting", "device_ids": ["synthetic-mic"], "sensitivity": "normal"}
    err = refused(
        lambda: env.svc.approval_request(env.project_id, OWNER, "recording_start", params)
    )
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    started = env.svc.approval_start_recording(
        env.project_id,
        approval_id,
        owner=OWNER_ACT,
        plan_sha256=err.details["plan_sha256"],
        backend=capture.SyntheticBackend(),
        start_capture=False,
    )
    assert started["result"]["session_id"]
    out = refused(lambda: env.svc.approval_stop(env.project_id, AGENT, approval_id))
    assert out.code == "not_found"
    owner_stop = env.svc.approval_stop(env.project_id, OWNER, approval_id, cancel=True)
    assert owner_stop["session_id"] == started["result"]["session_id"]


def test_the_service_refuses_a_recording_start_without_an_owner_act(tmp_path: Path) -> None:
    from cp1_support import Env

    env = Env(tmp_path)
    params = {"title": "Lab", "device_ids": ["synthetic-mic"]}
    err = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "recording_start", params)
    )
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    for pretender in (None, OWNER, "hub-owner"):
        out = refused(
            lambda p=pretender: env.svc.approval_start_recording(
                env.project_id,
                approval_id,
                owner=p,
                plan_sha256=err.details["plan_sha256"],
                start_capture=False,
            )
        )
        assert out.code == "policy_denied"
    assert rows(env, "SELECT state FROM approval_requests")[0]["state"] == "granted"


def _push_request(tmp_path: Path) -> tuple[Any, str]:
    from test_cp9_sync import Linked

    linked = Linked(tmp_path)
    main = linked.folder / "main.tex"
    main.write_text(main.read_text("utf-8") + "% pushed change\n", "utf-8", newline="\n")
    (linked.folder / "extra.tex").write_text("% a new section\n", "utf-8", newline="\n")
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "A change to push")
    preview = linked.svc.overleaf_push_preview(linked.pid, AGENT, linked.paper_id)
    err = refused(
        lambda: linked.svc.approval_request(
            linked.pid,
            AGENT,
            "overleaf_push",
            {"paper_id": linked.paper_id, "digest": preview["digest"]},
        )
    )
    return linked, err.details["approval_request_id"]


def test_a_file_restricted_after_the_grant_stops_the_push(tmp_path: Path) -> None:
    """N5: the CP9 digest binds commits only; the owner-view recheck alone notices that a
    pushed file became restricted after the owner granted the push."""
    from test_cp9_review_cp9a_r2 import _restrict_in_clone
    from test_cp9_sync import git

    linked, approval_id = _push_request(tmp_path)
    grant(linked.env, approval_id)
    head = git(linked.remote.bare, "rev-parse", "master")
    _restrict_in_clone(linked, "extra.tex")  # pushed, and unregistered until now
    out = refused(lambda: linked.svc.approval_resume(linked.pid, AGENT, approval_id))
    assert out.code == "revision_conflict"
    assert git(linked.remote.bare, "rev-parse", "master") == head
    assert rows(linked.env, "SELECT state FROM approval_requests")[0]["state"] == "granted"


def test_an_unrelated_uncommitted_edit_keeps_the_push_grant(tmp_path: Path) -> None:
    """R2-2: uncommitted local edits are never pushed, so they do not void the grant."""
    from test_cp9_sync import git

    linked, approval_id = _push_request(tmp_path)
    grant(linked.env, approval_id)
    other = linked.folder / "sections" / "notes-draft.tex"
    other.parent.mkdir(exist_ok=True)
    other.write_text("% unrelated, not committed\n", "utf-8", newline="\n")
    done = linked.svc.approval_resume(linked.pid, AGENT, approval_id)
    assert done["result"]["pushed"]
    assert "pushed change" in git(linked.remote.bare, "show", "master:main.tex")


def test_the_owner_view_binding_is_keyed(tmp_path: Path) -> None:
    """R2-1: the agent sees a keyed MAC; it cannot test guesses of the owner's view."""
    from prometheos.app import approval_kinds, approvals

    linked, approval_id = _push_request(tmp_path)
    plan = linked.svc.approval_get(linked.pid, AGENT, approval_id)["plan"]
    assert "owner_view_sha256" not in plan
    view = approval_kinds._push_owner_view(linked.svc, linked.pid, linked.paper_id)
    binding = approval_kinds._push_binding(view)
    assert plan["owner_view_mac"] != canonical_sha256(binding)  # not an unkeyed digest
    key = approvals.binding_key(linked.svc.home)
    assert plan["owner_view_mac"] == approvals.keyed_digest(key, binding)
    assert plan["owner_view_mac"] != approvals.keyed_digest(b"x" * 32, binding)
    assert len(key) == 32


def test_the_token_file_is_restricted_before_the_token_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N12 / R2-6: hub serve restricts the empty file first; a failure refuses to start."""
    import uvicorn
    from typer.testing import CliRunner

    from prometheos import cli
    from prometheos.store import private_files

    sizes: dict[str, int] = {}

    def restricting(path: Path) -> bool:
        sizes[path.name] = path.stat().st_size
        return True

    monkeypatch.setattr(private_files, "restrict_to_owner", restricting)

    def no_serving(self: Any, sockets: Any = None) -> None:
        for sock in sockets or []:
            sock.close()

    monkeypatch.setattr(uvicorn.Server, "run", no_serving)
    from cp1_support import Env

    env = Env(tmp_path)
    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    token_file = tmp_path / "agent.token"
    runner = CliRunner()
    done = runner.invoke(
        cli.app,
        ["hub", "serve", "--agent-token-file", str(token_file), "--project", env.project_id],
    )
    assert done.exit_code == 0, done.output
    # Both secrets are restricted while still empty: the agent token and, since A07 (CP21),
    # the hub's owner-channel secret.
    assert sizes == {"agent.token": 0, "owner.secret": 0}
    assert token_file.read_text("utf-8").startswith("pagt_")
    monkeypatch.setattr(private_files, "restrict_to_owner", lambda path: False)
    refused_file = tmp_path / "agent2.token"
    failed = runner.invoke(
        cli.app,
        ["hub", "serve", "--agent-token-file", str(refused_file), "--project", env.project_id],
    )
    assert failed.exit_code != 0 and not refused_file.exists()


def test_private_files_are_created_once_and_restricted(tmp_path: Path) -> None:
    import os

    from prometheos.store.private_files import write_private

    secret = tmp_path / "keys" / "k.bin"
    write_private(secret, b"abc")
    assert secret.read_bytes() == b"abc"
    if os.name != "nt":
        assert (secret.stat().st_mode & 0o077) == 0
    with pytest.raises(FileExistsError):
        write_private(secret, b"other")
    assert secret.read_bytes() == b"abc"


# ----- R2-3: the CLI start prompt escapes agent text ---------------------------------------------


def test_the_cli_start_prompt_escapes_agent_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cp1_support import Env
    from typer.testing import CliRunner

    from prometheos import cli

    env = Env(tmp_path)
    hostile = "Lab\nEffects: none\x1b[2J\u202eevil"
    params = {"title": hostile, "device_ids": ["mic\u200b\nfake"]}
    err = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "recording_start", params)
    )
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    shown: list[str] = []

    def capture_prompt(summary: str, purpose: str = "") -> None:
        shown.append(summary)
        raise PrometheosError("policy_denied", "stopped by the test", {})

    monkeypatch.setattr(cli, "_confirm_owner", capture_prompt)
    monkeypatch.setenv("PROMETHEOS_HOME", str(env.home))
    done = CliRunner().invoke(
        cli.app, ["approval", "resume", approval_id, "--project", env.project_id]
    )
    assert done.exit_code == 4
    (prompt,) = shown
    assert "\n" not in prompt and "\x1b" not in prompt and "\u202e" not in prompt
    assert "\u200b" not in prompt and prompt.isascii()
    assert "\\x1b" in prompt and "\\u202e" in prompt


def test_escaped_text_shows_every_unsafe_character() -> None:
    from prometheos.cli import _escaped_text

    assert _escaped_text("a\nb\x1b\u202e\u200bc") == "a\\nb\\x1b\\u202e\\u200bc"
    assert re.fullmatch(r"[ -~]*", _escaped_text("Ünïcödé \t\r"))


def test_only_an_owner_act_is_a_capture_owner(tmp_path: Path) -> None:
    """R2-7: a string, a caller or any other value never stands in for the owner's act."""
    from cp1_support import Env

    env = Env(tmp_path)
    params = {"title": "Lab", "device_ids": ["synthetic-mic"]}
    err = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "recording_start", params)
    )
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    for pretender in ("hub-owner", "cli-interactive", OWNER, {"channel": "hub-owner"}, True):
        out = refused(
            lambda p=pretender: env.svc.approval_resume(
                env.project_id, AGENT, approval_id, start_capture=False, capture_owner=p
            )
        )
        assert out.code == "policy_denied"
    assert rows(env, "SELECT state FROM approval_requests")[0]["state"] == "granted"
