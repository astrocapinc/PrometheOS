"""Disposable synthetic project for the live-host gate (E01): fixtures only, no real research."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from prometheos.app.service import Service
from prometheos.domain.ids import new_id, new_request_id

ROOT = Path(__file__).resolve().parents[1]
SCOPES = {
    "simplified": {
        "system": "synthetic braking event SIMPLIFIED-A",
        "operating_regime": "one constant-deceleration event",
        "comparator": "synthetic reference controller, same event",
        "model_fidelity": "algebraic energy balance; no actuator dynamics",
        "validation_level": "none (invented test numbers)",
    },
    "overbroad": {
        "system": "candidate controller",
        "operating_regime": "across operating conditions",
        "comparator": "reference controller",
        "model_fidelity": "unspecified",
        "validation_level": "unspecified",
    },
}


def _claim(cid: str, statement: str, scope: dict[str, str], ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "op": "create",
        "kind": "claim",
        "id": cid,
        "base_rev": 0,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": {
            "statement": statement,
            "scope": scope,
            "evidence": [ev],
            "assessment_refs": [],
            "state": "proposed",
            "superseded_by": None,
        },
    }


def build_project(work: Path) -> dict[str, Any]:
    research = work / "research"
    research.mkdir(parents=True)
    for name in ("baseline.md", "plant-result.md", "meeting.txt", "manuscript.md"):
        shutil.copyfile(ROOT / "fixtures" / name, research / name)
    svc = Service(home=work / "home", actor="e01-setup")
    pid = svc.init_project(
        title="Synthetic KERS study (E01 disposable)",
        objective="Compare the candidate controller with the reference within stated regimes.",
        timezone=None,
        roots={"research": str(research)},
    )["project_id"]
    plan = svc.import_plan(
        pid, root_alias="research", paths=["baseline.md"], retention="copy", sensitivity="normal"
    )
    svc.import_commit(pid, plan["plan_id"])
    item = plan["items"][0]
    ev = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": 7, "end_line": 7},
        "quote_sha256": None,
    }
    overbroad, narrow = new_id("claim"), new_id("claim")
    seq = svc.project_info(pid, _owner())["project_seq"]
    request = {
        "schema_version": 1,
        "project_id": pid,
        "request_id": new_request_id(),
        "expected_seq": seq,
        "operations": [
            _claim(
                overbroad,
                "The candidate improves energy recovery across operating conditions.",
                SCOPES["overbroad"],
                ev,
            ),
            _claim(
                narrow,
                "In the simplified constant-deceleration event only, the "
                "candidate recovered more energy than the reference.",
                SCOPES["simplified"],
                ev,
            ),
        ],
    }
    svc.apply(pid, json.dumps(request).encode())
    return {
        "project_id": pid,
        "overbroad_claim": overbroad,
        "narrow_claim": narrow,
        "home": str(work / "home"),
        "research_root": str(research),
    }


def _owner() -> Any:
    from prometheos.app.reads import Caller

    return Caller(actor="e01-setup", scope="owner")
