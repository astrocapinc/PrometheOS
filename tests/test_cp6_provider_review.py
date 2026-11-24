"""Provider-side regression tests from the CP6 pre-checkpoint review (reviewer-subagent-cp6-a,
decision 0013 "Review round"), run by the C06-AI check so every property its summary claims has a
test in its own commands: no proxy for provider calls, bytes sent equal to the bytes approved,
no hidden counts or ledger rows for agent scope, agent totals without call counts. Each test
failed before its fix; the reviewer's probes P1, P2 and P4 are adapted here."""

from __future__ import annotations

import http.server
import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, create_entry
from provider_support import ProviderEnv, completion

from prometheos.app import tasks
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError
from prometheos.providers import transport as tp


@pytest.fixture
def penv(tmp_path: Path) -> ProviderEnv:
    return ProviderEnv(tmp_path)


# ----- H1: no proxy, ever, for provider calls ------


def serve(handler_name: str, seen: list[str]) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append(f"{handler_name} {self.path} {self.headers.get('Authorization')}")
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_a_local_route_goes_directly_even_when_a_proxy_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    proxy, proxy_url = serve("PROXY", seen)
    target, target_url = serve("TARGET", seen)
    try:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, proxy_url)
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        # Name the loopback address in a way no_proxy defaults could not rescue (a hostname).
        reply = tp.UrllibTransport().post(
            f"{target_url}/v1/chat/completions", {"Authorization": "Bearer abc"}, b"{}", 10
        )
        assert reply.status == 200
        assert seen == ["TARGET /v1/chat/completions Bearer abc"]  # nothing reached the proxy
    finally:
        proxy.shutdown()
        proxy.server_close()
        target.shutdown()
        target.server_close()


def test_the_transport_opener_has_no_proxy_handler_with_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's probe P4: the handler may exist but must carry no proxies."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    seen: dict[str, Any] = {}
    real = urllib.request.build_opener

    def spy(*handlers: Any) -> Any:
        opener = real(*handlers)
        seen["proxies"] = [
            h.proxies for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)
        ]
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", spy)
    with pytest.raises(tp.NotSent):
        tp.UrllibTransport().post("http://127.0.0.1:9/v1/chat/completions", {}, b"{}", 5)
    # An empty ProxyHandler registers nothing, so no handler may carry proxies at all.
    assert all(not p for p in seen["proxies"])


# ----- M1: the bytes sent are the bytes approved ------


