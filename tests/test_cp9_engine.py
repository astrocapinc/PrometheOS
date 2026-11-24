"""CP9.1 engine install, verification and the bounded runner, without the real engine.

The install path is exercised against a loopback HTTP server serving a synthetic release
zip (no third-party request); the runner against small Python stand-ins for an engine.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import sys
import threading
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cp1_support import OWNER, Env
from cp9_support import LatexEnv

from prometheos.app import texengine
from prometheos.domain.models import PrometheosError

FAKE_EXE = b"MZ synthetic stand-in, not an engine\n" * 100


def fake_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class Server:
    def __init__(self, payload: bytes) -> None:
        body = payload

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/release.zip"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(texengine.ENGINE_DIR_ENV, raising=False)
    return tmp_path / "home"


def release_for(payload: bytes, url: str) -> texengine.Release:
    return replace(
        texengine.TECTONIC,
        url=url,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        asset="release.zip",
    )


def test_plan_discloses_size_licence_destination_and_network(home: Path) -> None:
    plan = texengine.install_plan(home)
    assert plan["download_bytes"] == 21_060_223
    assert plan["sha256"] == "f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f"
    assert plan["license"] == "MIT" and plan["version"] == "0.17.0"
    assert plan["destination"] == str(home / "tools" / "tectonic-0.17.0")
    assert plan["changes_path_or_system"] is False
    assert "never the manuscript text" in plan["network"]
    assert texengine.status(home)["state"] == "absent"


def test_install_downloads_verifies_and_records_digests(home: Path) -> None:
    payload = fake_zip({"tectonic.exe" if sys.platform == "win32" else "tectonic": FAKE_EXE})
    server = Server(payload)
    try:
        release = release_for(payload, server.url)
        plan = texengine.install_plan(home, release)
        with pytest.raises(PrometheosError) as stale:
            texengine.install(home, "0" * 64, release=release)
        assert stale.value.code == "revision_conflict"
        status = texengine.install(home, plan["plan_digest"], release=release)
    finally:
        server.close()
    assert status["state"] == "installed"
    manifest = json.loads((home / "tools" / "tectonic-0.17.0" / "install.json").read_text())
    assert manifest["zip_sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["exe_sha256"] == hashlib.sha256(FAKE_EXE).hexdigest()
    # Tampering with the executable afterwards is detected before any use.
    exe = home / "tools" / "tectonic-0.17.0" / texengine.EXE_NAME
    exe.write_bytes(FAKE_EXE + b"tampered")
    assert texengine.status(home, release)["state"] == "invalid"


def test_install_refuses_a_mismatching_archive_and_installs_nothing(home: Path) -> None:
    payload = fake_zip({texengine.EXE_NAME: FAKE_EXE})
    server = Server(payload)
    try:
        wrong = replace(release_for(payload, server.url), sha256="1" * 64)
        plan = texengine.install_plan(home, wrong)
        with pytest.raises(PrometheosError) as refused:
            texengine.install(home, plan["plan_digest"], release=wrong)
        assert refused.value.code == "validation_error"
        bigger = replace(release_for(payload, server.url), size=len(payload) - 10)
        plan = texengine.install_plan(home, bigger)
        with pytest.raises(PrometheosError):
            texengine.install(home, plan["plan_digest"], release=bigger)
    finally:
        server.close()
    assert not (home / "tools" / "tectonic-0.17.0").exists()
    assert not list((home / "tools").glob(".install-*"))


def test_install_from_a_local_archive_and_archive_shape_checks(home: Path, tmp_path: Path) -> None:
    good = fake_zip({f"tectonic-0.17.0/{texengine.EXE_NAME}": FAKE_EXE})
    archive = tmp_path / "release.zip"
    archive.write_bytes(good)
    release = release_for(good, "https://unused.invalid/release.zip")
    plan = texengine.install_plan(home, release)
    status = texengine.install(home, plan["plan_digest"], archive=archive, release=release)
    assert status["state"] == "installed"
    assert status["installed"]["source_url"] == "local archive release.zip"
    empty = fake_zip({"README.md": b"no engine here"})
    archive.write_bytes(empty)
    other_home = tmp_path / "home2"
    release2 = release_for(empty, "https://unused.invalid/release.zip")
    plan2 = texengine.install_plan(other_home, release2)
    with pytest.raises(PrometheosError):
        texengine.install(other_home, plan2["plan_digest"], archive=archive, release=release2)


def test_compile_refuses_without_a_verified_engine(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(texengine.ENGINE_DIR_ENV, str(tmp_path / "no-engine"))
    ws = LatexEnv(Env(tmp_path))
    with pytest.raises(PrometheosError) as refused:
        ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert refused.value.code == "unsupported"
    assert "tex engine install" in refused.value.message


# ----- runner limits with stand-in engines ------------------------------------------------------

BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)


def run_script(tmp_path: Path, code: str, **limits: Any) -> texengine.RunResult:
    script = tmp_path / "standin.py"
    script.write_text(code, "utf-8")
    temp = tmp_path / "tmp"
    temp.mkdir(exist_ok=True)
    env = texengine.clean_environment(tmp_path / "cache", temp, network=False)
    return texengine.run(
        [BASE_PYTHON, "-I", str(script)],
        cwd=tmp_path,
        env=env,
        limits=texengine.RunLimits(**limits),
        watch=tmp_path / "out",
    )


def test_runner_kills_at_the_time_limit(tmp_path: Path) -> None:
    result = run_script(
        tmp_path, "import time\nwhile True:\n    time.sleep(0.1)\n", timeout_seconds=2
    )
    assert result.timed_out and result.seconds < 15


def test_runner_stops_runaway_output(tmp_path: Path) -> None:
    code = "import sys\nwhile True:\n    sys.stderr.write('x' * 65536)\n"
    result = run_script(tmp_path, code, timeout_seconds=30, max_captured_bytes=1024 * 1024)
    assert result.output_exceeded and not result.timed_out
    assert len(result.stderr) <= 1024 * 1024


def test_runner_stops_runaway_files(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    code = (
        "import os, time\nf = open(os.path.join('out', 'big.pdf'), 'wb')\n"
        "while True:\n    f.write(b'0' * 1048576)\n    f.flush()\n    time.sleep(0.01)\n"
    )
    result = run_script(tmp_path, code, timeout_seconds=30, max_output_dir_bytes=8 * 1024 * 1024)
    assert result.output_exceeded and not result.timed_out


def test_runner_environment_is_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVERLEAF_TOKEN", "should-not-leak")
    monkeypatch.setenv("TEXINPUTS", "C:/somewhere")
    code = "import os, json\nprint(json.dumps(dict(os.environ)))\n"
    result = run_script(tmp_path, code, timeout_seconds=30)
    seen = json.loads(result.stdout.decode())
    assert "OVERLEAF_TOKEN" not in seen and "TEXINPUTS" not in seen
    assert seen["TECTONIC_UNTRUSTED_MODE"] == "1"
    assert seen["HTTPS_PROXY"] == texengine.DEAD_PROXY


@pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-only")
def test_engine_cannot_start_child_processes(tmp_path: Path) -> None:
    marker = tmp_path / "child-ran.txt"
    child = f"open({marker.as_posix()!r}, 'w').write('x')"
    code = (
        "import subprocess, sys\n"
        "try:\n"
        f"    subprocess.run([sys.executable, '-c', {child!r}])\n"
        "    print('spawned')\n"
        "except OSError as exc:\n"
        "    print('refused', exc)\n"
    )
    result = run_script(tmp_path, code, timeout_seconds=30)
    assert b"refused" in result.stdout
    assert not marker.exists()
