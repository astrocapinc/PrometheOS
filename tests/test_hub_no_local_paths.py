"""No hub GET route ever returns an absolute local path (decision 0006 item 19; integration
review CP5 INT1-M3, decision 0012 "Integration review fixes").

The routes are enumerated from the application itself, so a route added later is walked
automatically. The fixture's home, store, research root and engine folder all sit under a
directory whose name is a recognisable marker; no response may contain it.

Integration review CP5 INT2-M2 (decision 0012 item 46): the same holds for every POST, PUT
and DELETE route, in its error envelopes (validation errors, stale digests, not_found,
policy_denied) and its cheap success paths, and for any body a route or handler produces,
because the hub redacts every JSON body in one middleware.

CP10 integration review INT-CP10-M1 (decision 0019 "Round 2"): both walkers seed real
operation runs (an owner engine install from a local archive under the marker, an import and
a compile) and a real approval request, so the runs and approvals routes answer real data.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from cp9_support import LatexEnv
from hub_support import Hub, Reply, running_hub

from prometheos.app import texengine
from prometheos.domain.models import PrometheosError
from prometheos.hub.app import API, create_app
from prometheos.hub.redaction import Redactor, normalise_roots
from prometheos.hub.security import Gate

MARKER = "PathMarkerQ7Z"


def _get_routes(service: object, project_id: str) -> list[str]:
    app = create_app(service, project_id, Gate(port=1))
    return sorted(
        r.path  # type: ignore[attr-defined]
        for r in app.routes
        if "GET" in getattr(r, "methods", set()) and r.path.startswith(API)  # type: ignore[attr-defined]
    )


def _seed_runs(ws: LatexEnv) -> tuple[list[str], str]:
    """Real operation runs and a real approval request, every one carrying the marker where it
    can: an owner engine install from a local archive (its stored parameters keep the
    archive's absolute path; the archive is not the pinned release, so nothing is installed and
    nothing is downloaded), an import plan and its commit of a research file, a queued compile
    (no engine is installed) and an agent's engine-install request awaiting the owner."""
    svc, pid = ws.svc, ws.pid
    run_ids: list[str] = []
    archive = ws.env.tmp / "Owner Downloads" / "tectonic.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"not the pinned release")
    digest = svc.tex_engine_plan()["plan_digest"]
    with pytest.raises(PrometheosError) as install:
        svc.tex_engine_install(
            digest, archive=archive, authority=OWNER_ACT, project_id=pid, caller=OWNER,
            mode="wait",
        )  # fmt: skip
    run_ids.append(install.value.details["run_id"])
    note = ws.env.research / "notes" / "run note.txt"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("A synthetic note for the run walk.\n", encoding="utf-8")
    plan = svc.import_plan(pid, OWNER, root_alias="research", paths=["notes/run note.txt"])
    committed = svc.import_commit(pid, plan["plan_id"], OWNER)
    run_ids += [plan["run_id"], committed["run_id"]]
    # No engine is installed, so the compile is created queued and not executed here.
    queued = svc.run_start(
        pid, OWNER, "paper_compile", {"paper_id": ws.paper_id, "fetch": False}, mode="none"
    )
    run_ids.append(queued["run_id"])
    with pytest.raises(PrometheosError) as asked:
        svc.approval_request(pid, AGENT, "tex_engine_install", {"digest": digest})
    assert asked.value.code == "approval_required"
    with closing(sqlite3.connect(ws.env.store_dir / "record.sqlite")) as conn:
        stored = conn.execute(
            "SELECT params FROM operation_runs WHERE id = ?", (run_ids[0],)
        ).fetchone()[0]
    assert MARKER in stored  # the raw value exists server-side: the walk proves it is not shown
    return run_ids, str(asked.value.details["approval_request_id"])


def _library_work(ws: Any) -> str:
    """One synthetic PDF imported into the library (its file lies under the marked root)."""
    from synthetic_pdf import DRAG, paper

    target = ws.env.research / "papers" / "drag.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(paper(DRAG))
    svc, pid = ws.svc, ws.pid
    plan = svc.library_import_plan(pid, OWNER, root_alias="research", path="papers")
    svc.library_import_commit(pid, plan["plan_id"], OWNER)
    return str(svc.library_works(pid, OWNER)["works"][0]["work_id"])


