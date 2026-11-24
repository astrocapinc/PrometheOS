"""CP21.3, the PWA (A07; decision 0090): part of C21-LAUNCH.

- the manifest and the service worker are served by the hub; the worker's cache is named by the
  shipped build's digest, keeps only the built page and its assets, never handles /api and
  fetches pages network-first;
- the pinned CSP adds ``worker-src 'self'`` and ``manifest-src 'self'``;
- installability is the browser's own report: the installed Edge, headless, with a fresh
  temporary profile inside the loopback-only boundary, on a hub started by ``prometheos start``
  and paired by its launch code. The report must be empty for the hub and must name errors for
  the same page without its manifest (the control). The cache then holds no record data.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cp1_support import Env
from fastapi.testclient import TestClient

from prometheos import launcher
from prometheos.hub.app import _static_dir, create_app, service_worker
from prometheos.hub.security import CSP, Gate

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / (
    "Microsoft/Edge/Application/msedge.exe"
)


def _client(env: Env, static: Path | None = None) -> TestClient:
    gate = Gate(port=8765)
    app = create_app(env.svc, env.project_id, gate, static_dir=static)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _shipped_digest() -> str:
    info = json.loads((_static_dir() / "build-info.json").read_text(encoding="utf-8"))
    return str(info["source_sha256"])[:16]


def test_the_csp_admits_the_worker_and_the_manifest_from_the_hub_only() -> None:
    assert "worker-src 'self';" in CSP and "manifest-src 'self';" in CSP
    assert "*" not in CSP


def test_the_service_worker_is_named_by_the_build_and_keeps_no_api(tmp_path: Path) -> None:
    env = Env(tmp_path)
    reply = _client(env).get("/sw.js")
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/javascript")
    assert reply.headers["cache-control"] == "no-cache"
    assert "worker-src 'self'" in reply.headers["content-security-policy"]
    script = reply.text
    assert f'const CACHE = "prometheos-hub-{_shipped_digest()}";' in script
    shell = json.loads(script.split("const SHELL = ", 1)[1].split(";\n", 1)[0])
    assert shell[0] == "/" and "/manifest.webmanifest" in shell
    assert not any(path.startswith("/api") for path in shell)
    # pages network-first, /api never handled, nothing stored at run time
    assert 'url.pathname.startsWith("/api/")) return;' in script
    assert "fetch(request).catch(" in script
    assert ".put(" not in script


def test_a_new_build_names_a_new_cache(tmp_path: Path) -> None:
    static = tmp_path / "static"
    shutil.copytree(_static_dir(), static)
    first = service_worker(static)
    info = json.loads((static / "build-info.json").read_text(encoding="utf-8"))
    info["source_sha256"] = "f" * 64
    (static / "build-info.json").write_text(json.dumps(info), encoding="utf-8")
    second = service_worker(static)
    assert first and second and first != second
    assert '"prometheos-hub-ffffffffffffffff"' in second


def test_no_shipped_build_means_no_service_worker(tmp_path: Path) -> None:
    env = Env(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _client(env, empty).get("/sw.js").status_code == 404


def test_the_manifest_is_served_as_a_manifest(tmp_path: Path) -> None:
    env = Env(tmp_path)
    reply = _client(env).get("/manifest.webmanifest")
    assert reply.status_code == 200
    assert reply.headers["content-type"] == "application/manifest+json"
    manifest = reply.json()
    assert manifest["start_url"] == "/" and manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in manifest["icons"]:
        assert _client(env).get(icon["src"]).headers["content-type"] == "image/png"


@pytest.mark.skipif(sys.platform != "win32", reason="the launcher is proved on Windows")
@pytest.mark.skipif(not EDGE.is_file(), reason="Microsoft Edge is not installed")
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_edge_reports_the_hub_installable_and_caches_no_record_data(tmp_path: Path) -> None:
    env = Env(tmp_path)
    variables = {**os.environ, "PROMETHEOS_HOME": str(env.home), "BROWSER": "none"}
    started = subprocess.run(
        [sys.executable, "-m", "prometheos", "start", "--no-open", "--project", env.project_id,
         "--json"],
        env=variables, capture_output=True, text=True, timeout=90, check=False,
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    url = json.loads(started.stdout)["url"]
    try:
        port = int(json.loads((env.home / "launcher" / "hub.json").read_text())["port"])
        code = launcher._owner_call(env.home, port, "code")["code"]
        host = url.split("//", 1)[1].split(":", 1)[0]
        done = subprocess.run(
            ["node", "tools/pwa-installability.mjs", url, host, code, str(EDGE)],
            cwd=ROOT / "hub", capture_output=True, text=True, timeout=180, check=False,
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        report = json.loads(done.stdout)
    finally:
        subprocess.run(
            [sys.executable, "-m", "prometheos", "stop", "--json"],
            env=variables, capture_output=True, timeout=60, check=False,
        )  # fmt: skip
    assert report["installability_errors"] == [], report
    assert report["control_errors"], "the control must name errors: the report is real"
    assert report["fragment_after_pairing"] == ""
    assert report["service_worker"] == "activated"
    assert list(report["caches"]) == [f"prometheos-hub-{_shipped_digest()}"]
    [cached] = report["caches"].values()
    assert cached and not any(path.startswith("/api") for path in cached)
