"""A fresh synthetic PrometheOS home for the desktop shell's tests (CP19.2, decision 0054).

Usage: <main venv python> desktop/app/test/seed.py <empty home folder>
       <main venv python> desktop/app/test/seed.py bulk <home> <project id> <n>
       <main venv python> desktop/app/test/seed.py late <home> <project id> <seconds back>

The first form creates one project (Asia/Riyadh) and one work item an agent proposed, which
raises a research event (work_item.created) for the shell to announce, and prints
{"home", "project_id", "event_id"}. ``bulk`` adds n more agent suggestions (n more events).
``late`` adds an owner work item due in two days and runs one detector pass stamped that many
seconds in the past, as a pass that waited for the write lock would (review s3c1-a R2-F1); it
prints the new deadline events. Synthetic data only.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from prometheos.app.reads import Caller
from prometheos.app.service import Service
from prometheos.domain.ids import new_id, new_request_id

AGENT = Caller(actor="seed-agent", scope="agent")
OWNER = Caller(actor="seed", scope="owner")


def work(title: str, status: str = "proposed", due_at: str | None = None) -> dict[str, Any]:
    return {
        "title": title, "reason": "Synthetic test item.", "source_refs": [], "owner_ref": None,
        "due_at": due_at, "dependencies": [], "status": status, "blocker": None,
        "completion_evidence": [], "commitment_item_id": None, "description": None, "labels": [],
    }  # fmt: skip


def request(svc: Service, pid: str, operations: list[dict[str, Any]]) -> bytes:
    seq = svc.project_info(pid, OWNER)["project_seq"]
    return json.dumps(
        {"schema_version": 1, "project_id": pid, "request_id": new_request_id(),
         "expected_seq": seq, "operations": operations}
    ).encode("utf-8")  # fmt: skip


def create(body: dict[str, Any], action_id: str) -> dict[str, Any]:
    return {"op": "create", "kind": "action", "id": action_id, "base_rev": 0,
            "sensitivity": "normal", "occurred_at": None, "body": body}  # fmt: skip


def suggest(svc: Service, pid: str, title: str) -> None:
    svc.apply(pid, request(svc, pid, [create(work(title), new_id("action"))]), AGENT)


def fresh(home: Path) -> int:
    if home.exists() and any(home.iterdir()):
        print(f"refusing to reuse a non-empty folder: {home}", file=sys.stderr)
        return 2
    svc = Service(home=home, actor="seed")
    project = svc.init_project(
        title="Desktop shell test (synthetic)",
        objective="Show the hub in the desktop window.",
        timezone="Asia/Riyadh",
    )
    pid = project["project_id"]
    suggest(svc, pid, "Tidy the figure folder")
    events = svc.event_list(pid, OWNER)["items"]
    print(json.dumps({"home": str(home), "project_id": pid, "event_id": events[0]["event_id"]}))
    return 0


def bulk(home: Path, pid: str, n: int) -> int:
    svc = Service(home=home, actor="seed")
    before = {e["event_id"] for e in svc.event_list(pid, OWNER, limit=200, state="all")["items"]}
    for i in range(n):
        suggest(svc, pid, f"Suggestion {i + 1:03d}")
    added, cursor = [], None
    while True:
        page = svc.event_list(pid, OWNER, limit=200, cursor=cursor)
        added += [e["event_id"] for e in page["items"] if e["event_id"] not in before]
        cursor = page["next_cursor"]
        if not cursor:
            break
    print(json.dumps({"added": n, "event_ids": added}))
    return 0


def late(home: Path, pid: str, seconds_back: int) -> int:
    svc = Service(home=home, actor="seed")
    action_id = new_id("action")
    due = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    adopt = {"op": "transition", "id": action_id, "base_rev": 1, "to": "ready",
             "reason": "Added by the owner", "evidence": []}  # fmt: skip
    raw = request(svc, pid, [create(work("Submit the abstract", due_at=due), action_id), adopt])
    digest = svc.review_request(pid, raw, None)["digest"]
    grant = svc.grant(pid, operation="exact_request", digest=digest,
                      label="SYNTHETIC-TEST owner decision", synthetic=True)  # fmt: skip
    svc.apply(pid, raw, None, grant.secret)
    before = {e["event_id"] for e in svc.event_list(pid, OWNER, limit=200, state="all")["items"]}
    svc.events_detect(pid, now=datetime.now(UTC) - timedelta(seconds=seconds_back))
    after = svc.event_list(pid, OWNER, event_type="deadline.approaching")["items"]
    print(json.dumps({"deadline_events": [e["event_id"] for e in after
                                          if e["event_id"] not in before]}))  # fmt: skip
    return 0


def main() -> int:
    if sys.argv[1] == "bulk":
        return bulk(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
    if sys.argv[1] == "late":
        return late(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
    return fresh(Path(sys.argv[1]).resolve())


if __name__ == "__main__":
    sys.exit(main())