def _matrix(ws: Any, work_id: str) -> str:
    """CP11 S11.4: a matrix over that work, with a rule-extracted row (an evidence span)."""
    svc, pid = ws.svc, ws.pid
    columns = [{"id": "finding", "label": "Finding", "value_kind": "text", "instructions": ""}]
    made = svc.matrix_create(pid, OWNER, title="Paths", columns=columns, work_ids=[work_id])
    rules = {"rules": [{"id": "f", "column": "finding", "keywords": ["drag coefficient"]}]}
    svc.matrix_extract(pid, OWNER, matrix_id=made["matrix_id"], rules=rules)
    return str(made["matrix_id"])


def test_no_get_route_returns_a_local_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (texengine.ENGINE_DIR_ENV, "PROMETHEOS_MODELS_DIR"):
        monkeypatch.delenv(name, raising=False)  # every folder under the marked home
    base = tmp_path / MARKER
    base.mkdir()
    ws = LatexEnv(Env(base))
    svc, pid = ws.svc, ws.pid
    assert MARKER in str(ws.env.home) and MARKER in str(ws.env.store_dir)
    meeting = svc.capture_meeting(
        pid,
        OWNER,
        title="Synthetic meeting",
        text="Supervisor asked for the plot.\n",
        held_at=None,
        request_id="5f0f6a0e-0000-4000-8000-000000000001",
    )
    paper = ws.paper()
    source_id = paper["body"]["manuscript_source_id"]
    versions = svc.list_records(pid, OWNER, kind="version", limit=200)["items"]
    version_id = next(v["id"] for v in versions if v["body"]["source_id"] == source_id)
    run_ids, approval_id = _seed_runs(ws)
    values = {
        "record_id": [ws.paper_id, source_id, version_id, meeting["meeting_id"]],
        "meeting_id": [meeting["meeting_id"]],
        "version_id": [version_id, meeting["version_id"]],
        "paper_id": [ws.paper_id],
        "task_id": ["task_00000000-0000-4000-8000-000000000009"],
        "run_id": [*run_ids, "run_00000000-0000-4000-8000-000000000009"],
        "session_id": ["00000000-0000-4000-8000-000000000009"],
        "approval_id": [approval_id, "apreq_00000000-0000-4000-8000-000000000009"],
        "ref": ["capture-research-evidence"],
        # CP10.6 (decision 0022): routes generated from the agent operation table
        "job_id": ["job_00000000-0000-4000-8000-000000000009"],
        "name": ["entry_body", "no_such_schema"],
        # A05, CP17 (decision 0039): adoption scan generations
        "scan_gen": ["1", "999999"],
        "family_id": ["fam_0123456789abcdef"],
        "baseline_id": ["bl_000000000000000000000000"],  # CP17.5 (decision 0043)
        # CP11 S11.1 (decision 0060): a work imported from a PDF in the marked research root
        "work_id": [_library_work(ws), "work_00000000-0000-4000-8000-000000000009"],
    }
    # CP11 S11.4 (decision 0064): a matrix over that work
    values["matrix_id"] = [
        _matrix(ws, values["work_id"][0]),
        "matrix_00000000-0000-4000-8000-000000000009",
    ]
    queries = {
        f"{API}/transcripts/{{version_id}}/span": "?start_ms=0&end_ms=1000",
        f"{API}/papers/{{paper_id}}/handoff/plan": "?root_alias=research&folder=paper",
        f"{API}/knowledge/search": "?q=Synthetic",
        f"{API}/papers/{{paper_id}}/workspace/file": "?path=main.tex",
        f"{API}/papers/{{paper_id}}/synctex/forward": "?file=main.tex&line=1",
        f"{API}/papers/{{paper_id}}/synctex/inverse": "?page=1&x=10&y=10",
        f"{API}/changes": "?wait=0",
        f"{API}/library/search": "?query=drag",
    }
    routes = _get_routes(svc, pid)
    walked: dict[str, set[int]] = {}
    with running_hub(svc, pid) as hub:
        hub.pair()
        for route in routes:
            params = re.findall(r"{(\w+)}", route)
            missing = [p for p in params if p not in values]
            assert not missing, f"no fixture value for {missing} in {route}: extend the walker"
            urls = [route]
            for name in params:
                urls = [
                    u.replace(f"{{{name}}}", quote(v, safe="")) for u in urls for v in values[name]
                ]
            for url in urls:
                reply = hub.request("GET", url + queries.get(route, ""), timeout=60)
                walked.setdefault(route, set()).add(reply.status)
                text = reply.body.decode("utf-8", "replace")
                assert MARKER.casefold() not in text.casefold(), f"{url} returned a local path"
                if route.startswith((f"{API}/runs", f"{API}/approvals")):
                    _assert_clean(url, reply)  # any local path at all (INT-CP10-M1)
    assert set(walked) == set(routes)
    # The walk reached real data, not only errors.
    ok = {route for route, statuses in walked.items() if 200 in statuses}
    assert f"{API}/tex/engine" in ok and f"{API}/papers/{{paper_id}}/workspace/files" in ok
    for real in ("/runs", "/runs/{run_id}", "/approvals", "/approvals/{approval_id}"):
        assert f"{API}{real}" in ok, real
    assert len(ok) >= len(routes) * 2 // 3, sorted(set(routes) - ok)


