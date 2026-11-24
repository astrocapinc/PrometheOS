"""CP21.2 (A07; decision 0089): the home's host name and remembered devices.

The real hub (uvicorn on 127.0.0.1) answers at the home's own host name; requests carry exact
Host, Origin and Cookie headers. A "restart" is a second hub process for the same home: a new
Gate and app, the same devices store.
- pairing at the home's host name leaves a host-only, per-home remembered device cookie;
  pairing at 127.0.0.1 leaves none;
- after a restart the cookie yields a new session without pairing, and rotates; the previous
  value works for 60 seconds (two tabs), not after;
- 14 days unused or 90 days after creation, revocation (hub or CLI), sign-out, another home,
  another Host, port or Origin, and an agent token all end in pairing.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from cp1_support import Env

from prometheos import devices, launcher
from prometheos.hub.app import create_app
from prometheos.hub.security import CSRF_HEADER, SESSION_COOKIE, Gate


@dataclass
class Live:
    port: int
    gate: Gate
    host: str  # the home's host name with the port
    cookie_name: str

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        host: str | None = None,
        origin: str | None = None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, list[str]], Any]:
        sent = {"Host": host or self.host}
        if method != "GET":
            sent["Origin"] = origin or f"http://{host or self.host}"
        if cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=data, headers=sent)
            reply = conn.getresponse()
            set_cookies: dict[str, list[str]] = {}
            for name, value in reply.getheaders():
                if name.lower() == "set-cookie":
                    key = value.split("=", 1)[0]
                    set_cookies.setdefault(key, []).append(value)
            raw = reply.read()
            return reply.status, set_cookies, json.loads(raw) if raw else None
        finally:
            conn.close()


def cookie_value(set_cookies: dict[str, list[str]], name: str) -> str:
    return set_cookies[name][-1].split(";", 1)[0].split("=", 1)[1]


@contextmanager
def hub(env: Env) -> Iterator[Live]:
    label = devices.host_label(launcher.home_identity(env.home)["home_id"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    gate = Gate(port=port, home_host=f"{label}.localhost")
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(env.svc, env.project_id, gate),
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        yield Live(port, gate, f"{label}.localhost:{port}", devices.cookie_name(label))
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()


def paired(live: Live) -> tuple[str, str, str]:
    """Pair at the home's host name: (session, csrf, remembered device value)."""
    status, cookies, body = live.call(
        "POST", "/api/v1/pair", {"token": live.gate.new_pairing_token(ttl=60)}
    )
    assert status == 200, body
    return (
        cookie_value(cookies, SESSION_COOKIE),
        body["csrf_token"],
        cookie_value(cookies, live.cookie_name),
    )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_the_host_name_is_a_stable_dns_label(env: Env) -> None:
    home_id = launcher.home_identity(env.home)["home_id"]
    label = devices.host_label(home_id)
    assert label == devices.host_label(launcher.home_identity(env.home)["home_id"])
    assert len(label) == 17 and label[0] == "h" and int(label[1:], 16) >= 0
    assert label[1:] == home_id.replace("-", "")[:16]


def test_pairing_at_the_home_host_remembers_the_browser_host_only(env: Env) -> None:
    with hub(env) as live:
        status, cookies, _ = live.call(
            "POST", "/api/v1/pair", {"token": live.gate.new_pairing_token(ttl=60)}
        )
        assert status == 200
        header = cookies[live.cookie_name][-1].lower()
        assert "httponly" in header and "samesite=strict" in header and "path=/" in header
        assert "domain=" not in header  # host-only: no other host name receives it
        plain = f"127.0.0.1:{live.port}"
        status, cookies, _ = live.call(
            "POST", "/api/v1/pair", {"token": live.gate.new_pairing_token(ttl=60)}, host=plain
        )
        assert status == 200 and live.cookie_name not in cookies
    stored = (env.home / "launcher" / "devices.sqlite").read_bytes()
    assert cookie_value({"x": [header]}, "x").split(".", 1)[1].encode() not in stored


def test_a_restart_resumes_without_pairing_and_rotates(env: Env) -> None:
    with hub(env) as first:
        _, _, remembered = paired(first)
    with hub(env) as second:  # a new hub process for the same home
        status, cookies, body = second.call(
            "POST", "/api/v1/device/resume", {}, cookies={second.cookie_name: remembered}
        )
        assert status == 200 and body["paired"] is True
        session = cookie_value(cookies, SESSION_COOKIE)
        rotated = cookie_value(cookies, second.cookie_name)
        assert rotated != remembered
        status, _, info = second.call("GET", "/api/v1/session", cookies={SESSION_COOKIE: session})
        assert info["paired"] is True
        # a second tab still holding the previous value, inside the grace: a session, no rotation
        status, cookies, _ = second.call(
            "POST", "/api/v1/device/resume", {}, cookies={second.cookie_name: remembered}
        )
        assert status == 200 and second.cookie_name not in cookies


