"""Serve the real hub on an existing walk store (C10-WALK; decision 0022 item 5).

    uv run python tools/walk_hub.py --home HOME --project PROJECT_ID --info INFO.json

Starts the built hub (the assets shipped in the package) on 127.0.0.1 over a store the walk
harness seeded, with a queue of one-use pairing tokens (one per browser context) and an agent
bearer token, and writes ``{url, pairing_tokens, agent_token, project_id}`` to ``--info``.
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
    # Synthetic fixture hold: the initial run stays queued until the fresh agent resumes it.
    original_dispatch = service._dispatch
    held_runs: set[str] = set()

    def dispatch(env, run_id, mode, wait_seconds):
        from prometheos.app.reads import Caller

        if (
            service.run_get(args.project, Caller(actor="walk", scope="owner"), run_id)["operation"]
            == "source_import_plan"
            and run_id not in held_runs
        ):
            held_runs.add(run_id)
            return
        original_dispatch(env, run_id, mode, wait_seconds)

    service._dispatch = dispatch
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    gate = MultiTokenGate(port=port)
    info = {
        "url": f"http://127.0.0.1:{port}/",
        "port": port,
        "project_id": args.project,
        "pairing_tokens": [gate.add_token() for _ in range(args.tokens)],
        "agent_token": gate.new_agent_token(),
    }
    args.info.write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps({"url": info["url"]}), flush=True)
    app = create_app(service, args.project, gate)
    uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")).run(
        sockets=[sock]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