def test_cli_still_shows_the_real_engine_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(texengine.ENGINE_DIR_ENV, raising=False)
    env = Env(tmp_path)
    assert env.svc.tex_engine_plan()["destination"] == str(texengine.engine_dir(env.home))
    shown = env.svc.tex_engine_plan(redact=True)
    assert shown["destination"] == "<PrometheOS home>/tools/tectonic-0.17.0"
    assert shown["plan_digest"] == env.svc.tex_engine_plan()["plan_digest"]


def test_mcp_engine_status_shows_a_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    import anyio
    from mcp import Client

    from prometheos.mcp_adapter import build_server

    monkeypatch.delenv(texengine.ENGINE_DIR_ENV, raising=False)
    base = tmp_path / MARKER
    base.mkdir()
    env = Env(base)

    async def main() -> str:
        async with Client(build_server(env.svc, env.project_id)) as client:
            reply = await client.call_tool("tex_engine_status", {})
            return str(reply.content[0].text)  # type: ignore[union-attr]

    text = anyio.run(main)
    assert MARKER not in text
    assert json.loads(text)["location"] == "<PrometheOS home>/tools/tectonic-0.17.0"


# ----- INT2-M2: every method, every status --------------------------------------------------

BOGUS_DIGEST = "0" * 64


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _local_markers() -> list[re.Pattern[str]]:
    """What may not appear in any string of a reply: a Windows drive or UNC path, the user's
    profile or temporary folder in any spelling, or the user name as a path segment."""
    user = re.escape(Path.home().name)
    found = [
        re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]"),
        re.compile(r"(?:^|[\s'\"(])\\\\[^\\\s]"),
        re.compile(rf"[\\/]{user}(?:[\\/]|$)|pytest-of-{user}", re.IGNORECASE),
    ]
    for root in {str(Path.home()), tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir())}:
        found.append(re.compile(re.escape(root.replace("\\", "/")), re.IGNORECASE))
        found.append(re.compile(re.escape(root.replace("/", "\\")), re.IGNORECASE))
    return found


def _assert_clean(label: str, reply: Reply) -> None:
    text = reply.body.decode("utf-8", "replace")
    assert MARKER.casefold() not in text.casefold(), f"{label} returned a local path: {text}"
    assert reply.headers.get("content-type", "").startswith("application/json"), label
    markers = _local_markers()
    for item in _strings(json.loads(text)):
        for marker in markers:
            assert not marker.search(item), f"{label} returned a local path: {item!r}"


def _mutation_routes(service: object, project_id: str) -> list[tuple[str, str]]:
    app = create_app(service, project_id, Gate(port=1))
    return sorted(
        (method, r.path)  # type: ignore[attr-defined]
        for r in app.routes
        if r.path.startswith(API)  # type: ignore[attr-defined]
        for method in getattr(r, "methods", set())
        if method not in ("GET", "HEAD")
    )


@pytest.fixture
def marked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LatexEnv:
    for name in (texengine.ENGINE_DIR_ENV, "PROMETHEOS_MODELS_DIR"):
        monkeypatch.delenv(name, raising=False)
    base = tmp_path / MARKER
    base.mkdir()
    ws = LatexEnv(Env(base))
    assert MARKER in str(ws.env.home) and MARKER in str(ws.folder)
    # No Overleaf route can reach a network: the workspace is not a Git clone, so every Git
    # route stops before any fetch or push, and the link route only ever gets invalid bodies.
    assert not (ws.folder / ".git").exists()
    return ws


