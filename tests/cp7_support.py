"""Shared helpers for CP7 tests: a synthetic manuscript project with claims, a paper,
anchors and a dated venue guide. Synthetic fixtures only; nothing here says anything
about a real venue's rules or a real paper."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from cp1_support import OWNER, Env, request

from prometheos.domain.ids import new_id

VENUE_GUIDE = """# SYNTHETIC venue guide (test fixture, not a real venue)

Captured for tests only. Requirements:

- Manuscripts must not exceed eight pages including references.
- [ ] State the model fidelity of every simulated result.
1. Provide a data availability statement.

Unrelated prose that is not a list item.
"""

SCOPE_OVERBROAD = {
    "system": "candidate controller",
    "operating_regime": "across operating conditions",
    "comparator": "reference controller",
    "model_fidelity": "unspecified",
    "validation_level": "unspecified",
}
SCOPE_SIMPLIFIED = {
    "system": "synthetic braking event SIMPLIFIED-A",
    "operating_regime": "one constant-deceleration event",
    "comparator": "synthetic reference controller, same event",
    "model_fidelity": "algebraic energy balance; no actuator dynamics",
    "validation_level": "none (invented test numbers)",
}
OVERBROAD_LINE = "The candidate improves energy recovery across operating conditions."


def line_of(path: Path, needle: str) -> int:
    for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if needle in line:
            return number
    raise AssertionError(needle)


def text_evidence(item: dict[str, Any], line: int, quote: str | None = None) -> dict[str, Any]:
    return {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": line, "end_line": line},
        "quote_sha256": hashlib.sha256(quote.encode()).hexdigest() if quote else None,
    }


def create(
    kind: str, body: dict[str, Any], rid: str | None = None, sensitivity: str = "normal"
) -> dict[str, Any]:
    return {
        "op": "create",
        "kind": kind,
        "id": rid or new_id(kind),
        "base_rev": 0,
        "sensitivity": sensitivity,
        "occurred_at": None,
        "body": body,
    }


def claim_body(statement: str, scope: dict[str, str], evidence: list[Any]) -> dict[str, Any]:
    return {
        "statement": statement,
        "scope": scope,
        "evidence": evidence,
        "assessment_refs": [],
        "state": "proposed",
        "superseded_by": None,
    }


def paper_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Synthetic KERS manuscript",
        "objective": "Report the simplified comparison within its stated regime.",
        "claim_refs": [],
        "manuscript_source_id": None,
        "venue_source_refs": [],
        "status": "drafting",
        "deadline": None,
        "requirements": [],
        "contribution": "A scoped comparison on synthetic events.",
        "related_papers": [],
        "review_rounds": [],
    }
    body.update(overrides)
    return body


def anchor_body(paper_id: str, version_id: str, line: int, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "paper_id": paper_id,
        "manuscript_version_id": version_id,
        "locator": {"type": "text", "start_line": line, "end_line": line},
        "claim_refs": [],
        "evidence": [],
        "status": "current",
        "reason": None,
    }
    body.update(overrides)
    return body


class PaperProject:
    """Env plus imported manuscript, baseline, plant result and venue guide."""

    def __init__(self, env: Env) -> None:
        self.env = env
        self.svc = env.svc
        self.pid = env.project_id
        (env.research / "venue-guide.md").write_text(VENUE_GUIDE, "utf-8")
        imported = env.import_files(
            ["manuscript.md", "baseline.md", "plant-result.md", "venue-guide.md"]
        )
        self.manuscript, self.baseline, self.plant, self.venue = imported["plan"]["items"]
        self.overbroad_line = line_of(env.research / "manuscript.md", OVERBROAD_LINE)

    def get(self, rid: str) -> dict[str, Any]:
        return self.svc.get(self.pid, OWNER, rid, None)

    def grant(self, ops: list[dict[str, Any]]) -> tuple[bytes, str]:
        raw = request(self.pid, ops, self.env.seq())
        digest = self.svc.review_request(self.pid, raw, OWNER)["digest"]
        grant = self.svc.grant(
            self.pid,
            operation="exact_request",
            digest=digest,
            label="SYNTHETIC-TEST owner decision",
            synthetic=True,
        )
        return raw, grant.secret

    def protected(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        raw, secret = self.grant(ops)
        return self.svc.apply(self.pid, raw, OWNER, secret)

    def claim(self, statement: str, scope: dict[str, str], line_item: dict[str, Any]) -> str:
        cid = new_id("claim")
        self.env.apply([create("claim", claim_body(statement, scope, [line_item]), cid)])
        return cid

    def paper(self, **overrides: Any) -> str:
        rid = new_id("paper")
        body = paper_body(manuscript_source_id=self.manuscript["source_id"], **overrides)
        self.env.apply([create("paper", body, rid)])
        return rid

    def anchor(self, paper_id: str, line: int, **overrides: Any) -> str:
        rid = new_id("anchor")
        body = anchor_body(paper_id, self.manuscript["version_id"], line, **overrides)
        self.env.apply([create("anchor", body, rid)])
        return rid

    def revise(self, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        current = self.get(rid)
        return self.env.apply(
            [
                {
                    "op": "revise",
                    "id": rid,
                    "base_rev": current["rev"],
                    "sensitivity": current["sensitivity"],
                    "occurred_at": None,
                    "body": body,
                }
            ]
        )

    def transition(self, rid: str, to: str, reason: str | None = None) -> dict[str, Any]:
        return self.env.apply([self.transition_op(rid, to, reason)])

    def transition_op(self, rid: str, to: str, reason: str | None = None) -> dict[str, Any]:
        return {
            "op": "transition",
            "id": rid,
            "base_rev": self.get(rid)["rev"],
            "to": to,
            "reason": reason,
            "evidence": [],
        }


def table_counts(store_dir: Path) -> dict[str, int]:
    conn = sqlite3.connect(store_dir / "record.sqlite")
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in ("records", "revisions", "audit", "requests", "relations", "source_checks")
        }
    finally:
        conn.close()
