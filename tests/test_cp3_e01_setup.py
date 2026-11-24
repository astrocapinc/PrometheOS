"""The E01 live-host harness builds a usable disposable project (no host is started here)."""

from __future__ import annotations

from pathlib import Path

import e01_setup
from cp1_support import AGENT

from prometheos.app.service import Service


def test_e01_disposable_project_has_scoped_claims(tmp_path: Path) -> None:
    project = e01_setup.build_project(tmp_path)
    svc = Service(home=Path(project["home"]))
    claims = svc.list_records(project["project_id"], AGENT, kind="claim")["items"]
    statements = {c["id"]: c["body"]["statement"] for c in claims}
    assert "across operating conditions" in statements[project["overbroad_claim"]]
    assert "simplified constant-deceleration" in statements[project["narrow_claim"]]
    assert "SIMPLIFIED-A" in svc.context(project["project_id"], AGENT)["text"]
