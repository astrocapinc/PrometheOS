"""Serve the real hub on an existing C18-WALK store (CP18.4; tools/walk_cp18.py).

    uv run python tools/walk_hub18.py --home HOME --project PROJECT_ID --info INFO.json

Starts the built hub (the assets shipped in the package) on 127.0.0.1 over a store the walk
harness made, as ``prometheos hub serve`` does: run recovery, then the one detector pass a
starting server runs (decision 0035 item 6). It adds a queue of one-use pairing tokens (one per
browser context) and writes ``{url, port, pairing_tokens, project_id}`` to ``--info``. Unlike
the C10 walk's launcher it holds nothing back: every run is dispatched as in production.
Test tool only: it serves whatever store it is pointed at, so only ever point it at a
disposable synthetic project.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hub_fixture import MultiTokenGate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--info", type=Path, required=True)
    parser.add_argument("--tokens", type=int, default=6)
    args = parser.parse_args()

    from prometheos.app.service import Service
    from prometheos.hub.app import create_app

    service = Service(home=args.home, actor="hub")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    gate = MultiTokenGate(port=port)
    app = create_app(service, args.project, gate)
    # What `prometheos hub serve` does before it announces itself (cli.py, decision 0035).
    service.run_recover(args.project)
    start_pass = service.events_detect_at_start(args.project)
    info = {
        "url": f"http://127.0.0.1:{port}/",
        "port": port,
        "project_id": args.project,
        "pairing_tokens": [gate.add_token() for _ in range(args.tokens)],
        "start_pass": start_pass,
    }
    args.info.write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps({"url": info["url"]}), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")).run(
        sockets=[sock]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