def test_engine_install_stale_digest_shows_no_local_path(marked: LatexEnv) -> None:
    """The INT2-M2 reproduction: a stale plan digest answers 409 with the current plan."""
    with running_hub(marked.svc, marked.pid) as hub:
        hub.pair()
        reply = hub.request("POST", f"{API}/tex/engine/install", {"digest": BOGUS_DIGEST})
    assert reply.status == 409, reply.body
    error = reply.json()["error"]
    assert error["code"] == "revision_conflict"
    plan = error["details"]["plan"]
    assert plan["destination"] == "<PrometheOS home>/tools/tectonic-0.17.0"
    assert plan["plan_digest"] == marked.svc.tex_engine_plan()["plan_digest"]
    _assert_clean("POST /tex/engine/install (stale digest)", reply)


def _bodies(ws: LatexEnv, hub: Hub, version_id: str) -> dict[tuple[str, str], list[Any]]:
    """Route-specific bodies beyond the generic ``{}`` and malformed JSON: realistic error
    paths (stale digests, unknown ids, refused paths) and cheap success paths. Nothing here
    can download, record, call a provider or reach a remote."""
    p = f"{API}/papers/{{paper_id}}"
    current = hub.request("GET", f"{API}/papers/{ws.paper_id}/workspace/file?path=main.tex")
    sha = current.json()["sha256"]
    text = current.json()["text"]
    unknown_version = "version_00000000-0000-4000-8000-000000000009"
    return {
        ("POST", f"{API}/pair"): [{"token": "wrong"}],
        ("POST", f"{API}/tex/engine/install"): [{"digest": BOGUS_DIGEST}],
        ("POST", f"{API}/transactions"): [{"request_id": "x", "ops": []}],
        ("POST", f"{API}/decisions/preview"): [{"request": {"ops": []}}],
        ("POST", f"{API}/decisions/apply"): [{"request": {"ops": []}, "digest": BOGUS_DIGEST}],
        ("POST", f"{API}/meetings"): [{"title": "Synthetic", "text": "Plot requested.\n"}],
        ("POST", f"{API}/tasks/prepare"): [{"purpose": "no-such-purpose"}],
        ("POST", f"{API}/tasks/{{task_id}}/submit"): [{"result": {}, "request_id": "r"}],
        ("POST", f"{API}/sources/plans"): [
            {"root_alias": "research", "paths": ["paper/main.tex"]},
            {"root_alias": "research", "paths": ["paper/missing.txt"]},
            {"root_alias": "research", "paths": ["../outside.txt"]},
            {"root_alias": "nope", "paths": ["a.txt"]},
        ],
        ("POST", f"{p}/workspace/bind"): [
            {"root_alias": "research", "main": "paper/main.tex"},
            {"root_alias": "research", "main": "missing/main.tex"},
        ],
        ("PUT", f"{p}/workspace/file"): [
            {"path": "main.tex", "text": "x", "base_sha256": BOGUS_DIGEST},
            {"path": "../escape.tex", "text": "x", "base_sha256": None},
            {"path": "main.tex", "text": text, "base_sha256": sha},
        ],
        ("POST", f"{p}/workspace/snapshot"): [{"paths": ["main.tex"]}, {"paths": ["nope.tex"]}],
        ("POST", f"{p}/workspace/insert"): [
            {"kind": "cite", "version_id": unknown_version},
            {"kind": "figure", "version_id": version_id},
        ],
        ("POST", f"{p}/compile"): [{"fetch": False}],
        ("POST", f"{p}/overleaf/pull/preview"): [{"fetch": False}],
        ("POST", f"{p}/overleaf/pull/apply"): [{"digest": BOGUS_DIGEST}],
        ("POST", f"{p}/overleaf/commit"): [{"message": "synthetic"}],
        ("POST", f"{p}/overleaf/push/apply"): [{"digest": BOGUS_DIGEST}],
        ("POST", f"{API}/audio/import"): [
            {"root_alias": "research", "path": "paper/main.tex"},
            {"root_alias": "research", "path": "missing.wav"},
        ],
        ("POST", f"{API}/audio/transcriptions"): [{"version_id": version_id}],
        ("POST", f"{API}/transcripts/{{version_id}}/corrections"): [
            {"edits": [{"segment_id": "seg-00001", "text": "x"}]}
        ],
        ("POST", f"{API}/meetings/from-audio"): [
            {"transcript_version_id": version_id, "title": "Synthetic"}
        ],
        ("POST", f"{API}/provider/preview"): [{"purpose": "no-such-purpose"}],
        ("POST", f"{API}/provider/configure/preview"): [{"config": {"provider": "nope"}}],
        # CP10.6 (decision 0022): research-root operands, resolved without leaving the root
        ("POST", f"{API}/skills/inspect"): [
            {"root_alias": "research", "path": "paper"},
            {"root_alias": "research", "path": "../outside"},
            {"root_alias": "nope", "path": "paper"},
        ],
        ("POST", f"{API}/skills/validate"): [{"root_alias": "research", "path": "paper"}],
        ("POST", f"{API}/skills/import-plan"): [{"root_alias": "research", "path": "paper"}],
        ("POST", f"{API}/host/setup"): [{"root_alias": "research", "folder": "paper"}],
        ("POST", f"{API}/skills/create"): [
            {"name": "synthetic-skill", "description": "A skill.", "root_alias": "research"}
        ],
        ("POST", f"{p}/export"): [
            {"root_alias": "research", "path": "paper/bundle.zip"},
            {"root_alias": "research", "path": "missing/bundle.zip"},
        ],
        ("POST", f"{p}/handoff/apply"): [
            {"root_alias": "research", "folder": "paper", "digest": BOGUS_DIGEST}
        ],
        ("POST", f"{API}/audio/models/install"): [{"plan_sha256": BOGUS_DIGEST}],
    }


