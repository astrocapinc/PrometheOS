"""Out-of-process actor for crash, lock and concurrency probes (tests only).

Usage: worker.py COMMAND ARGS...   (PROMETHEOS_HOME selects the disposable home)
  apply PROJECT REQUEST_FILE
  import-commit PROJECT PLAN_ID
  backup PROJECT OUT
  hold-lock STORE_DIR SECONDS READY_FILE
  race PROJECT START_FILE COUNT     -- append COUNT entries, retrying on conflict
  record PROJECT TITLE              -- scripted endless capture (recording kill probe)
  transcribe PROJECT RUN_ID         -- execute a transcription run (kill probe)
Prints one JSON line: {"ok": ..., "result"|"error": ...}.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from prometheos.app.authority import OwnerAct
from prometheos.app.reads import Caller
from prometheos.app.service import Service
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.store.lock import maintenance_lock


def main() -> int:
    command, *args = sys.argv[1:]
    svc = Service(actor=f"worker:{command}")
    try:
        if command == "apply":
            result = svc.apply(args[0], Path(args[1]).read_bytes())
        elif command == "import-commit":
            result = svc.import_commit(args[0], args[1])
        elif command == "backup":
            result = svc.backup(args[0], Path(args[1]), Caller("worker", "owner", full_data=True))
        elif command == "hold-lock":
            with maintenance_lock(Path(args[0])):
                Path(args[2]).write_text("ready", encoding="utf-8")
                time.sleep(float(args[1]))
            result = {"held": True}
        elif command == "jobs-tick":
            # jobs-tick PROJECT NOW_ISO [external|fail-external]: one pass at an injected time.
            # "external" swaps the digest handler for a synthetic one that marks an external
            # request before "sending" (tests only; no v1 handler transmits anything).
            from datetime import datetime

            from prometheos.app import jobs

            if len(args) > 2 and args[2] == "external":

                def synthetic_external(ctx: jobs.JobContext) -> jobs.Outcome:
                    ctx.before_external("synthetic provider request (test)")
                    return jobs.Outcome({"usage": {"provider_calls": 1}}, "synthetic")

                jobs.HANDLERS["digest"] = synthetic_external
            elif len(args) > 2 and args[2] == "fail-external":
                # Marks an external request, then fails: the slot becomes unknown.

                def failing_external(ctx: jobs.JobContext) -> jobs.Outcome:
                    ctx.before_external("synthetic provider request (test)")
                    raise RuntimeError("synthetic failure after the request began")

                jobs.HANDLERS["digest"] = failing_external
            result = svc.jobs_tick(args[0], now=datetime.fromisoformat(args[1]))
        elif command == "race":
            project, start, count = args[0], Path(args[1]), int(args[2])
            while not start.exists():
                time.sleep(0.005)
            conflicts = 0
            done = 0
            while done < count:
                seq = svc.project_info(project, Caller("worker", "owner"))["project_seq"]
                body = {
                    "title": f"race {new_id('entry')[-6:]}",
                    "text": "x",
                    "category": "note",
                    "evidence": [],
                    "claim_refs": [],
                    "author_ref": None,
                    "origin": "agent",
                    "quantities": [],
                    "review": "unreviewed",
                }
                raw = json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": project,
                        "request_id": new_request_id(),
                        "expected_seq": seq,
                        "operations": [
                            {
                                "op": "create",
                                "kind": "entry",
                                "id": new_id("entry"),
                                "base_rev": 0,
                                "sensitivity": "normal",
                                "occurred_at": None,
                                "body": body,
                            }
                        ],
                    }
                ).encode()
                try:
                    svc.apply(project, raw)
                    done += 1
                except PrometheosError as exc:
                    if exc.code not in ("revision_conflict", "busy"):
                        raise
                    conflicts += 1
            result = {"done": done, "conflicts": conflicts}
        elif command == "record":
            # record PROJECT TITLE: capture an endless scripted tone with a 5 s segment size
            # (tests only; the fault point recording_after_segment kills it mid-session).
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from audio_support import MIC, ScriptedBackend, ScriptedSource

            from prometheos.audio import recorder

            backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 10**9)})
            owner = Caller("worker", "owner")
            session = svc.recording_start(
                args[0],
                owner,
                recorder.SessionRequest(
                    args[1], ("mic-1",), "normal", segment_seconds=5, consent_acknowledged=True
                ),
                backend,
                authority=OwnerAct(channel="cli-interactive", actor="worker-owner"),
            )
            result = svc.recording_run(args[0], session["session_id"], backend, stall_seconds=30)
        elif command == "transcribe":
            # transcribe PROJECT RUN_ID: execute a run (needs the synthetic engine switch)
            result = svc.transcribe_run(args[0], Caller("worker", "owner"), args[1])
        else:
            raise SystemExit(f"unknown command {command}")
    except PrometheosError as exc:
        print(json.dumps({"ok": False, "error": exc.to_response().model_dump(mode="json")}))
        return exc.exit_code
    print(json.dumps({"ok": True, "result": result}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