def test_a_change_after_the_approved_preview_never_reaches_the_provider(
    penv: ProviderEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    penv.configure()
    shown = penv.preview(purpose="notebook_draft", meeting_id=None)
    grant = penv.svc.grant(
        penv.project_id, operation="provider_run", digest=shown["digest"], label="SYNTHETIC-TEST"
    )
    real_render = tasks.render
    calls = {"n": 0}

    def render(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        found = real_render(*a, **k)
        if calls["n"] == 2:  # run()'s own build, after its internal preview matched the digest
            return tasks.Rendered(
                found.text + "\nINJECTED-AFTER-APPROVAL", found.manifest, found.refs, found.seq,
                found.rank, found.excluded_hidden,
            )  # fmt: skip
        return found

    monkeypatch.setattr(tasks, "render", render)
    kwargs: dict[str, Any] = {
        "purpose": "notebook_draft", "meeting_id": None, "context_budget": 12000,
        "include_restricted": False, "approved_digest": shown["digest"],
        "capability": grant.secret,
    }  # fmt: skip
    penv.script()  # any call would fail the test: nothing may be sent
    with pytest.raises(PrometheosError) as err:
        penv.svc.provider_run(penv.project_id, **kwargs)
    assert (
        err.value.code == "revision_conflict"
        and "changed after it was reviewed" in err.value.message
    )
    assert penv.transport.requests == []
    assert penv.svc.provider_calls(penv.project_id, OWNER)["calls"] == []  # nothing reserved
    assert not [t for t in penv.svc.list_records(penv.project_id, OWNER, kind="task")["items"]]
    # The grant was not spent: with the render restored it still authorises the same run.
    monkeypatch.setattr(tasks, "render", real_render)
    penv.script(completion({"purpose": "notebook_draft", "entry": _entry()}))
    assert penv.svc.provider_run(penv.project_id, **kwargs)["ok"]
    sent = canonical_sha256(penv.transport.requests[0][2])
    assert sent == shown["transmits"]["payload_sha256"]


def _entry() -> dict[str, Any]:
    return {
        "title": "t", "text": "x", "category": "note", "evidence": [], "claim_refs": [],
        "author_ref": None, "origin": "agent", "quantities": [], "review": "unreviewed",
    }  # fmt: skip


# ----- M2: an agent cannot count hidden records ------


def test_an_agent_preview_is_identical_whether_or_not_restricted_records_exist(
    penv: ProviderEnv, tmp_path: Path
) -> None:
    penv.configure()
    before = penv.preview(purpose="notebook_draft", meeting_id=None)["inputs"]
    penv.apply([create_entry(title="Hidden", text="private", sensitivity="restricted")])
    after = penv.preview(purpose="notebook_draft", meeting_id=None)["inputs"]
    assert before == after and after["excluded_above_scope"] is None
    owner = penv.preview(OWNER, purpose="notebook_draft", meeting_id=None)["inputs"]
    assert owner["excluded_above_scope"] == 1  # the owner may see the count


# ----- M3: the ledger is filtered by rank ------


def test_the_call_ledger_hides_restricted_runs_from_agent_scope(penv: ProviderEnv) -> None:
    penv.configure()
    penv.apply(
        [create_entry(title="Sensitive", text="RESTRICTED-CANARY", sensitivity="restricted")]
    )
    shown = penv.preview(OWNER, purpose="notebook_draft", meeting_id=None, include_restricted=True)
    penv.script(completion("not json"), completion("still not json"))
    with pytest.raises(PrometheosError) as err:
        penv.run(shown=shown, purpose="notebook_draft", meeting_id=None, include_restricted=True)
    task_id = err.value.details["task_id"]
    with pytest.raises(PrometheosError):
        penv.svc.task_get(penv.project_id, task_id, AGENT)
    agent_view = penv.svc.provider_calls(penv.project_id, AGENT)
    assert task_id not in json.dumps(agent_view) and agent_view["calls"] == []
    owner_view = penv.svc.provider_calls(penv.project_id, OWNER)
    assert len(owner_view["calls"]) == 2 and all(
        c["task_id"] == task_id for c in owner_view["calls"]
    )
    # A run over normal inputs stays visible to the agent.
    penv.script(completion({"purpose": "notebook_draft", "entry": _entry()}))
    plain = penv.run(purpose="notebook_draft", meeting_id=None)
    visible = penv.svc.provider_calls(penv.project_id, AGENT)["calls"]
    assert [c["task_id"] for c in visible] == [plain["task_id"]]


def test_an_agent_ledger_summary_has_no_call_counts(penv: ProviderEnv) -> None:
    penv.configure()
    penv.apply([create_entry(title="Hidden", text="x", sensitivity="restricted")])
    shown = penv.preview(OWNER, purpose="notebook_draft", meeting_id=None, include_restricted=True)
    penv.script(completion({"purpose": "notebook_draft", "entry": _entry()}))
    penv.run(shown=shown, purpose="notebook_draft", meeting_id=None, include_restricted=True)
    agent = penv.svc.provider_calls(penv.project_id, AGENT)
    assert "calls" not in agent["usage"] and "unknown_outcomes" not in agent["usage"]
    assert agent["usage"]["spent_estimate_usd"] != "0.000000"  # spend and cap stay visible
    status = penv.svc.provider_status(penv.project_id, AGENT)
    assert "calls" not in status["usage"] and status["usage"]["cap_usd"] == "1.00"
    seen = penv.preview(purpose="notebook_draft", meeting_id=None)["cost"]["usage_so_far"]
    assert "calls" not in seen
    owner = penv.svc.provider_calls(penv.project_id, OWNER)["usage"]
    assert owner["calls"] == 1 and owner["unknown_outcomes"] == 0


def test_the_provider_regression_tests_are_part_of_the_c06_ai_check() -> None:
    """R3-M1: the check that claims a property must itself run the test of that property."""
    suites = json.loads(
        (Path(__file__).resolve().parents[1] / "tools" / "checkpoint_suites.json").read_text(
            encoding="utf-8"
        )
    )["suites"]["CP6"]
    ai_files = {part for cmd in suites["C06-AI"]["commands"].values() for part in cmd}
    audio_files = {part for cmd in suites["C06-AUDIO"]["commands"].values() for part in cmd}
    assert "tests/test_cp6_provider_review.py" in ai_files
    assert "tests/test_cp6_review_round1.py" in audio_files
    assert "tests/test_cp6_real.py" in audio_files