# Sent only the generic invalid bodies: a valid body could open an audio device, call a
# provider or reach a remote. Logout is walked last because it ends the session.
_GENERIC_ONLY = {
    f"{API}/recordings",
    f"{API}/provider/run",
    f"{API}/provider/configure",
    f"{API}/papers/{{paper_id}}/overleaf/link",
}


def test_no_mutation_route_returns_a_local_path(marked: LatexEnv) -> None:
    ws, svc, pid = marked, marked.svc, marked.pid
    source_id = ws.paper()["body"]["manuscript_source_id"]
    versions = svc.list_records(pid, OWNER, kind="version", limit=200)["items"]
    version_id = next(v["id"] for v in versions if v["body"]["source_id"] == source_id)
    # Real runs and a real approval (INT-CP10-M1). The walk sends only invalid or generic
    # bodies to the approval routes (no plan digest): nothing is granted, started or resumed.
    run_ids, approval_id = _seed_runs(ws)
    values = {
        "paper_id": [ws.paper_id, "paper_00000000-0000-4000-8000-000000000009"],
        "version_id": [version_id, "version_00000000-0000-4000-8000-000000000009"],
        "task_id": ["task_00000000-0000-4000-8000-000000000009"],
        "run_id": [*run_ids, "run_00000000-0000-4000-8000-000000000009"],
        "session_id": ["00000000-0000-4000-8000-000000000009"],
        "plan_id": ["plan_00000000-0000-4000-8000-000000000009", "not-a-plan"],
        "notification_id": ["999999", "not-a-number"],
        "action": ["stop", "recover", "no-such-action"],
        "ref": ["capture-research-evidence", "no-such-skill"],
        # CP10 routes (decision 0019): unknown IDs; the walk sends no valid owner decision.
        "approval_id": [approval_id, "apreq_00000000-0000-4000-8000-000000000009"],
        "target_id": ["claim_00000000-0000-4000-8000-000000000009"],
        "execution_id": ["exec_00000000-0000-4000-8000-000000000009"],
        "job_id": ["job_00000000-0000-4000-8000-000000000009"],
        # A05, CP17 (decision 0042): adoption assist routes of an unknown scan
        "scan_gen": ["999999", "not-a-number"],
        # CP11 S11.4 (decision 0064): a real matrix over a library work, and an unknown one
        "matrix_id": [
            _matrix(ws, _library_work(ws)),
            "matrix_00000000-0000-4000-8000-000000000009",
        ],
    }
    routes = _mutation_routes(svc, pid)
    assert ("PUT", f"{API}/papers/{{paper_id}}/workspace/file") in routes
    routes.sort(key=lambda route: route[1] == f"{API}/logout")
    walked: dict[tuple[str, str], set[int]] = {}
    with running_hub(svc, pid) as hub:
        hub.pair()
        specific = _bodies(ws, hub, version_id)
        unknown = set(specific) - set(routes)
        assert not unknown, f"bodies for routes that do not exist: {unknown}"
        # The guard's own refusals: no CSRF token, no session.
        method, route = routes[0]
        _assert_clean("no csrf", hub.request(method, route, {}, csrf=None))
        _assert_clean("no session", hub.request(method, route, {}, cookie=None))
        for method, route in routes:
            params = re.findall(r"{(\w+)}", route)
            missing = [p for p in params if p not in values]
            assert not missing, f"no fixture value for {missing} in {route}: extend the walker"
            urls = [route]
            for name in params:
                urls = [
                    u.replace(f"{{{name}}}", quote(v, safe="")) for u in urls for v in values[name]
                ]
            bodies: list[Any] = [{}, b"{not json"]
            if route not in _GENERIC_ONLY:
                bodies += specific.get((method, route), [])
            for url in urls:
                for body in bodies:
                    reply = hub.request(method, url, body, timeout=60)
                    walked.setdefault((method, route), set()).add(reply.status)
                    _assert_clean(f"{method} {url} {body!r}", reply)
    assert set(walked) == set(routes)
    statuses = set().union(*walked.values())
    # The walk reached every kind of reply the claim is about.
    assert {200, 400, 403, 404, 409} <= statuses, sorted(statuses)
    assert 200 in walked[("PUT", f"{API}/papers/{{paper_id}}/workspace/file")]
    assert 409 in walked[("POST", f"{API}/tex/engine/install")]


