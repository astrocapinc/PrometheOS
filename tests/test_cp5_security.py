"""C05-SECURITY (scenario S13): an untrusted web origin targeting the loopback hub gets
nothing — unpaired reads, CSRF, hostile Host/Origin and script content are refused or
inert — while a properly paired same-origin session works."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cp1_support import Env, create_entry
from cp1_support import request as request_bytes
from hub_support import Hub, running_hub

from prometheos.domain.ids import new_id
from prometheos.hub.security import PAIRING_TTL, Gate


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def hub(env: Env) -> Iterator[Hub]:
    with running_hub(env.svc, env.project_id) as running:
        yield running


SECRET_TITLE = "CANARY-7f3a restricted lab result"


def _seed_secret(env: Env) -> str:
    from cp1_support import OWNER

    rid = new_id("entry")
    env.apply([create_entry(rid, sensitivity="restricted", title=SECRET_TITLE)], caller=OWNER)
    return rid


def test_unpaired_requests_get_nothing(env: Env, hub: Hub) -> None:
    rid = _seed_secret(env)
    for path in ("/api/v1/project", f"/api/v1/records/{rid}", "/api/v1/records",
                 "/api/v1/notebook", "/api/v1/context", "/api/v1/changes?wait=0"):  # fmt: skip
        reply = hub.request("GET", path)
        assert reply.status == 401, path
        assert SECRET_TITLE.encode() not in reply.body and rid.encode() not in reply.body
    # CP10.5: the session route also says whether the hub is in maintenance mode.
    assert hub.request("GET", "/api/v1/session").json() == {"paired": False, "mode": "normal"}
    forged = hub.request("GET", "/api/v1/project", cookie="not-a-session")
    assert forged.status == 401


def test_pairing_is_one_use_in_the_body_and_sets_a_strict_cookie(hub: Hub) -> None:
    wrong = hub.request("POST", "/api/v1/pair", {"token": "guess"}, cookie=None, csrf=None)
    assert wrong.status == 403 and "set-cookie" not in wrong.headers
    hub.pair()
    raw_cookie = hub.request("POST", "/api/v1/pair", {"token": hub.token}, cookie=None, csrf=None)
    assert raw_cookie.status == 403  # the token was consumed
    fresh = hub.gate.new_pairing_token()
    reply = hub.request("POST", "/api/v1/pair", {"token": fresh}, cookie=None, csrf=None)
    attributes = reply.headers["set-cookie"].lower()
    assert "httponly" in attributes and "samesite=strict" in attributes and "path=/" in attributes
    query = hub.request("GET", f"/api/v1/pair?token={hub.gate.new_pairing_token()}")
    assert query.status in (401, 404, 405)  # never accepted from a URL
    assert hub.request("GET", "/api/v1/project").status == 200


def test_pairing_expires_and_stops_after_repeated_guesses() -> None:
    now = [1000.0]
    gate = Gate(port=1, clock=lambda: now[0])
    token = gate.new_pairing_token()
    now[0] += PAIRING_TTL + 1
    assert gate.pair(token) is None
    token = gate.new_pairing_token()
    for _ in range(10):
        assert gate.pair("wrong") is None
    assert gate.pair(token) is None  # burned after too many guesses
    token = gate.new_pairing_token()
    paired = gate.pair(token)
    assert paired is not None
    session_id, _ = paired
    now[0] += 8 * 3600 + 1
    assert gate.session(session_id) is None  # sessions are short-lived


def test_csrf_and_origin_are_required_for_mutations(env: Env, hub: Hub) -> None:
    hub.pair()
    request = json.loads(request_bytes(env.project_id, [create_entry(title="Hub")], env.seq()))
    before = env.seq()
    assert hub.request("POST", "/api/v1/transactions", request, csrf=None).status == 403
    assert hub.request("POST", "/api/v1/transactions", request, csrf="wrong").status == 403
    evil = hub.request("POST", "/api/v1/transactions", request, origin="http://evil.example")
    assert evil.status == 403
    no_origin = hub.request("POST", "/api/v1/transactions", request, origin=None)
    assert no_origin.status == 403
    assert env.seq() == before  # nothing committed by any refused attempt
    ok = hub.request("POST", "/api/v1/transactions", request)
    assert ok.status == 200, ok.body
    assert env.seq() == before + 1


def test_hostile_host_and_cross_site_reads_are_refused(env: Env, hub: Hub) -> None:
    hub.pair()
    rid = _seed_secret(env)
    for host in (f"evil.example:{hub.port}", "127.0.0.1", f"127.0.0.1:{hub.port + 1}",
                 f"attacker.127.0.0.1.nip.io:{hub.port}"):  # fmt: skip
        reply = hub.request("GET", f"/api/v1/records/{rid}", host=host)
        assert reply.status == 421 and SECRET_TITLE.encode() not in reply.body, host
        assert hub.request("GET", "/", host=host).status == 421
    cross = hub.request("GET", f"/api/v1/records/{rid}", origin="http://evil.example")
    assert cross.status == 403 and SECRET_TITLE.encode() not in cross.body
    fetch_meta = hub.request(
        "GET", f"/api/v1/records/{rid}", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert fetch_meta.status == 403
    assert hub.request("GET", f"/api/v1/records/{rid}").status == 200  # same origin works


def test_security_headers_and_script_content_stays_inert(env: Env, hub: Hub) -> None:
    hub.pair()
    script = "<script>alert('x')</script><img src=x onerror=alert(1)>"
    rid = new_id("entry")
    env.apply([create_entry(rid, title=script)])
    reply = hub.request("GET", f"/api/v1/records/{rid}")
    assert reply.headers["content-type"].startswith("application/json")
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert reply.headers["cache-control"] == "no-store"
    csp = reply.headers["content-security-policy"]
    # The exact policy is pinned (integration review CP5 INT1-I2): any added or loosened
    # directive (e.g. media-src *) must change this test deliberately.
    assert csp == (
        "default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; media-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; manifest-src 'self'; form-action 'self'; "
        "object-src 'none'"
    )
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert reply.headers["x-frame-options"] == "DENY"
    assert reply.json()["body"]["title"] == script  # data, rendered as text by the UI
    page = hub.request("GET", "/%3Cscript%3Ealert(1)%3C/script%3E")
    assert b"<script>alert(1)" not in page.body  # the SPA fallback never reflects the path


def test_logout_ends_the_session(hub: Hub) -> None:
    hub.pair()
    assert hub.request("POST", "/api/v1/logout").status == 200
    assert hub.request("GET", "/api/v1/project").status == 401


def test_project_info_hides_local_paths(env: Env, hub: Hub) -> None:
    hub.pair()
    info = hub.request("GET", "/api/v1/project").json()
    assert "store" not in info and str(env.home).encode() not in json.dumps(info).encode()
    assert info["settings"]["allowed_roots"]  # aliases stay; their local paths do not
    assert all("path" not in root for root in info["settings"]["allowed_roots"])
    assert str(env.research) not in json.dumps(info)


def test_r1_no_local_paths_on_any_route(env: Env, hub: Hub, tmp_path: Path) -> None:
    from cp4_support import import_package, install, write_package

    import_package(env.svc, write_package(tmp_path / "pkg"))
    target = tmp_path / "proj"
    target.mkdir()
    install(env.svc, "synthetic-evidence", "claude-code", target, env.project_id)
    hub.pair()
    bodies = [
        hub.request("GET", "/api/v1/records?limit=200").body,
        hub.request("GET", f"/api/v1/records/{env.project_id}").body,
        hub.request("GET", f"/api/v1/records/{env.project_id}/history").body,
        hub.request("GET", "/api/v1/records?kind=package").body,
        hub.request("GET", "/api/v1/project").body,
        hub.request("GET", "/api/v1/skills").body,
    ]
    for body in bodies:
        text = body.decode("utf-8")
        for local in (str(env.home), str(env.research), str(target), str(tmp_path)):
            assert local not in text and local.replace("\\", "\\\\") not in text
    roots = hub.request("GET", f"/api/v1/records/{env.project_id}").json()["body"]
    assert [r["alias"] for r in roots["allowed_roots"]]  # aliases stay


def test_r1_errors_keep_the_envelope_and_headers(hub: Hub) -> None:
    hub.pair()
    for path in (
        "/api/v1/records?limit=0",
        f"/api/v1/changes?after_seq={2**63}&wait=0",
        f"/api/v1/records?at_seq={2**63}",
        "/api/v1/changes?after_seq=0&wait=21",
    ):
        reply = hub.request("GET", path)
        assert reply.status == 400, (path, reply.body)
        assert reply.json()["error"]["code"] == "validation_error"
        assert "content-security-policy" in reply.headers and "x-frame-options" in reply.headers


def test_r1_cursor_beyond_the_store_resets_immediately(env: Env, hub: Hub) -> None:
    import time

    hub.pair()
    started = time.monotonic()
    reply = hub.request("GET", f"/api/v1/changes?after_seq={env.seq() + 50}&wait=20").json()
    assert reply["reset"] is True and time.monotonic() - started < 2


def test_r1_real_hub_serve_command_pairs_end_to_end(env: Env, tmp_path: Path) -> None:
    import http.client
    import os
    import re
    import subprocess
    import sys
    import time

    log = tmp_path / "hub-serve.log"
    with log.open("w", encoding="utf-8") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "prometheos", "hub", "serve", "--project", env.project_id],
            env={**os.environ, "PROMETHEOS_HOME": str(env.home), "PYTHONIOENCODING": "utf-8"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sink,
        )
    try:
        deadline = time.monotonic() + 60
        text = ""
        while time.monotonic() < deadline and "Pairing code" not in text:
            time.sleep(0.2)
            text = log.read_text(encoding="utf-8")
        assert "Pairing code" in text, text
        port = int(re.search(r"http://127\.0\.0\.1:(\d+)/", text).group(1))  # type: ignore[union-attr]
        code = re.search(r"Pairing code[^:]*: (\S+)", text).group(1)  # type: ignore[union-attr]

        def call(method: str, path: str, body: bytes | None = None, cookie: str = "") -> Any:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if cookie:
                headers["Cookie"] = cookie
            try:
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                return response.status, response.getheader("set-cookie"), response.read()
            finally:
                conn.close()

        for _ in range(100):
            try:
                if call("GET", "/api/v1/session")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        status, cookie, _ = call("POST", "/api/v1/pair", json.dumps({"token": code}).encode())
        assert status == 200 and cookie
        session = cookie.split(";", 1)[0]
        assert call("GET", "/api/v1/project", cookie=session)[0] == 200
        again = call("POST", "/api/v1/pair", json.dumps({"token": code}).encode())
        assert again[0] == 403  # the printed code was one-use
        assert call("GET", "/")[0] == 200  # shipped assets are served
    finally:
        proc.terminate()
        proc.wait(timeout=30)
