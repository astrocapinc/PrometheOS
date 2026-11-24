"""Built hub assets are served with their media type from a fixed table, not from the host.

Regression (session 7, owner's PC): the registry mapped ``.mjs`` to ``text/plain``; Python's
``mimetypes`` passed that on, the hub served pdf.js's worker module as text and Chromium
refused it, so no PDF page rendered after a successful compile."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import pytest
from cp1_support import Env
from hub_support import running_hub

from prometheos.hub import app as hub_app

EXPECTED = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".png": "image/png",  # the PWA's icons (A07, CP21.3)
    ".webmanifest": "application/manifest+json",
}


def test_every_built_asset_has_its_type_even_when_the_host_maps_it_to_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = Path(str(hub_app._static_dir()))
    built = sorted(p for p in static.rglob("*") if p.is_file())
    assert {p.suffix for p in built} >= {".mjs", ".js", ".css", ".html"}, "hub assets not built"
    # A host whose registry maps every extension to text/plain.
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: ("text/plain", None))
    env = Env(tmp_path)
    with running_hub(env.svc, env.project_id) as hub:
        for path in built:
            rel = path.relative_to(static).as_posix()
            reply = hub.request("GET", f"/{rel}", cookie=None)
            assert reply.status == 200, rel
            served = reply.headers["content-type"].split(";")[0].strip()
            assert served == EXPECTED[path.suffix], (rel, served)


def test_unknown_extensions_are_served_as_octet_stream() -> None:
    assert hub_app.asset_media_type("x.unknown") == "application/octet-stream"
    assert hub_app.asset_media_type("WORKER.MJS").startswith("text/javascript")