def _redactor_for(home: Path, research: Path) -> Redactor:
    return Redactor(
        lambda: normalise_roots([(home, "<PrometheOS home>")]),
        lambda: normalise_roots([(research, "<research root research>")]),
    )


def test_redaction_rules(tmp_path: Path) -> None:
    home = tmp_path / MARKER / "home"
    research = tmp_path / MARKER / "research"
    redactor = _redactor_for(home, research)
    engine = str(home / "tools" / "tectonic-0.17.0")
    outside = str(research / "data" / "run1.csv")
    note = f"See {outside} for the raw run.\nAnd {engine}."
    success = {
        "destination": engine,  # path-like name: dropped
        "previous_store": outside,  # path-like suffix: dropped
        "written": [outside, "relative/kept.txt"],
        "plan": {"anything": engine},  # not a path-like name: content, kept (P2-L1)
        "path": "paper/main.tex",  # relative: kept
        "body": {"text": note},  # researcher content: untouched
        "error": f"OSError: [Errno 2] No such file: {outside!r}",  # diagnostic: rewritten
    }
    shown = redactor.body(success, envelope=False)
    assert "destination" not in shown and "previous_store" not in shown
    assert shown["written"] == ["relative/kept.txt"]
    assert shown["plan"]["anything"] == engine
    assert shown["path"] == "paper/main.tex"
    assert shown["body"]["text"] == note
    assert MARKER not in shown["error"] and "<research root research>" in shown["error"]
    envelope = {
        "ok": False,
        "error": {
            "message": f"could not place the model: [WinError 5] denied: {engine!r}",
            "details": {"unexpected_key": f"copy at {outside} failed", "other": "D:\\x\\y z"},
        },
    }
    scrubbed = json.dumps(redactor.body(envelope, envelope=True))
    assert MARKER not in scrubbed and "D:" not in scrubbed
    assert "<PrometheOS home>" in scrubbed and "<local path>" in scrubbed
    # A diagnostic value that is wholly a PrometheOS folder keeps its home-relative layout.
    whole = redactor.body({"ok": False, "error": {"details": {"at": engine}}}, envelope=True)
    assert whole["error"]["details"]["at"] == "<PrometheOS home>/tools/tectonic-0.17.0"


