"""CP19.2: the hub as the desktop app's sidecar (C19-SHELL; decision 0054).

``prometheos hub serve --sidecar`` speaks to the process that started it, and only to it:
- stdout carries one JSON line per event: a pairing line with the URL and a one-use code, a new
  pairing line when the parent writes ``pair``, and ``stopped`` at the end;
- the code never goes to stderr or anywhere else;
- the window pairs through the hub's own pairing route, so no new authority path exists;
- end of input is a graceful stop: the hub finishes, exits 0 and frees its port.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cp1_support import Env


def pair(url: str, code: str) -> int:
    """POST the code to the hub's own pairing route, as the window does (loopback only)."""
    port = int(url.rsplit(":", 1)[1].strip("/"))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            "POST", "/api/v1/pair", body=json.dumps({"token": code}),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
        )  # fmt: skip
        return connection.getresponse().status
    finally:
        connection.close()


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def sidecar(tmp_path: Path) -> Any:
    env = Env(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "prometheos", "hub", "serve", "--sidecar", "--project",
         env.project_id],
        env={**os.environ, "PROMETHEOS_HOME": str(env.home), "PYTHONIOENCODING": "utf-8"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    yield env, process
    if process.poll() is None:
        process.kill()
        process.wait(10)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()  # an unclosed pipe surfaces later as an unraisable ResourceWarning


def test_the_sidecar_hands_its_pairing_code_to_its_parent_only(sidecar: Any) -> None:
    env, process = sidecar
    first = json.loads(process.stdout.readline())
    assert first["event"] == "pairing" and first["project_id"] == env.project_id
    assert first["url"].startswith("http://127.0.0.1:") and first["pairing_code"]
    assert pair(first["url"], first["pairing_code"]) == 200
    assert pair(first["url"], first["pairing_code"]) != 200  # one use
    process.stdin.write("pair\n")
    process.stdin.flush()
    again = json.loads(process.stdout.readline())
    assert again["event"] == "pairing" and again["pairing_code"] != first["pairing_code"]
    assert pair(again["url"], again["pairing_code"]) == 200
    process.stdin.close()
    assert process.wait(30) == 0
    rest = process.stdout.read()
    assert [json.loads(line)["event"] for line in rest.splitlines()] == ["stopped"]
    stderr = process.stderr.read()
    assert first["pairing_code"] not in stderr and again["pairing_code"] not in stderr


def test_end_of_input_is_a_graceful_stop_that_frees_the_port(sidecar: Any) -> None:
    _env, process = sidecar
    first = json.loads(process.stdout.readline())
    port = int(first["url"].rsplit(":", 1)[1].strip("/"))
    assert port_open(port)
    process.stdin.close()
    assert process.wait(30) == 0  # no terminate needed
    assert not port_open(port)