def test_the_previous_value_lapses_after_sixty_seconds(env: Env) -> None:
    value = devices.remember(env.home, "Edge on Windows", now=1000.0)
    _, rotated = devices.resume(env.home, value, now=1000.0) or ("", None)
    assert rotated is not None
    assert devices.resume(env.home, value, now=1059.0) is not None  # inside the grace
    assert devices.resume(env.home, value, now=1061.0) is None
    assert devices.resume(env.home, rotated, now=1061.0) is not None


def test_idle_and_absolute_expiry(env: Env) -> None:
    day = 86400.0
    value = devices.remember(env.home, "Chrome", now=0.0)
    assert devices.resume(env.home, value, now=15 * day) is None  # 14 days unused
    value = devices.remember(env.home, "Chrome", now=0.0)
    for used in range(10, 90, 10):  # used every ten days, never idle for 14
        _, fresh = devices.resume(env.home, value, now=used * day) or ("", None)
        assert fresh is not None, used
        value = fresh
    assert devices.resume(env.home, value, now=91 * day) is None  # 90 days at most


def test_revocation_ends_the_current_and_the_grace_value(env: Env) -> None:
    value = devices.remember(env.home, "Edge", now=0.0)
    device_id = value.split(".", 1)[0]
    _, rotated = devices.resume(env.home, value, now=1.0) or ("", None)
    devices.revoke(env.home, device_id, now=2.0)
    assert devices.resume(env.home, value, now=3.0) is None  # inside the grace, revoked
    assert devices.resume(env.home, rotated, now=3.0) is None
    [view] = devices.list_devices(env.home, now=3.0)
    assert view["state"] == "revoked" and set(view) == {
        "device_id", "label", "state", "created_at", "last_used_at", "expires_at", "revoked_at"
    }  # fmt: skip


def test_revoking_in_the_hub_ends_the_device_sessions_at_once(env: Env) -> None:
    with hub(env) as first:
        _, _, remembered = paired(first)
    with hub(env) as live:
        _, cookies, _ = live.call(
            "POST", "/api/v1/device/resume", {}, cookies={live.cookie_name: remembered}
        )
        device_session = cookie_value(cookies, SESSION_COOKIE)
        owner, csrf, _ = paired(live)
        status, _, listed = live.call("GET", "/api/v1/devices", cookies={SESSION_COOKIE: owner})
        assert status == 200 and len(listed["devices"]) == 2
        device_id = remembered.split(".", 1)[0]
        status, _, _ = live.call(
            "POST", "/api/v1/devices/revoke", {"device_id": device_id},
            cookies={SESSION_COOKIE: owner}, headers={CSRF_HEADER: csrf},
        )  # fmt: skip
        assert status == 200
        status, _, _ = live.call("GET", "/api/v1/project", cookies={SESSION_COOKIE: device_session})
        assert status == 401


def test_revoking_from_the_cli_signs_the_browser_out_within_seconds(env: Env) -> None:
    with hub(env) as live:
        _, _, remembered = paired(live)
        _, cookies, _ = live.call(
            "POST", "/api/v1/device/resume", {}, cookies={live.cookie_name: remembered}
        )
        resumed = cookie_value(cookies, SESSION_COOKIE)
        assert live.call("GET", "/api/v1/project", cookies={SESSION_COOKIE: resumed})[0] == 200
        env.svc.device_revoke(remembered.split(".", 1)[0])  # the CLI's own act, another process
        time.sleep(2.2)  # the hub rereads a device's state at most every 2 seconds
        assert live.call("GET", "/api/v1/project", cookies={SESSION_COOKIE: resumed})[0] == 401


def test_signing_out_forgets_the_browser(env: Env) -> None:
    with hub(env) as live:
        session, csrf, remembered = paired(live)
        status, cookies, _ = live.call(
            "POST", "/api/v1/logout", {},
            cookies={SESSION_COOKIE: session, live.cookie_name: remembered},
            headers={CSRF_HEADER: csrf},
        )  # fmt: skip
        assert status == 200
        assert any("max-age=0" in c.lower() for c in cookies[live.cookie_name])
    with hub(env) as again:
        status, _, _ = again.call(
            "POST", "/api/v1/device/resume", {}, cookies={again.cookie_name: remembered}
        )
        assert status == 401


