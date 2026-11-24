"""Shared helpers for CP1 tests: disposable homes, research roots and requests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from prometheos.app.authority import OwnerAct
from prometheos.app.reads import Caller
from prometheos.app.service import Service
from prometheos.domain.ids import new_id, new_request_id

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"
WORKER = Path(__file__).resolve().parent / "helpers" / "worker.py"
OWNER = Caller(actor="test", scope="owner")
AGENT = Caller(actor="test", scope="agent")
FULL = Caller(actor="test", scope="owner", full_data=True)
# An owner decision made in an owner channel (tests stand in for the interactive confirmation
# or the paired hub; A02, CP10.2).
OWNER_ACT = OwnerAct(channel="cli-interactive", actor="test-owner")
# MCP tools that exist only to request an owner decision: they always answer approval_required
# (or the operation's own refusal) and never act (A02, CP10.2; decision 0016).
CP10_REQUEST_TOOLS = frozenset(
    {
        "tex_engine_install", "audio_models_install", "audio_models_remove", "skill_trust",
        "recording_start", "job_resolve", "overleaf_push", "provider_run", "provider_configure",
        "retention_purge_commit",
    }
)  # fmt: skip


class Env:
    """A disposable PrometheOS home plus one research root with real fixture bytes."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.research = tmp / "research"
        self.research.mkdir()
        for name in ("baseline.md", "plant-result.md", "meeting.txt", "manuscript.md"):
            shutil.copyfile(FIXTURES / name, self.research / name)
        (self.research / "artifact.bin").write_bytes(bytes(range(256)) * 64)
        (self.research / "sub").mkdir()
        (self.research / "sub" / "notes.txt").write_text("line one\nline two\n", "utf-8")
        self.svc = Service(home=self.home, actor="test")
        result = self.svc.init_project(
            title="Synthetic KERS study",
            objective="Compare the candidate controller with the reference within stated regimes.",
            timezone="Europe/London",
            roots={"research": str(self.research)},
        )
        self.project_id: str = result["project_id"]
        self.store_dir = Path(result["store"])

    def seq(self) -> int:
        return int(self.svc.project_info(self.project_id, OWNER)["project_seq"])

    def apply(
        self,
        ops: list[dict[str, Any]],
        expected: int | None = None,
        rid: str | None = None,
        caller: Caller | None = None,
    ) -> dict[str, Any]:
        seq = self.seq() if expected is None else expected
        return self.svc.apply(self.project_id, request(self.project_id, ops, seq, rid), caller)

    def apply_protected(
        self, ops: list[dict[str, Any]], caller: Caller | None = None
    ) -> dict[str, Any]:
        """Apply with a synthetic owner decision bound to this exact request (tests only)."""
        raw = request(self.project_id, ops, self.seq())
        digest = self.svc.review_request(self.project_id, raw, caller)["digest"]
        grant = self.svc.grant(
            self.project_id,
            operation="exact_request",
            digest=digest,
            label="SYNTHETIC-TEST owner decision",
            synthetic=True,
        )
        return self.svc.apply(self.project_id, raw, caller, grant.secret)

    def import_files(
        self, paths: list[str], retention: str = "copy", sensitivity: str = "normal"
    ) -> dict[str, Any]:
        plan = self.svc.import_plan(
            self.project_id,
            root_alias="research",
            paths=paths,
            retention=retention,
            sensitivity=sensitivity,
        )
        commit = self.svc.import_commit(self.project_id, plan["plan_id"])
        return {"plan": plan, "commit": commit}

    def worker(
        self, *args: str, fault: str | None = None, timeout: float = 120
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PROMETHEOS_HOME"] = str(self.home)
        env.pop("PROMETHEOS_FAULT_POINT", None)
        if fault:
            env["PROMETHEOS_FAULT_POINT"] = fault
        return subprocess.run(
            [sys.executable, str(WORKER), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def request(
    project_id: str, ops: list[dict[str, Any]], expected_seq: int, rid: str | None = None
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "project_id": project_id,
            "request_id": rid or new_request_id(),
            "expected_seq": expected_seq,
            "operations": ops,
        }
    ).encode("utf-8")


def entry_body(title: str = "A note", text: str = "Observation.", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": title,
        "text": text,
        "category": "note",
        "evidence": [],
        "claim_refs": [],
        "author_ref": None,
        "origin": "typed",
        "quantities": [],
        "review": "unreviewed",
    }
    body.update(extra)
    return body


def create_entry(
    record_id: str | None = None, sensitivity: str = "normal", **body: Any
) -> dict[str, Any]:
    return {
        "op": "create",
        "kind": "entry",
        "id": record_id or new_id("entry"),
        "base_rev": 0,
        "sensitivity": sensitivity,
        "occurred_at": None,
        "body": entry_body(**body),
    }


def counts(store_dir: Path) -> dict[str, int]:
    import sqlite3

    conn = sqlite3.connect(store_dir / "record.sqlite")
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in (
                "records",
                "revisions",
                "bodies",
                "audit",
                "requests",
                "relations",
                "media",
                "payloads",
                "source_checks",
            )
        }
    finally:
        conn.close()
