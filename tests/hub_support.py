"""Run the real hub (uvicorn on 127.0.0.1, OS-chosen port) in a thread for tests, and
talk to it over real HTTP with full control of Host/Origin/Cookie headers."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn

from prometheos.hub.app import create_app
from prometheos.hub.security import CSRF_HEADER, SESSION_COOKIE, Gate


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Hub:
    port: int
    gate: Gate
    token: str
    cookie: str | None = None
    csrf: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        host: str | None = None,
        origin: str | bool | None = True,
        cookie: str | bool | None = True,
        csrf: str | bool | None = True,
        headers: dict[str, str] | None = None,
        # 60 s, as the e2e suite and GIT_WAIT: on the owner's PC a local git command behind a
        # git-backed route intermittently takes ~30 s (INT4-CP10-L1; decision 0036 section 6).
        timeout: float = 60,
    ) -> Reply:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        sent: dict[str, str] = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin is True:
            if method not in ("GET", "HEAD"):
                sent["Origin"] = self.origin
        elif origin:
            sent["Origin"] = str(origin)
        chosen_cookie = self.cookie if cookie is True else cookie
        if chosen_cookie:
            sent["Cookie"] = f"{SESSION_COOKIE}={chosen_cookie}"
        chosen_csrf = self.csrf if csrf is True else csrf
        if chosen_csrf and method not in ("GET", "HEAD"):
            sent[CSRF_HEADER] = str(chosen_csrf)
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        try:
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            return Reply(
                response.status,
                {k.lower(): v for k, v in response.getheaders()},
                response.read(),
            )
        finally:
            # Closed on failure too: a timed-out socket left open fails the next test with an
            # unraisable ResourceWarning (int5b CP1).
            conn.close()

    def pair(self) -> None:
        reply = self.request("POST", "/api/v1/pair", {"token": self.token}, cookie=None, csrf=None)
        assert reply.status == 200, reply.body
        cookie = reply.headers["set-cookie"]
        self.cookie = cookie.split(";", 1)[0].split("=", 1)[1]
        self.csrf = reply.json()["csrf_token"]


@contextmanager
def running_hub(service: Any, project_id: str) -> Iterator[Hub]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    gate = Gate(port=port)
    token = gate.new_pairing_token()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(service, project_id, gate),
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("hub did not start")
        time.sleep(0.02)
    try:
        yield Hub(port=port, gate=gate, token=token)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