def test_no_route_or_handler_bypasses_the_redaction(marked: LatexEnv) -> None:
    """A route that returns its own JSONResponse, raises with an unexpected key or crashes is
    still redacted: the redaction sits in the middleware, not in the route helpers."""
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from prometheos.hub.security import CSRF_HEADER

    home = str(marked.env.home)
    gate = Gate(port=1)
    app = create_app(marked.svc, marked.pid, gate)

    def raw() -> JSONResponse:
        return JSONResponse({"store": home, "nested": [{"target_dir": home}], "remote_url": home})

    def raises() -> None:
        raise PrometheosError("io_error", f"cannot write {home}", {"made_up_key": home})

    def crashes() -> None:
        raise RuntimeError(home)

    def other_shape() -> JSONResponse:  # an error body that is not the standard envelope
        return JSONResponse({"detail": f"cannot open {home}"}, status_code=418)

    app.add_api_route(f"{API}/test-raw", raw, methods=["POST"])
    app.add_api_route(f"{API}/test-raises", raises, methods=["DELETE"])
    app.add_api_route(f"{API}/test-crashes", crashes, methods=["PUT"])
    app.add_api_route(f"{API}/test-other-shape", other_shape, methods=["PATCH"])
    origin = "http://127.0.0.1:1"
    with TestClient(app, base_url=origin, raise_server_exceptions=False) as client:
        token = gate.new_pairing_token()
        paired = client.post(f"{API}/pair", json={"token": token}, headers={"Origin": origin})
        assert paired.status_code == 200, paired.text
        headers = {"Origin": origin, CSRF_HEADER: paired.json()["csrf_token"]}
        seen = set()
        for method, path in (
            ("POST", "test-raw"),
            ("DELETE", "test-raises"),
            ("PUT", "test-crashes"),
            ("PATCH", "test-other-shape"),
        ):
            reply = client.request(method, f"{API}/{path}", headers=headers)
            seen.add(reply.status_code)
            assert MARKER not in reply.text, (path, reply.text)
            assert reply.headers["content-length"] == str(len(reply.content))
    assert seen == {200, 418, 500, 503}


# ----- CP9 INT2-F5: a paper whose workspace is a clone of a local bare repository -----------


GIT_WAIT = 60


