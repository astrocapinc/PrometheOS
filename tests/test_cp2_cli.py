"""CP2 CLI surface: pure queries, explicit exports, owner decisions refused without a terminal."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from cp1_support import FIXTURES
from test_cp1_cli import Cli

from prometheos.domain.ids import new_id, new_request_id


def test_cp2_cli_queries_and_refusals(tmp_path: Path) -> None:
    research = tmp_path / "research"
    research.mkdir()
    shutil.copyfile(FIXTURES / "meeting.txt", research / "meeting.txt")
    cli = Cli(tmp_path)
    code, init, _ = cli(
        "project",
        "init",
        "--title",
        "CLI CP2",
        "--objective",
        "Undecided",
        "--root",
        f"research={research}",
    )
    pid = init["project_id"]
    code, captured, _ = cli(
        "meeting",
        "capture",
        "--title",
        "Supervision",
        "--notes",
        str(research / "meeting.txt"),
        "--held-at",
        "2026-09-28T10:00:00Z",
    )
    assert code == 0 and captured["meeting_id"].startswith("meeting_")
    code, recon, _ = cli("meeting", "reconcile", "--meeting", captured["meeting_id"])
    assert code == 0 and recon["items"] == []

    code, ctx, _ = cli("context", "show", "--budget", "2000")
    assert code == 0 and len(ctx["text"]) <= 2000 and ctx["manifest"]["caller_scope"] == "agent"
    code, nxt, _ = cli("context", "next")
    assert code == 0 and "candidates" in nxt
    code, _agenda, _ = cli("context", "agenda", "--meeting", captured["meeting_id"])
    assert code == 0
    code, found, _ = cli("knowledge", "search", "actuator")
    assert code == 0 and any(h["id"] == captured["version_id"] for h in found["hits"])
    code, links, _ = cli("knowledge", "links", captured["meeting_id"])
    assert code == 0 and any(o["id"] == captured["version_id"] for o in links["outgoing"])

    out = tmp_path / "notebook.md"
    code, _exported, _ = cli("notebook", "export", "--out", str(out))
    assert code == 0 and out.read_text(encoding="utf-8").startswith("# Notebook")
    code, _err, _ = cli("notebook", "export", "--out", str(out))
    assert code == 2  # never overwrites

    # Protected change: agent apply is denied; decision review refuses without a terminal.
    item = new_id("item")
    raw = json.dumps(
        {
            "schema_version": 1,
            "project_id": pid,
            "request_id": new_request_id(),
            "expected_seq": 2,
            "operations": [
                {
                    "op": "create",
                    "kind": "item",
                    "id": item,
                    "base_rev": 0,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": {
                        "meeting_id": captured["meeting_id"],
                        "type": "commitment",
                        "text": "Attach the plant result.",
                        "evidence": [],
                        "proposed_owner": None,
                        "due_at": None,
                        "status": "proposed",
                        "action_id": None,
                    },
                }
            ],
        }
    ).encode()
    code, _made, _ = cli("apply", "--file", "-", stdin=raw)
    assert code == 0
    action = new_id("action")
    adopt = {
        "schema_version": 1,
        "project_id": pid,
        "request_id": new_request_id(),
        "expected_seq": 3,
        "operations": [
            {
                "op": "create",
                "kind": "action",
                "id": action,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {
                    "title": "Attach plant result",
                    "reason": "Commitment",
                    "source_refs": [],
                    "owner_ref": None,
                    "due_at": None,
                    "dependencies": [],
                    "status": "proposed",
                    "blocker": None,
                    "completion_evidence": [],
                    "commitment_item_id": item,
                },
            },
            {
                "op": "transition",
                "id": action,
                "base_rev": 1,
                "to": "ready",
                "reason": "adopt",
                "evidence": [],
            },
            {
                "op": "transition",
                "id": item,
                "base_rev": 1,
                "to": "adopted",
                "reason": "adopt",
                "evidence": [],
            },
        ],
    }
    req = tmp_path / "adopt.json"
    req.write_text(json.dumps(adopt), encoding="utf-8")
    code, denied, _ = cli("apply", "--file", str(req))
    assert code == 4 and denied["error"]["code"] == "approval_required"  # CP10.2
    code, refused, _ = cli("decision", "review", "--file", str(req), "--apply")
    assert code == 4 and "interactive" in refused["error"]["message"]
    code, refused, _ = cli("decision", "grant", "--operation", "read_sensitive")
    assert code == 4
    code, refused, _ = cli("record", "list", "--scope", "owner")
    assert code == 4  # owner scope needs a capability
    code, _plan, _ = cli("retention", "purge", "plan", captured["source_id"])
    assert code == 4  # planning reveals restricted IDs: owner scope only
    from test_cp1_cli import synthetic_capability

    cap = tmp_path / "read.cap"
    cap.write_text(synthetic_capability(tmp_path, pid, "read_sensitive"), encoding="utf-8")
    code, plan, _ = cli(
        "retention", "purge", "plan", captured["source_id"], "--capability-file", str(cap)
    )
    assert code == 0 and captured["meeting_id"] in plan["closure"]
    code, refused, _ = cli("retention", "purge", "commit", plan["plan_id"])
    assert code == 4