def test_the_credential_works_only_on_its_own_host_port_and_origin(env: Env) -> None:
    with hub(env) as live:
        _, _, remembered = paired(live)
        jar = {live.cookie_name: remembered}
        plain = f"127.0.0.1:{live.port}"
        assert live.call("POST", "/api/v1/device/resume", {}, host=plain, cookies=jar)[0] == 403
        other_origin = f"http://{live.host.split(':')[0]}:{live.port + 1}"
        assert (
            live.call("POST", "/api/v1/device/resume", {}, origin=other_origin, cookies=jar)[0]
            == 403
        )
        stranger = f"hdeadbeefdeadbeef.localhost:{live.port}"
        assert live.call("POST", "/api/v1/device/resume", {}, host=stranger, cookies=jar)[0] == 421
        agent = {"Authorization": f"Bearer {live.gate.new_agent_token()}"}
        assert live.call("POST", "/api/v1/device/resume", {}, cookies=jar, headers=agent)[0] == 403
        # still valid for its own host name after all the refusals
        assert live.call("POST", "/api/v1/device/resume", {}, cookies=jar)[0] == 200


def test_another_homes_credential_is_not_recognised(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    one, two = Env(tmp_path / "one"), Env(tmp_path / "two")
    with hub(one) as first:
        _, _, remembered = paired(first)
    with hub(two) as second:
        assert second.cookie_name != first.cookie_name
        jar = {first.cookie_name: remembered, second.cookie_name: remembered}
        assert second.call("POST", "/api/v1/device/resume", {}, cookies=jar)[0] == 401


def test_an_agent_token_cannot_list_or_revoke_devices(env: Env) -> None:
    with hub(env) as live:
        agent = {"Authorization": f"Bearer {live.gate.new_agent_token()}"}
        assert live.call("GET", "/api/v1/devices", headers=agent)[0] == 403
        status, _, _ = live.call(
            "POST", "/api/v1/devices/revoke", {"device_id": "dev_0123456789abcdef"}, headers=agent
        )
        assert status == 403


# ----- review cp21-a (decision 0091 records the repairs) ----------------------------------------


def test_the_credential_needs_the_homes_own_origin_not_a_loopback_one(env: Env) -> None:
    """F2: Host, port and Origin must all be the home's own (spec/08)."""
    with hub(env) as live:
        _, _, device = paired(live)
        for origin in (f"http://127.0.0.1:{live.port}", f"http://localhost:{live.port}"):
            status, cookies, _ = live.call(
                "POST", "/api/v1/device/resume", {}, origin=origin,
                cookies={live.cookie_name: device},
            )  # fmt: skip
            assert status == 403 and SESSION_COOKIE not in cookies, origin
            status, cookies, _ = live.call(
                "POST", "/api/v1/pair", {"token": live.gate.new_pairing_token(ttl=60)},
                origin=origin,
            )  # fmt: skip
            assert status == 200 and live.cookie_name not in cookies, origin
        # the refused attempts rotated nothing: the credential still resumes once, properly
        status, _, _ = live.call("POST", "/api/v1/device/resume", {},
                                 cookies={live.cookie_name: device})  # fmt: skip
        assert status == 200


def test_revoking_a_freshly_paired_browser_ends_its_pairing_session(env: Env) -> None:
    """F3: the session a pairing created follows the device it remembered."""
    with hub(env) as live:
        session, csrf, device = paired(live)
        device_id = device.split(".", 1)[0]
        assert live.call("GET", "/api/v1/project", cookies={SESSION_COOKIE: session})[0] == 200
        devices.revoke(env.home, device_id)  # the CLI's act, outside the hub
        time.sleep(2.3)
        status, _, _ = live.call("GET", "/api/v1/project", cookies={SESSION_COOKIE: session})
        assert status == 401
        # In the hub (Recovery page): the pairing session ends at once.
        session, csrf, device = paired(live)
        status, _, _ = live.call(
            "POST", "/api/v1/devices/revoke", {"device_id": device.split(".", 1)[0]},
            cookies={SESSION_COOKIE: session}, headers={CSRF_HEADER: csrf},
        )  # fmt: skip
        assert status == 200
        status, _, _ = live.call("GET", "/api/v1/project", cookies={SESSION_COOKIE: session})
        assert status == 401


def test_a_non_ascii_credential_is_unknown_not_a_server_error(env: Env) -> None:
    """A6: never a 500."""
    assert devices.resume(env.home, "dev_0123456789abcdef.sécret") is None
    with hub(env) as live:
        conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=30)
        try:
            conn.putrequest("POST", "/api/v1/device/resume", skip_host=True)
            conn.putheader("Host", live.host)
            conn.putheader("Origin", f"http://{live.host}")
            conn.putheader("Cookie", f"{live.cookie_name}=dev_0123456789abcdef.s\xe9cret")
            conn.putheader("Content-Length", "0")
            conn.endheaders()
            assert conn.getresponse().status == 401
        finally:
            conn.close()