def test_linked_clone_routes_return_no_local_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a local-path remote (accepted, and used by the e2e fixture), the Overleaf status
    and both previews used to return the remote's absolute path. Every GET route of the
    paper and every Overleaf route are walked against a real local clone. The remote is a
    bare repository in the marked folder: nothing reaches a network."""
    from test_cp9_sync import Linked

    for name in (texengine.ENGINE_DIR_ENV, "PROMETHEOS_MODELS_DIR"):
        monkeypatch.delenv(name, raising=False)
    base = tmp_path / MARKER
    base.mkdir()
    linked = Linked(base)
    assert MARKER in str(linked.remote.bare) and (linked.folder / ".git").is_dir()
    linked.remote.edit("main.tex", "settling time", "settling time (revised)", "Collaborator")
    svc, pid, paper_id = linked.svc, linked.pid, linked.paper_id
    p = f"{API}/papers/{paper_id}"
    queries = {
        f"{API}/papers/{{paper_id}}/workspace/file": "?path=main.tex",
        f"{API}/papers/{{paper_id}}/synctex/forward": "?file=main.tex&line=1",
        f"{API}/papers/{{paper_id}}/synctex/inverse": "?page=1&x=10&y=10",
    }
    gets = [r for r in _get_routes(svc, pid) if "{paper_id}" in r]
    statuses: dict[str, int] = {}
    with running_hub(svc, pid) as hub:
        hub.pair()
        for route in gets:
            url = route.replace("{paper_id}", paper_id) + queries.get(route, "")
            reply = hub.request("GET", url, timeout=60)
            statuses[route] = reply.status
            _assert_clean(f"GET {url}", reply)
        # Git-backed responses get the 60 s wait the e2e suite uses (decision 0028, browser R4):
        # on the owner's PC a local-transport git step can take ~30 s (decision 0024, session 7).
        overleaf = hub.request("GET", f"{p}/overleaf", timeout=GIT_WAIT)
        assert overleaf.status == 200, overleaf.body
        assert overleaf.json()["remote"]["url"] == "<local path>"
        for fetch in (False, True):  # fetch=True fetches from the local bare repository
            pull = hub.request(
                "POST", f"{p}/overleaf/pull/preview", {"fetch": fetch}, timeout=GIT_WAIT
            )
            assert pull.status == 200, pull.body
            assert pull.json()["plan"]["remote_url"] == "<local path>"
            _assert_clean(f"pull preview fetch={fetch}", pull)
        digest = pull.json()["digest"]
        for body in ({"digest": BOGUS_DIGEST}, {"digest": digest}):
            apply = hub.request("POST", f"{p}/overleaf/pull/apply", body, timeout=GIT_WAIT)
            _assert_clean("pull apply", apply)
        commit = hub.request("POST", f"{p}/overleaf/commit", {"message": "m"}, timeout=GIT_WAIT)
        _assert_clean("commit", commit)
        push = hub.request("POST", f"{p}/overleaf/push/preview", timeout=GIT_WAIT)
        assert push.status == 200, push.body
        assert push.json()["plan"]["remote_url"] == "<local path>"
        _assert_clean("push preview", push)
        for body in ({"digest": BOGUS_DIGEST}, {"digest": push.json()["digest"]}):
            pushed = hub.request("POST", f"{p}/overleaf/push/apply", body, timeout=GIT_WAIT)
            _assert_clean("push apply", pushed)
        link = {"url": str(linked.remote.bare), "root_alias": "research", "folder": "overleaf"}
        _assert_clean("relink", hub.request("POST", f"{p}/overleaf/link", link, timeout=GIT_WAIT))
    assert statuses[f"{API}/papers/{{paper_id}}/overleaf"] == 200
    assert statuses[f"{API}/papers/{{paper_id}}/workspace/files"] == 200


# ----- pre-check P2-L1 and P2-L2 ---------------------------------------------------------------


def test_researcher_content_is_never_rewritten(marked: LatexEnv) -> None:
    """P2-L1: a workspace file whose whole content is a path under the PrometheOS home
    round-trips byte-identically through the hub's read and save, and a note whose title and
    text are such a path comes back unchanged. The hub writes these values back, so a label
    there would corrupt the document."""
    from cp1_support import create_entry

    ws = marked
    inside = str(ws.env.home / "backups" / "note.txt")
    store = str(ws.env.store_dir / "exports" / "x.zip")
    entry = create_entry(title=inside, text=store)
    ws.env.apply([entry])
    url = f"{API}/papers/{ws.paper_id}/workspace/file"
    with running_hub(ws.svc, ws.pid) as hub:
        hub.pair()
        created = hub.request(
            "PUT", url, {"path": "pathonly.tex", "text": inside, "base_sha256": None}
        )
        assert created.status == 200, created.body
        read = hub.request("GET", f"{url}?path=pathonly.tex").json()
        assert read["text"] == inside
        saved = hub.request(
            "PUT",
            url,
            {"path": "pathonly.tex", "text": read["text"], "base_sha256": read["sha256"]},
        )
        assert saved.status == 200, saved.body
        note = hub.request("GET", f"{API}/records/{entry['id']}").json()
    assert ws.path("pathonly.tex").read_bytes() == inside.encode("utf-8")
    assert note["body"]["title"] == inside and note["body"]["text"] == store


def test_error_text_paths_with_spaces_are_consumed_whole(tmp_path: Path) -> None:
    """P2-L2: a path with spaces is redacted whole, quoted (to its closing quote) or not (to
    the end of the line), so no later segment, such as a user name, survives."""
    profile = tmp_path / "Jane Doe"
    redactor = Redactor(
        lambda: normalise_roots([(profile / "prometheos", "<PrometheOS home>")]),
        lambda: normalise_roots([(profile, "<user home>")]),
    )
    quoted = str(profile / "My Data" / "run 1.csv")
    messages = {
        f"cannot open {quoted!r}; retry": "cannot open '<user home>'; retry",
        "copy failed: D:\\Shared Data\\Jane Doe\\x.csv": "copy failed: <local path>",
        'git: "\\\\server\\share\\Jane Doe\\repo" gone\nnext': 'git: "<local path>" gone\nnext',
        "plain: owner/repo at 1:2": "plain: owner/repo at 1:2",
    }
    for message, expected in messages.items():
        shown = redactor.body({"ok": False, "error": {"message": message}}, envelope=True)
        assert shown["error"]["message"] == expected
        assert "Jane" not in shown["error"]["message"] and "Data" not in shown["error"]["message"]
