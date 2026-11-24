"""Synthetic walk fixture: run the real MCP CLI with dispatch held on the first chat.

The first process persists the ordinary source_import_plan run, but its dispatcher does
not start it. The next fresh process uses unchanged dispatch and resumes the durable run.
This deterministic fixture belongs only to C10-WALK; production scheduling is unchanged.
"""

from __future__ import annotations

import json
import os
import runpy
from pathlib import Path

from prometheos.app.service import Service

Path(os.environ["WALK_PROCESS_INFO"]).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
if os.environ.get("WALK_HOLD_DISPATCH") == "1":
    original = Service._dispatch

    def held(self, env, run_id, mode, wait_seconds):
        if (
            self.run_get(
                env.project_id,
                __import__("prometheos.app.reads", fromlist=["Caller"]).Caller(
                    actor="walk", scope="owner"
                ),
                run_id,
            )["operation"]
            == "source_import_plan"
        ):
            return
        original(self, env, run_id, mode, wait_seconds)

    Service._dispatch = held

runpy.run_module("prometheos", run_name="__main__")
