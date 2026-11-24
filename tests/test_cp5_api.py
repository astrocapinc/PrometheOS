"""C05-HUB API behaviour over real HTTP: the same services and envelopes as the CLI,
owner decisions as preview + digest-bound apply, the bounded change feed, source text
and status-code mapping."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry
from cp1_support import request as request_bytes
from hub_support import Hub, running_hub

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError
from prometheos.hub import app as hub_app
from prometheos.hub.security import SESSION_COOKIE
from prometheos.store import admission


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def hub(env: Env) -> Iterator[Hub]:
    with running_hub(env.svc, env.project_id) as running:
        running.pair()
        yield running


def _req(env: Env, ops: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(json.loads(request_bytes(env.project_id, ops, env.seq())))


def test_reads_match_the_service(env: Env, hub: Hub) -> None:
    rid = new_id("entry")
    env.apply([create_entry(rid, title="Shared note")])
    via_http = hub.request("GET", f"/api/v1/records/{rid}").json()
    assert via_http == json.loads(json.dumps(env.svc.get(env.project_id, OWNER, rid, None)))
    listed = hub.request("GET", "/api/v1/records?kind=entry").json()
    assert [r["id"] for r in listed["items"]] == [rid]
    notebook = hub.request("GET", "/api/v1/notebook").json()
    assert notebook["entries"][0]["title"] == "Shared note"
    assert "Shared" in hub.request("GET", "/api/v1/knowledge/search?q=Shared").body.decode()
    assert hub.request("GET", "/api/v1/next").json()["generation"] == "deterministic"
    history = hub.request("GET", f"/api/v1/records/{rid}/history").json()
    assert history["revisions"][0]["rev"] == 1


def test_status_codes_follow_the_error_codes(env: Env, hub: Hub) -> None:
    missing = hub.request("GET", f"/api/v1/records/{new_id('entry')}")
    assert missing.status == 404 and missing.json()["error"]["code"] == "not_found"
    stale = _req(env, [create_entry()])
    stale["expected_seq"] = env.seq() + 5
    conflict = hub.request("POST", "/api/v1/transactions", stale)
    assert conflict.status == 409 and conflict.json()["error"]["code"] == "revision_conflict"
    bad = hub.request("POST", "/api/v1/transactions", b"{not json")
    assert bad.status == 400
    paper = hub.request("GET", f"/api/v1/papers/{new_id('paper')}/check")
    # Paper checks exist since CP7: an unknown paper is 404 (422 "unsupported" is covered
    # by the preview of an HTML source in test_cp7_hub).
    assert paper.status == 404 and paper.json()["error"]["code"] == "not_found"


def test_protected_effects_need_the_two_step_owner_decision(env: Env, hub: Hub) -> None:
    note = new_id("entry")
    env.apply([create_entry(note)])
    archive = _req(env, [{"op": "archive", "id": note, "base_rev": 1}])
    direct = hub.request("POST", "/api/v1/transactions", archive)
    assert direct.status == 403  # protected: not through the plain transaction route
    preview = hub.request("POST", "/api/v1/decisions/preview", {"request": archive}).json()
    assert preview["protected_effects"] and preview["would_apply"]
    assert env.svc.get(env.project_id, OWNER, note, None)["lifecycle"] == "active"
    wrong = hub.request("POST", "/api/v1/decisions/apply", {"request": archive, "digest": "0" * 64})
    assert wrong.status == 409
    applied = hub.request(
        "POST", "/api/v1/decisions/apply", {"request": archive, "digest": preview["digest"]}
    )
    assert applied.status == 200, applied.body
    assert env.svc.get(env.project_id, OWNER, note, None)["lifecycle"] == "archived"
    again = hub.request(
        "POST", "/api/v1/decisions/apply", {"request": archive, "digest": preview["digest"]}
    )
    assert again.status == 200 and again.json()["result"]["replayed"] is True  # idempotent
    assert env.svc.get(env.project_id, OWNER, note, None)["rev"] == 2


def test_change_feed_wakes_on_commit_and_is_metadata_only(env: Env, hub: Hub) -> None:
    seq = env.seq()
    idle = hub.request("GET", f"/api/v1/changes?after_seq={seq}&wait=0").json()
    assert idle["changed"] == [] and idle["latest_seq"] == seq
    secret = new_id("entry")

    def later() -> None:
        time.sleep(0.6)
        env.apply([create_entry(secret, title="Late arrival", text="body text")])

    threading.Thread(target=later).start()
    started = time.monotonic()
    woke = hub.request("GET", f"/api/v1/changes?after_seq={seq}&wait=10").json()
    assert time.monotonic() - started < 8
    assert woke["changed"] == [{"id": secret, "kind": "entry", "rev": 1, "seq": seq + 1}]
    assert "Late arrival" not in json.dumps(woke)  # identifiers only
    assert hub.request("GET", "/api/v1/changes?after_seq=0&wait=21").status in (400, 422)


def test_meeting_capture_and_source_text(env: Env, hub: Hub) -> None:
    notes = "Supervision 2026-09-28\nAttach the plant result.\nWho owns the review?\n"
    captured = hub.request(
        "POST", "/api/v1/meetings",
        {"title": "Supervision", "text": notes, "held_at": "2026-09-28T10:00:00Z"},
    ).json()  # fmt: skip
    text = hub.request("GET", f"/api/v1/versions/{captured['version_id']}/text").json()
    assert text["text"] == notes and text["availability"] == "retained"
    reconcile = hub.request("GET", f"/api/v1/meetings/{captured['meeting_id']}/reconcile")
    assert reconcile.status == 200


def test_owner_hub_sees_restricted_records_agents_do_not(env: Env, hub: Hub) -> None:
    rid = new_id("entry")
    env.apply([create_entry(rid, sensitivity="restricted", title="Owner only")], caller=OWNER)
    assert hub.request("GET", f"/api/v1/records/{rid}").status == 200
    from prometheos.domain.models import PrometheosError

    with pytest.raises(PrometheosError):
        env.svc.get(env.project_id, AGENT, rid, None)


def test_skill_install_preview_takes_no_path_and_writes_nothing(
    env: Env, hub: Hub, tmp_path: Path
) -> None:
    from cp4_support import import_package, write_package

    import_package(env.svc, write_package(tmp_path / "src"))
    reply = hub.request(
        "POST", "/api/v1/skills/synthetic-evidence/install-preview", {"host": "opencode"}
    )
    assert reply.status == 200, reply.body
    plan = reply.json()["plan"]
    assert "target" not in plan
    assert {op["action"] for op in plan["operations"]} == {"create"}
    assert all(op["path"].startswith(".opencode/skills/") for op in plan["operations"])
    with_path = hub.request(
        "POST", "/api/v1/skills/synthetic-evidence/install-preview",
        {"host": "opencode", "target": str(tmp_path)},
    )  # fmt: skip
    assert with_path.status in (400, 422)
    detail = hub.request("GET", "/api/v1/skills/synthetic-evidence").json()
    assert detail["recipes"][0]["id"] == "evidence-pass" and "path" not in detail["origin"]


# ----- decision 0074: waiting change polls and the admission gate --------------------------


def _open_poll(hub: Hub, after_seq: int) -> socket.socket:
    """A raw long-poll whose connection the test can drop, as the hub does on navigation."""
    sock = socket.create_connection(("127.0.0.1", hub.port), timeout=30)
    sock.sendall(
        (
            f"GET /api/v1/changes?after_seq={after_seq}&wait=20 HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{hub.port}\r\nCookie: {SESSION_COOKIE}={hub.cookie}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
    )
    return sock


def _poll_reply(sock: socket.socket) -> dict[str, Any]:
    data = b""
    try:
        while chunk := sock.recv(65536):
            data += chunk
    finally:
        sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.split(b" ", 2)[1] == b"200", head
    return dict(json.loads(body))


@pytest.fixture
def gate_takes(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """When each project admission gate was taken, from any store connection in this process."""
    taken: list[float] = []
    original = admission.Gate.__init__

    def counting(self: admission.Gate, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        taken.append(time.monotonic())

    monkeypatch.setattr(admission.Gate, "__init__", counting)
    return taken


def _takes_during(taken: list[float], seconds: float) -> int:
    start = time.monotonic()
    time.sleep(seconds)
    return sum(1 for t in taken if start <= t < start + seconds)


def test_waiting_polls_share_one_probe_and_abandoned_polls_stop(
    env: Env, hub: Hub, gate_takes: list[float]
) -> None:
    seq = env.seq()
    before = len(gate_takes)
    env.svc.latest_seq(env.project_id)
    per_read = len(gate_takes) - before  # opening the store takes the gate too
    assert per_read >= 1
    polls = [_open_poll(hub, seq) for _ in range(8)]
    time.sleep(0.5)
    # Eight waiting polls read the store as one: about one probe per 0.25 s tick, where
    # each poll reading on its own made 8 x 4 = 32 reads a second.
    assert _takes_during(gate_takes, 1.0) <= 6 * per_read
    for sock in polls:
        sock.close()  # the hub aborts its poll on every navigation
    time.sleep(0.75)
    # Nobody is waiting any more: the abandoned polls stopped, not ran on for 20 s.
    assert _takes_during(gate_takes, 1.0) == 0


def test_a_write_gets_the_gate_past_waiting_and_abandoned_polls_and_wakes_them(
    env: Env, hub: Hub, gate_takes: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A long probe interval: only the hub's own wake can answer the polls within 2 s.
    monkeypatch.setattr(hub_app, "LONG_POLL_TICK", 6.0)
    seq = env.seq()
    abandoned = [_open_poll(hub, seq) for _ in range(12)]
    time.sleep(0.3)
    for sock in abandoned:
        sock.close()
    live = [_open_poll(hub, seq) for _ in range(12)]
    time.sleep(0.5)
    rid = new_id("entry")
    started = time.monotonic()
    wrote = hub.request("POST", "/api/v1/transactions", _req(env, [create_entry(rid)]))
    assert wrote.status == 200, wrote.body  # not busy: project_admission
    assert time.monotonic() - started < admission.TIMEOUT_MS / 1000
    replies = [_poll_reply(sock) for sock in live]
    # A write served by this hub wakes the waiting polls at once, not at the next probe.
    assert time.monotonic() - started < 2
    for reply in replies:
        assert reply["changed"] == [{"id": rid, "kind": "entry", "rev": 1, "seq": seq + 1}]


def test_a_failed_probe_keeps_its_typed_error(
    env: Env, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(project_id: str) -> int:
        raise PrometheosError("busy", "held elsewhere; retry", {"lock": "project_admission"})

    monkeypatch.setattr(env.svc, "latest_seq", busy)
    reply = hub.request("GET", f"/api/v1/changes?after_seq={env.seq()}&wait=1")
    # Not a 500 "internal error": the error raised in the wait keeps its envelope.
    assert reply.status == 503, reply.body
    assert reply.json()["error"]["code"] == "busy"
