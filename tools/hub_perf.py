"""Measure spec/02's responsiveness targets on this machine (synthetic data only).

    uv run python tools/hub_perf.py [--entries 1000]

Builds a temporary project with N notebook entries, then measures:

* context assembly (agent `context show`, 12 000-character budget): p95 of 20 runs,
  target < 1 s;
* ordinary writes (one entry per request through the shared commit engine): p95 of 50,
  target < 500 ms;
* hub change delivery: a paired long-poll on the real hub over HTTP returns after a
  commit; p95 latency of 20 commits, target < 2 s.

Prints hardware/dataset details and the measurements as JSON and exits non-zero if a
target is missed. Targets are acceptance thresholds to measure, not claims.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, round(0.95 * len(ordered)) - 1)]


def entry_op(index: int) -> dict[str, Any]:
    from prometheos.domain.ids import new_id

    return {
        "op": "create",
        "kind": "entry",
        "id": new_id("entry"),
        "base_rev": 0,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": {
            "title": f"Synthetic observation {index}",
            "text": f"Run {index}: the candidate controller recovered energy in the test event. "
            * 3,
            "category": "note",
            "evidence": [],
            "claim_refs": [],
            "author_ref": None,
            "origin": "typed",
            "quantities": [],
            "review": "reviewed",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entries", type=int, default=1000)
    args = parser.parse_args()
    from cp1_support import Env
    from hub_support import running_hub

    from prometheos.app.reads import Caller

    with tempfile.TemporaryDirectory(prefix="prometheos-perf-") as tmp:
        env = Env(Path(tmp))
        batch = 100
        for start in range(0, args.entries, batch):
            env.apply([entry_op(i) for i in range(start, min(start + batch, args.entries))])
        agent = Caller(actor="perf")
        context_times = []
        for _ in range(20):
            began = time.perf_counter()
            env.svc.context(env.project_id, agent, purpose="orient", budget=12000)
            context_times.append(time.perf_counter() - began)
        write_times = []
        for i in range(50):
            began = time.perf_counter()
            env.apply([entry_op(args.entries + i)])
            write_times.append(time.perf_counter() - began)
        delivery = []
        with running_hub(env.svc, env.project_id) as hub:
            hub.pair()
            for i in range(20):
                seq = env.seq()
                committed: list[float] = []

                def commit(n: int = i, marks: list[float] = committed) -> None:
                    time.sleep(0.3)
                    marks.append(time.perf_counter())
                    env.apply([entry_op(10_000 + n)])

                worker = threading.Thread(target=commit)
                worker.start()
                reply = hub.request("GET", f"/api/v1/changes?after_seq={seq}&wait=10")
                arrived = time.perf_counter()
                worker.join()
                assert reply.status == 200 and reply.json()["changed"], reply.body
                delivery.append(arrived - committed[0])
    result = {
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        },
        "dataset": {"entries": args.entries, "context_budget_chars": 12000},
        "context_p95_s": round(p95(context_times), 4),
        "context_median_s": round(statistics.median(context_times), 4),
        "write_p95_s": round(p95(write_times), 4),
        "write_median_s": round(statistics.median(write_times), 4),
        "hub_update_p95_s": round(p95(delivery), 4),
        "targets": {"context_p95_s": 1.0, "write_p95_s": 0.5, "hub_update_p95_s": 2.0},
    }
    missed = [k for k, limit in result["targets"].items() if result[k] >= limit]
    result["missed"] = missed
    print(json.dumps(result, indent=2))
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
