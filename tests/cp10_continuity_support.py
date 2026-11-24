"""Shared scenario for the CP10.4 continuity tests (S26, K1-K4 shaped; synthetic fixtures only).

``Scenario`` builds the "given" of S26 on a disposable project through the service:

- two scoped findings (``claim_a`` relies on the assumption, ``claim_b`` is an unrelated control);
- an assumption entry shared by that finding and a manuscript anchor (``used_in``), with a
  replacement assumption for the correction;
- an adopted action that works from ``claim_a`` and is due;
- an external simulation registered as running (``run_live``) and another that will complete
  (``run_done``);
- an execution receipt imported without a registration (``bare_receipt``);
- a reference-only artifact (``notes.txt``) next to the retained ones.

Names map to IDs through ``names`` so the walk can normalise outcomes across transports.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cp1_support import AGENT, OWNER, Env, request
from cp7_support import (
    SCOPE_SIMPLIFIED,
    PaperProject,
    create,
    line_of,
    text_evidence,
)

from prometheos.domain.ids import new_id, new_request_id


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_body(title: str, text: str, category: str = "note", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": title,
        "text": text,
        "category": category,
        "evidence": [],
        "claim_refs": [],
        "author_ref": None,
        "origin": "typed",
        "quantities": [],
        "review": "unreviewed",
    }
    body.update(extra)
    return body


def receipt_payload(outputs: list[dict[str, Any]], reporter: str = "sim agent") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "observation": "reported",
        "reporter": reporter,
        "command": "simulate --regime variable-demand",
        "working_root": "research",
        "environment": {"os": "TestOS", "tools": [{"name": "SimTool", "version": "1.0"}]},
        "started_at": "2026-10-05T06:36:30Z",
        "ended_at": "2026-10-05T06:36:41Z",
        "exit_status": 0,
        "outcome": "succeeded",
        "inputs": [],
        "outputs": outputs,
        "limitations": ["synthetic test"],
    }


class Scenario:
    def __init__(self, tmp: Path) -> None:
        self.env = Env(tmp)
        self.svc, self.pid = self.env.svc, self.env.project_id
        self.names: dict[str, str] = {}
        (self.env.research / "model.slx").write_bytes(b"PK\x03\x04 synthetic model" * 40)
        self.paper = PaperProject(self.env)
        self.plant = self.paper.plant
        self.overbroad_line = line_of(self.env.research / "plant-result.md", "Recovered energy")
        # A reference-only artifact: PrometheOS holds only its digest.
        refs = self.env.import_files(["sub/notes.txt"], retention="reference")
        self.reference = refs["plan"]["items"][0]
        self.name(self.reference["version_id"], "reference_version")
        self.name(self.plant["version_id"], "plant_version")

    # ----- naming ---------------------------------------------------------------------
    def name(self, record_id: str, label: str) -> str:
        self.names[record_id] = label
        return record_id

    def normalise(self, value: Any) -> Any:
        """Replace known IDs with their names so two stores compare equal."""
        text = json.dumps(value, sort_keys=True)
        for record_id, label in sorted(self.names.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(record_id, f"<{label}>")
        text = re.sub(
            r"[a-z]+_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            "<id>",
            text,
        )
        text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", "<time>", text)
        text = re.sub(
            r"(test|mcp|http-agent|cli) asked for your decision",
            "<actor> asked for your decision",
            text,
        )
        text = re.sub(r"project sequence \d+", "project sequence <n>", text)
        text = re.sub(r"seq \d+", "seq <n>", text)
        return json.loads(text)

    # ----- the given ------------------------------------------------------------------
    def build(self, *, supported_scope: dict[str, str] | None = None) -> Scenario:
        env, paper = self.env, self.paper
        evidence = text_evidence(self.plant, self.overbroad_line)
        self.claim_a = self.name(
            paper.claim(
                "The candidate recovers less energy than the reference in the variable-demand "
                "regime.",
                (supported_scope if supported_scope is not None else SCOPE_SIMPLIFIED)
                | {"objective": "Replace the baseline controller?"},
                evidence,
            ),
            "claim_a",
        )
        self.claim_b = self.name(
            paper.claim("Unrelated control finding.", SCOPE_SIMPLIFIED, evidence), "claim_b"
        )
        self.drag = self.name(new_id("entry"), "assumption_drag")
        self.drag_v2 = self.name(new_id("entry"), "assumption_drag_v2")
        env.apply(
            [
                create(
                    "entry",
                    entry_body(
                        "Constant drag", "Drag is constant across speeds.", category="assumption"
                    ),
                    self.drag,
                ),
                create(
                    "entry",
                    entry_body(
                        "Speed-dependent drag",
                        "Drag grows with the square of speed.",
                        category="assumption",
                    ),
                    self.drag_v2,
                ),
            ]
        )
        self.paper_id = self.name(paper.paper(claim_refs=[]), "paper")
        self.anchor = self.name(paper.anchor(self.paper_id, 3), "anchor_uses_assumption")
        self.anchor_claim = self.name(
            paper.anchor(self.paper_id, 5, claim_refs=[{"id": self.claim_a, "rev": 1}]),
            "anchor_uses_claim",
        )
        self.anchor_control = self.name(
            paper.anchor(self.paper_id, 7, claim_refs=[{"id": self.claim_b, "rev": 1}]),
            "anchor_control",
        )
        env.apply(
            [
                {
                    "op": "relation_add",
                    "source_ref": {"id": self.claim_a, "rev": 1},
                    "target_ref": {"id": self.drag, "rev": 1},
                    "relation": "depends_on",
                },
                {
                    "op": "relation_add",
                    "source_ref": {"id": self.drag, "rev": 1},
                    "target_ref": {"id": self.anchor, "rev": 1},
                    "relation": "used_in",
                },
            ]
        )
        # An adopted action working from claim_a, overdue.
        self.action = self.name(new_id("action"), "action")
        env.apply(
            [
                create(
                    "action",
                    {
                        "title": "Rerun the sweep with the new drag model",
                        "reason": "Finding A decides the controller choice.",
                        "source_refs": [{"id": self.claim_a, "rev": 1}],
                        "owner_ref": None,
                        "due_at": iso(datetime.now(UTC) - timedelta(days=1)),
                        "dependencies": [],
                        "status": "proposed",
                        "blocker": None,
                        "completion_evidence": [],
                        "commitment_item_id": None,
                    },
                    self.action,
                )
            ]
        )
        paper.protected([paper.transition_op(self.action, "ready", None)])
        # External runs: one will stay running, one will complete.
        self.run_live = self.name(
            self.register("Plant sweep (live)")["results"][0]["id"], "run_live"
        )
        self.run_done = self.name(
            self.register("Plant sweep (done)")["results"][0]["id"], "run_done"
        )
        self.report(self.run_live, "reported_running")
        self.report(self.run_done, "reported_running")
        # A receipt nobody registered a run for.
        self.bare_receipt = self.receipt("Bare receipt (no registration)")
        self.name(self.bare_receipt["version_id"], "bare_receipt")
        self.name(self.bare_receipt["source_id"], "bare_receipt_source")
        return self

    # ----- helpers --------------------------------------------------------------------
    def register(self, title: str, caller: Any = AGENT, rid: str | None = None) -> dict[str, Any]:
        return self.svc.execution_register(
            self.pid,
            caller,
            title=title,
            host_label="lab workstation",
            reporter="sim agent",
            command_summary="simulate --regime variable-demand",
            working_root_alias="research",
            external_ref="sweep-1",
            expected_outputs=["results/sweep.csv"],
            request_id=rid or new_request_id(),
        )

    def report(
        self, execution_id: str, state: str, receipts: list[str] | None = None, caller: Any = AGENT
    ) -> dict[str, Any]:
        return self.svc.execution_report(
            self.pid,
            caller,
            execution_id,
            state=state,
            receipt_version_ids=receipts or [],
            request_id=new_request_id(),
        )

    def receipt(self, title: str) -> dict[str, Any]:
        outputs = [
            {
                "source_id": self.plant["source_id"],
                "version_id": self.plant["version_id"],
                "sha256": self.plant["sha256"],
                "role": "result",
            }
        ]
        commit = self.svc.receipt_record(
            self.pid,
            raw=json.dumps(receipt_payload(outputs)).encode("utf-8"),
            title=title,
            description="",
            request_id=new_request_id(),
        )
        source = next(r["id"] for r in commit["results"] if r["id"].startswith("source_"))
        version = next(r["id"] for r in commit["results"] if r["id"].startswith("version_"))
        return {"source_id": source, "version_id": version}

    def supersede_correction(self) -> tuple[dict[str, Any], str]:
        """The agent's proposal: supersede the shared assumption with its replacement."""
        correction_id = self.name(new_id("correction"), "correction")
        body = {
            "target_refs": [{"id": self.drag, "rev": 1}],
            "replacement_refs": [{"id": self.drag_v2, "rev": 1}],
            "reason": "The constant-drag assumption fails at high speed.",
            "evidence": [],
            "effects": [
                {
                    "target_ref": {"id": self.drag, "rev": 1},
                    "effect": "supersede",
                    "replacement_ref": {"id": self.drag_v2, "rev": 1},
                    "replacement_text": None,
                }
            ],
            "status": "proposed",
            "applied_seq": None,
        }
        self.env.apply([create("correction", body, correction_id)], caller=AGENT)
        apply_op = {"op": "correction_apply", "id": correction_id, "base_rev": 1}
        return apply_op, correction_id

    # ----- store assertions -----------------------------------------------------------
    def flags(self) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.env.store_dir / "record.sqlite")) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT record_id, reason, cause_id, created_seq FROM view_flags "
                "WHERE cleared_seq IS NULL ORDER BY record_id"
            ).fetchall()

    def body(self, record_id: str) -> dict[str, Any]:
        return dict(self.svc.get(self.pid, OWNER, record_id, None)["body"])

    def raw_request(self, ops: list[dict[str, Any]]) -> bytes:
        return request(self.pid, ops, self.env.seq())
