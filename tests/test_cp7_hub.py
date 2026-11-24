"""CP7 hub routes over real HTTP: paper overview and pure check, safe previews of
retained manuscript/PDF/image bytes, jobs and the inbox. Owner session only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cp1_support import AGENT, Env
from cp7_support import PaperProject, anchor_body, create, paper_body, table_counts
from hub_support import Hub, running_hub

from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@pytest.fixture
def pp(tmp_path: Path) -> PaperProject:
    return PaperProject(Env(tmp_path))


@pytest.fixture
def hub(pp: PaperProject) -> Iterator[Hub]:
    with running_hub(pp.svc, pp.pid) as running:
        running.pair()
        yield running


def test_paper_routes_are_pure_reads(pp: PaperProject, hub: Hub) -> None:
    paper = pp.paper()
    anchor = pp.anchor(paper, pp.overbroad_line)
    seq, counts = pp.env.seq(), table_counts(pp.env.store_dir)
    overview = hub.request("GET", f"/api/v1/papers/{paper}").json()
    assert overview["paper"]["id"] == paper
    assert [a["id"] for a in overview["anchors"]] == [anchor]
    assert overview["manuscript_versions"][0]["id"] == pp.manuscript["version_id"]
    check = hub.request("GET", f"/api/v1/papers/{paper}/check")
    assert check.status == 200 and "venue_unknown" in {f["code"] for f in check.json()["findings"]}
    assert check.headers["cache-control"] == "no-store"
    assert pp.env.seq() == seq and table_counts(pp.env.store_dir) == counts
    missing = hub.request("GET", f"/api/v1/papers/{new_id('paper')}/check")
    assert missing.status == 404
    saved = hub.request("POST", f"/api/v1/papers/{paper}/check/save", {"request_id": None})
    assert saved.status == 200 and pp.env.seq() == seq + 1
    unpaired = hub.request("GET", f"/api/v1/papers/{paper}/check", cookie=None)
    assert unpaired.status == 401


def test_previews_have_their_own_policy_and_only_safe_types(pp: PaperProject, hub: Hub) -> None:
    research = pp.env.research
    (research / "draft.pdf").write_bytes(MINIMAL_PDF)
    (research / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    (research / "page.html").write_text("<script>alert(1)</script>", "utf-8")
    (research / "drawing.svg").write_text("<svg onload='alert(1)'/>", "utf-8")
    items = pp.env.import_files(["draft.pdf", "figure.png", "page.html", "drawing.svg"])
    pdf, png, html, svg = items["plan"]["items"]

    reply = hub.request("GET", f"/api/v1/versions/{pdf['version_id']}/content")
    assert reply.status == 200 and reply.body == MINIMAL_PDF
    assert reply.headers["content-type"] == "application/pdf"
    assert reply.headers["content-disposition"] == "inline"
    assert "script-src" not in reply.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in reply.headers["content-security-policy"]
    assert reply.headers["x-content-type-options"] == "nosniff"
    image = hub.request("GET", f"/api/v1/versions/{png['version_id']}/content")
    assert image.status == 200 and image.headers["content-type"] == "image/png"
    assert "sandbox" in image.headers["content-security-policy"]
    text = hub.request("GET", f"/api/v1/versions/{pp.manuscript['version_id']}/content")
    assert text.headers["content-type"] == "text/plain; charset=utf-8"
    assert "sandbox" in text.headers["content-security-policy"]
    for unsafe in (html, svg):
        refused = hub.request("GET", f"/api/v1/versions/{unsafe['version_id']}/content")
        assert refused.status == 422 and refused.json()["error"]["code"] == "unsupported"
    # The app's own pages keep the app policy.
    assert "script-src 'self'" in hub.request("GET", "/").headers["content-security-policy"]

    # A PDF manuscript takes page anchors, not line anchors.
    paper = new_id("paper")
    pp.env.apply([create("paper", paper_body(manuscript_source_id=pdf["source_id"]), paper)])
    page = anchor_body(
        paper, pdf["version_id"], 1, locator={"type": "pdf", "page": 1, "bbox": None}
    )
    pp.env.apply([create("anchor", page)])
    with pytest.raises(PrometheosError) as err:
        pp.env.apply([create("anchor", anchor_body(paper, pdf["version_id"], 1))])
    assert err.value.code == "validation_error"


def test_jobs_and_inbox_routes(pp: PaperProject, hub: Hub) -> None:
    start = datetime.now(UTC).replace(second=0, microsecond=0)
    body = {
        "handler": "digest",
        "parameters": {},
        "schedule": {
            "type": "interval",
            "minutes": 15,
            "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "enabled": True,
        "budget": {
            "max_inputs": 10,
            "max_output_chars": 1000,
            "max_provider_calls": 0,
            "max_concurrent_runs": 1,
            "spend_cap_estimate_usd": None,
        },
        "last_slot": None,
    }
    job_id = pp.svc.job_add(pp.pid, AGENT, body, new_request_id())["job_id"]
    pp.paper()  # a change after the job exists, so the digest has something to report
    pp.svc.jobs_tick(pp.pid, now=start + timedelta(minutes=16))
    listed = hub.request("GET", "/api/v1/jobs").json()
    assert listed["jobs"][0]["job"]["id"] == job_id
    inbox = hub.request("GET", "/api/v1/inbox").json()["items"]
    assert [i["category"] for i in inbox] == ["digest"]
    dismissed = hub.request("POST", f"/api/v1/inbox/{inbox[0]['id']}/dismiss")
    assert dismissed.status == 200
    assert hub.request("GET", "/api/v1/inbox").json()["items"] == []
    assert len(hub.request("GET", "/api/v1/inbox?include_dismissed=true").json()["items"]) == 1
    tick = hub.request("POST", "/api/v1/jobs/tick")
    assert tick.status == 200 and "ran" in tick.json()
    no_csrf = hub.request("POST", "/api/v1/jobs/tick", csrf=None)
    assert no_csrf.status == 403
