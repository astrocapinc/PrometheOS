"""Review cp22-a (decision 0096): a regression test for each required finding, adapted from the
reviewer's probes. Temp homes, a scripted transport; nothing leaves this computer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import test_cp22_tasks as T
from cp1_support import OWNER, OWNER_ACT, Env, create_entry

from prometheos import agentops
from prometheos.agentops import Ctx
from prometheos.app import intelligence as app_ai
from prometheos.app.reads import Caller
from prometheos.app.service import Service
from prometheos.domain.models import PrometheosError
from prometheos.intelligence import evaluation, results, spend
from prometheos.intelligence.run import run
from prometheos.intelligence.tasks import CLASSIFY_FILE, STATUS_STATEMENTS
from prometheos.providers import config as provider_config
from prometheos.providers import transport as tp
from prometheos.providers.secrets import MemorySecretStore
from prometheos.store.project import ProjectStore

agentops.load()
AGENT = Caller("review-agent")
PAIR = {"model": "mistralai/mistral-small-4", "provider": "mistral", "company": "mistralai",
        "supported_parameters": ["temperature"], "lowest_effort": None,
        "price_input_per_mtok_usd": "0.15", "price_output_per_mtok_usd": "0.60",
        "metadata_as_of": "2026-10-30"}  # fmt: skip


def policy(**change: Any) -> dict[str, Any]:
    base = {"tasks": [{"task_kind": "adoption.classify_file", "models": [PAIR],
                       "single_company_reason": "one pair until the spike measures more",
                       "task_ceiling_usd": "0.05"}],
            "sensitivity_ceiling": "normal", "data_policy": "zdr_deny_collection",
            "run_cap_usd": "0.50", "monthly_cap_usd": "1", "agents_may_run": True}  # fmt: skip
    return base | change


class Recorder:
    def __init__(self, answers: list[Any], on_post: Any = None) -> None:
        self.answers, self.bodies, self.on_post = list(answers), [], on_post

    def post(self, url: str, headers: dict[str, str], data: bytes, timeout: float) -> tp.Reply:
        self.bodies.append(json.loads(data))
        if self.on_post:
            self.on_post(len(self.bodies))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[no-any-return]


def setup(svc: Service, pid: str, store_dir: Path, transport: Recorder,
          doc: dict[str, Any] | None = None) -> None:  # fmt: skip
    secrets = MemorySecretStore()
    secrets.set("openrouter", "sk-or-test-not-real")
    svc.secret_store = secrets
    svc.provider_transport = transport
    store = ProjectStore(store_dir)
    conn = store.connect()
    try:
        _, revision = provider_config.load(conn)
    finally:
        conn.close()
    config = provider_config.ProviderConfig.model_validate({
        "policy": "remote-allowed", "endpoint": "https://openrouter.ai/api/v1",
        "model": PAIR["model"], "credential_ref": "openrouter",
        "price_input_per_mtok_usd": "0.15", "price_output_per_mtok_usd": "0.60",
        "budget": {"max_input_chars": 60000, "max_output_tokens": 2048, "max_calls_per_run": 2,
                   "spend_cap_usd": None, "timeout_seconds": 60}})  # fmt: skip
    provider_config.save(store, config, revision)
    doc = doc or policy()
    binding = svc.ai_policy_preview(pid, doc)["binding_sha256"]
    svc.ai_policy_grant(pid, doc, binding, authority=OWNER_ACT)


def scan(svc: Service, pid: str, alias: str) -> int:
    return int(svc.adoption_scan(pid, OWNER, root_alias=alias, mode="wait")["scan_gen"])


def level_of(env: Env, relative: str, root_level: str = "normal") -> str:
    path = env.research / relative
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        return app_ai.file_sensitivity(
            conn,
            [("research", env.research.resolve(), root_level)],
            path,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    finally:
        conn.close()


# ----- F1-F4: what may leave --------------------------------------------------------------------


def test_f3_a_file_under_a_private_nested_root_is_never_sent(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc = env.svc
    made = svc.init_project(title="Nested", objective="Probe nested roots.", timezone="UTC",
                            roots={"phd": str(env.research),
                                   "interviews": str(env.research / "sub")})  # fmt: skip
    pid, store_dir = made["project_id"], Path(made["store"])
    transport = Recorder([T.ok()])
    setup(svc, pid, store_dir, transport)
    svc.ai_root_sensitivity_set(pid, "interviews", "private")
    svc.ai_root_sensitivity_set(pid, "phd", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "phd")
    done = svc.ai_run(pid, OWNER, task_kind=CLASSIFY_FILE.kind, scan_gen=gen,
                      paths=["sub/notes.txt"])  # fmt: skip
    assert transport.bodies == [] and done["items"][0]["outcome"] == "denied"


def test_f1_a_restricted_record_citing_the_file_through_evidence_counts(tmp_path: Path) -> None:
    env = Env(tmp_path)
    imported = env.import_files(["sub/notes.txt"])
    item = imported["plan"]["items"][0]
    assert level_of(env, "sub/notes.txt") == "normal"
    env.apply([create_entry(sensitivity="restricted", evidence=[{
        "source_id": item["source_id"], "version_id": item["version_id"],
        "locator": {"type": "file"}, "quote_sha256": None}])])  # fmt: skip
    assert level_of(env, "sub/notes.txt") == "restricted"


def test_f2_an_edited_restricted_file_stays_restricted(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.import_files(["sub/notes.txt"], sensitivity="restricted")
    assert level_of(env, "sub/notes.txt") == "restricted"
    (env.research / "sub" / "notes.txt").write_text("line one\nline two\nline three\n", "utf-8")
    assert level_of(env, "sub/notes.txt") == "restricted"


def test_f4_a_file_changed_during_the_call_makes_the_reply_stale(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    target = env.research / "meeting.txt"

    def edit(n: int) -> None:
        target.write_text(target.read_text("utf-8") + "\nchanged during the call\n", "utf-8")

    transport = Recorder([T.ok()], on_post=edit)
    setup(svc, pid, env.store_dir, transport)
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    done = svc.ai_run(pid, OWNER, task_kind=CLASSIFY_FILE.kind, scan_gen=gen,
                      paths=["meeting.txt"])  # fmt: skip
    assert done["items"][0]["outcome"] == "rejected"
    assert done["items"][0]["reasons"] == ["digest_changed"]


# ----- F5, F6: authority ------------------------------------------------------------------------


def test_f5_revocation_stops_a_running_batch(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id

    def revoke(n: int) -> None:
        if n == 1:
            svc.ai_policy_revoke(pid, authority=OWNER_ACT)

    transport = Recorder([T.ok(), T.ok(), T.ok()], on_post=revoke)
    setup(svc, pid, env.store_dir, transport)
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    done = svc.ai_run(pid, OWNER, task_kind=CLASSIFY_FILE.kind, scan_gen=gen,
                      paths=["meeting.txt", "sub/notes.txt", "baseline.md"])  # fmt: skip
    assert len(transport.bodies) == 1
    assert done["stopped"]["reason"] == "policy_revoked"
    assert [i["outcome"] for i in done["items"]] == ["accepted", "not_run", "not_run"]


def _agent(svc: Service, pid: str, op: str, **params: Any) -> str:
    operation = agentops.OPS[op]
    with pytest.raises(PrometheosError) as asked:
        operation.run(Ctx(service=svc, project_id=pid, caller=AGENT),
                      operation.params.model_validate(params))  # fmt: skip
    assert asked.value.code == "approval_required", asked.value
    return str(asked.value.details["approval_request_id"])


def _grant(svc: Service, pid: str, approval_id: str) -> None:
    view = svc.approval_get(pid, Caller("o", scope="owner"), approval_id)
    svc.approval_decide(pid, approval_id, grant=True, plan_sha256=view["plan_sha256"],
                        owner=OWNER_ACT)  # fmt: skip


def test_f6_an_approved_lowering_refuses_after_the_owner_raised_it(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    aid = _agent(svc, pid, "root_sensitivity_set", root_alias="research", sensitivity="normal")
    _grant(svc, pid, aid)
    svc.ai_root_sensitivity_set(pid, "research", "private")
    with pytest.raises(PrometheosError) as stale:
        svc.approval_resume(pid, AGENT, aid)
    assert stale.value.code == "revision_conflict"
    roots = {r["root_alias"]: r["sensitivity"] for r in svc.ai_root_sensitivity_show(pid)["roots"]}
    assert roots["research"] == "private"


def test_f6_an_approved_grant_refuses_after_the_owner_revoked(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    setup(svc, pid, env.store_dir, Recorder([]))
    doc = policy(agents_may_run=False)
    binding = svc.ai_policy_preview(pid, doc)["binding_sha256"]
    aid = _agent(svc, pid, "ai_policy_grant", policy=doc, binding_sha256=binding)
    _grant(svc, pid, aid)
    svc.ai_policy_revoke(pid, authority=OWNER_ACT)
    with pytest.raises(PrometheosError) as stale:
        svc.approval_resume(pid, AGENT, aid)
    assert stale.value.code == "revision_conflict"
    assert svc.ai_policy_show(pid)["in_force"] is False


# ----- F7-F10, F14: receipts and spend ------------------------------------------------------------


def test_f7_an_interrupted_call_keeps_its_receipt_and_is_never_resent(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("1"))
    batch = T.subjects(1)
    with pytest.raises(KeyboardInterrupt):
        interrupted = Recorder([KeyboardInterrupt()])
        run(store, CLASSIFY_FILE, batch, T.gateway_executor(store, home, interrupted))  # type: ignore[arg-type]
    conn = sqlite3.connect(store.db_path)
    states = [r[0] for r in conn.execute("SELECT state FROM provider_calls").fetchall()]
    conn.close()
    assert states == ["sent"]
    again = Recorder([T.ok()])
    done = run(store, CLASSIFY_FILE, batch, T.gateway_executor(store, home, again))  # type: ignore[arg-type]
    assert again.bodies == [] and done["items"][0]["outcome"] == "held"
    assert spend.summary(home)["held_calls"] == 1


def test_f8_every_call_that_left_is_recorded(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("1"))
    transport = T.Scripted([tp.Reply(500, b"{}"), T.ok(model=T.B.model, provider="Google Vertex"),
                            tp.Reply(429, b"{}")])  # fmt: skip
    run(store, CLASSIFY_FILE, T.subjects(2), T.gateway_executor(store, home, transport))
    conn = sqlite3.connect(store.db_path)
    calls = conn.execute("SELECT model, state, bundle_digest FROM provider_calls "
                         "ORDER BY created_at").fetchall()  # fmt: skip
    conn.close()
    assert transport.sent == 3 and [c[1] for c in calls] == ["failed", "succeeded", "failed"]
    assert all(c[2] for c in calls)


def test_f9_spend_never_passes_the_monthly_cap(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("0.002"))
    transport = T.Scripted([T.ok(cost=0.0001) for _ in range(40)])
    done = run(
        store, CLASSIFY_FILE, T.subjects(40), T.gateway_executor(store, home, transport, "1")
    )
    assert done["stopped"]["reason"] == "budget_exceeded"
    assert Decimal(spend.summary(home)["used"]) <= Decimal("0.002")
    # A reply billed above its upper estimate is kept and the batch stops (the bound was wrong).
    other = tmp_path / "other"
    spend.set_monthly_cap(other, Decimal("1"))
    (tmp_path / "second").mkdir()
    store = ProjectStore(Env(tmp_path / "second").store_dir)  # no cached samples
    dear = T.Scripted([T.ok(cost=0.05), T.ok()])
    stopped = run(store, CLASSIFY_FILE, T.subjects(2), T.gateway_executor(store, other, dear, "1"))
    assert stopped["stopped"]["reason"] == "over_budget" and dear.sent == 1


def test_f10_a_batch_reads_the_keys_remaining_limit_first(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id

    class KeyEmpty(Recorder):
        def get(self, url: str, headers: dict[str, str], timeout: float) -> tp.Reply:
            return tp.Reply(200, json.dumps({"data": {"limit": 5, "limit_remaining": 0}}).encode())

    transport = KeyEmpty([T.ok()])
    setup(svc, pid, env.store_dir, transport)
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    with pytest.raises(PrometheosError) as empty:
        svc.ai_run(pid, OWNER, task_kind=CLASSIFY_FILE.kind, scan_gen=gen, paths=["meeting.txt"])
    assert empty.value.code == "budget_exceeded" and empty.value.details["cap"] == "key"
    assert transport.bodies == []


def test_f14_the_dry_run_bound_counts_every_sample_and_pair(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    other = {**PAIR, "model": "google/gemini-3.1-flash-lite", "provider": "google-vertex",
             "company": "google", "price_output_per_mtok_usd": "1.50"}  # fmt: skip
    doc = policy(tasks=[{"task_kind": "adoption.extract_status_statements", "models": [PAIR, other],
                         "single_company_reason": None, "task_ceiling_usd": "0.05"}])  # fmt: skip
    setup(svc, pid, env.store_dir, Recorder([]), doc)
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    dry = svc.ai_run(pid, OWNER, task_kind="adoption.extract_status_statements", scan_gen=gen,
                     paths=["meeting.txt"], dry_run=True)  # fmt: skip
    assert dry["calls_at_most"] == 4  # two samples, each possibly through both pairs
    one_pair = svc.ai_run(
        pid,
        OWNER,
        task_kind="adoption.extract_status_statements",
        scan_gen=gen,
        paths=["meeting.txt"],
        dry_run=True,
    )["estimate_upper_usd"]
    assert Decimal(one_pair) > 0


# ----- F11-F13: trust -----------------------------------------------------------------------------


def golden(n: int = 3) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        b = T.classify_bundle(f"% script number {chr(97 + i)}")
        out.append({"item_id": f"g{i}", "subject": b.subject, "subject_sha256": b.subject_sha256,
                    "input": b.input, "label": "source_code"})  # fmt: skip
    return out


def record(items: list[dict[str, Any]], **change: Any) -> evaluation.EvaluationRecord:
    base = {"task_kind": CLASSIFY_FILE.kind, "task_version": CLASSIFY_FILE.version,
            "outcome_field": "role", "scoring": "exact_value", "purpose": "acceptance",
            "golden": items,
            "targets": {"precision_lower_bound": 0.3, "coverage": 0.5}, "confidence": 0.95,
            "method": "clopper_pearson_one_sided", "min_items": 1}  # fmt: skip
    return evaluation.EvaluationRecord.model_validate(base | change)


def test_f12_trust_does_not_depend_on_how_many_results_the_store_holds(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    for i in range(201):
        results.save(store, key=f"k{i}", task=CLASSIFY_FILE, model="x/y", params_sha256="p",
                     input_sha256=f"i{i}", sample=0, subject="s", executor="cassette",
                     outcome="rejected", reasons=("shape",), output=None, hint_only=True,
                     served_model=None, served_provider=None, call_id=None, run_id="r",
                     sensitivity="normal")  # fmt: skip
    digest = evaluation.fix(store, record(golden()), by="test")
    transport = T.Scripted([T.ok() for _ in range(3)])
    measured = evaluation.measure(store, CLASSIFY_FILE, digest,
                                  T.gateway_executor(store, home, transport))  # fmt: skip
    assert measured["models"] == [T.A.model] and measured["trusted"] is True


def test_f11_replies_from_outside_the_list_confer_no_trust(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    digest = evaluation.fix(store, record(golden()), by="test")
    transport = T.Scripted([T.ok(model="mistralai/mistral-large-3") for _ in range(3)])
    measured = evaluation.measure(store, CLASSIFY_FILE, digest,
                                  T.gateway_executor(store, home, transport))  # fmt: skip
    assert measured["replies_outside_the_list"] == 3 and measured["trusted"] is False
    assert not evaluation.trusted(store, CLASSIFY_FILE, T.A.model, measured["served_providers"])


def test_f13_the_golden_set_cannot_be_swapped_only_grown(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    first = golden(2)
    evaluation.fix(store, record(first), by="test")
    swapped = [{**first[0], "label": "notes"}, first[1]]
    with pytest.raises(PrometheosError, match="only grow"):
        evaluation.fix(store, record(swapped), by="test")
    with pytest.raises(PrometheosError, match="only grow"):
        evaluation.fix(store, record(first, outcome_field="quote"), by="test")
    evaluation.fix(store, record(golden(3)), by="test")  # a superset of the earlier items


# ----- decision 0097: the per-call route outside the policy (closes 0094's interim) ---------


def test_a_run_outside_the_policy_is_one_approved_run_whose_outputs_are_hints(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    transport = Recorder([T.ok()])
    setup(svc, pid, env.store_dir, transport, policy(agents_may_run=False))
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    aid = _agent(svc, pid, "ai_run", task_kind=CLASSIFY_FILE.kind, scan_gen=gen,
                 paths=["meeting.txt"])  # fmt: skip
    assert transport.bodies == []  # nothing is sent before the owner decides
    view = svc.approval_get(pid, Caller("o", scope="owner"), aid)
    assert view["plan"]["calls_at_most"] == 1 and view["plan"]["model"] == PAIR["model"]
    _grant(svc, pid, aid)
    done = svc.approval_resume(pid, AGENT, aid)
    result = done.get("result", done)
    [item] = result["items"]
    assert item["outcome"] == "accepted" and item["hint_only"] is True
    assert len(transport.bodies) == 1
    assert "only" not in transport.bodies[0]["provider"]  # no pin, but the data policy holds
    assert transport.bodies[0]["provider"]["zdr"] is True
    assert transport.bodies[0]["provider"]["data_collection"] == "deny"
    again = svc.approval_resume(pid, AGENT, aid)  # a used request answers its recorded result
    assert len(transport.bodies) == 1 and again.get("result", again)["run_id"] == result["run_id"]


def test_a_per_call_run_refuses_when_the_file_changed_after_approval(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    transport = Recorder([T.ok()])
    setup(svc, pid, env.store_dir, transport, policy(agents_may_run=False))
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    aid = _agent(svc, pid, "ai_run", task_kind=CLASSIFY_FILE.kind, scan_gen=gen,
                 paths=["meeting.txt"])  # fmt: skip
    _grant(svc, pid, aid)
    target = env.research / "meeting.txt"
    target.write_text(target.read_text("utf-8") + "\nedited\n", "utf-8")
    with pytest.raises(PrometheosError):
        svc.approval_resume(pid, AGENT, aid)
    assert transport.bodies == []


# ----- review cp22-a round 2 ---------------------------------------------------------------


def test_r2_f1_the_real_transport_reads_the_keys_limit(tmp_path: Path) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from prometheos.intelligence.gateway import Gateway
    from prometheos.providers.transport import UrllibTransport

    class Key(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"data": {"limit": 5, "limit_remaining": 1.25}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Key)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config = provider_config.ProviderConfig.model_validate({
            "policy": "local-only", "endpoint": f"http://127.0.0.1:{server.server_port}/api/v1",
            "model": PAIR["model"], "credential_ref": "walk-key",
            "price_input_per_mtok_usd": "0.15", "price_output_per_mtok_usd": "0.60",
            "budget": {"max_input_chars": 60000, "max_output_tokens": 2048,
                       "max_calls_per_run": 2, "spend_cap_usd": None,
                       "timeout_seconds": 60}})  # fmt: skip
        secrets = MemorySecretStore()
        secrets.set("walk-key", "sk-or-test-not-real")
        gateway = Gateway(config, secrets, UrllibTransport(), tmp_path / "home", "p")
        assert gateway.key_limit() == {"limit": 5, "limit_remaining": 1.25}
    finally:
        server.shutdown()
        server.server_close()


def _accept_ready(tmp_path: Path) -> tuple[Env, int, Any]:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    transport = Recorder([T.ok(), T.ok(role="notes")])
    setup(svc, pid, env.store_dir, transport, policy(agents_may_run=True))
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    svc.ai_run(pid, OWNER, task_kind=CLASSIFY_FILE.kind, scan_gen=gen, paths=["meeting.txt"])
    return env, gen, transport


def test_r2_f2_an_accept_runs_exactly_the_samples_approved_and_only_once(tmp_path: Path) -> None:
    env, gen, _ = _accept_ready(tmp_path)
    svc, pid = env.svc, env.project_id
    aid = _agent(svc, pid, "ai_accept", task_kind=CLASSIFY_FILE.kind, scan_gen=gen,
                 subject="meeting.txt")  # fmt: skip
    approved = svc.approval_get(pid, Caller("o", scope="owner"), aid)["plan"]["samples"]
    _grant(svc, pid, aid)
    with pytest.raises(PrometheosError) as changed:
        svc.ai_accept(pid, CLASSIFY_FILE.kind, gen, "meeting.txt", authority=OWNER_ACT,
                      result_keys=["0" * 64])  # fmt: skip
    assert changed.value.code == "revision_conflict"
    done = svc.approval_resume(pid, AGENT, aid)
    assert done.get("result", done)["accepted_samples"] == len(approved) == 1
    with pytest.raises(PrometheosError, match="not yet used"):
        svc.ai_accept(pid, CLASSIFY_FILE.kind, gen, "meeting.txt", authority=OWNER_ACT)


def test_r2_f3_a_file_with_a_bang_in_its_name_is_read_back_unchanged(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    (env.research / "final!.md").write_text("# Final!\nThe last draft of the note.\n", "utf-8")
    transport = Recorder([T.ok(role="notes")])
    setup(svc, pid, env.store_dir, transport)
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    done = svc.ai_run(pid, OWNER, task_kind=CLASSIFY_FILE.kind, scan_gen=gen,
                      paths=["final!!.md"])  # fmt: skip
    assert done["items"][0]["outcome"] == "accepted", done["items"]


def test_the_replay_transport_needs_the_explicit_test_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.intelligence.replay import ReplayTransport
    from prometheos.providers.transport import UrllibTransport

    svc = Service(home=tmp_path / "home", actor="test")
    monkeypatch.setenv("PROMETHEOS_TEST_REPLAY", str(tmp_path))
    monkeypatch.delenv("PROMETHEOS_TEST_HOOKS", raising=False)
    assert isinstance(svc._transport(), UrllibTransport)
    monkeypatch.setenv("PROMETHEOS_TEST_HOOKS", "1")
    replay = svc._transport()
    assert isinstance(replay, ReplayTransport)
    request = {"messages": [{"role": "user", "content": 'DATA:\n{"a": 1}'}]}
    with pytest.raises(tp.NotSent):  # nothing recorded for this bundle: never sent anywhere
        replay.post("https://openrouter.ai/api/v1/chat/completions", {},
                    json.dumps(request).encode(), 5)  # fmt: skip


# ----- review cp22-a round 3 ----------------------------------------------------------------


def configured_without_policy(env: Env, transport: Recorder, calls: int = 2) -> None:
    """The provider configured with a CP6.3 budget, and no policy ever granted on this home."""
    secrets = MemorySecretStore()
    secrets.set("openrouter", "sk-or-test-not-real")
    env.svc.secret_store = secrets
    env.svc.provider_transport = transport
    store = ProjectStore(env.store_dir)
    conn = store.connect()
    try:
        _, revision = provider_config.load(conn)
    finally:
        conn.close()
    config = provider_config.ProviderConfig.model_validate({
        "policy": "remote-allowed", "endpoint": "https://openrouter.ai/api/v1",
        "model": PAIR["model"], "credential_ref": "openrouter",
        "price_input_per_mtok_usd": "0.15", "price_output_per_mtok_usd": "0.60",
        "budget": {"max_input_chars": 60000, "max_output_tokens": 150,
                   "max_calls_per_run": calls, "spend_cap_usd": None,
                   "timeout_seconds": 60}})  # fmt: skip
    provider_config.save(store, config, revision)


def test_r3_f1_a_per_call_run_works_on_a_home_that_never_had_a_policy(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    transport = Recorder([T.ok()])
    configured_without_policy(env, transport)
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    aid = _agent(svc, pid, "ai_run", task_kind=CLASSIFY_FILE.kind, scan_gen=gen,
                 paths=["meeting.txt"])  # fmt: skip
    _grant(svc, pid, aid)
    done = svc.approval_resume(pid, AGENT, aid)
    result = done.get("result", done)
    assert [i["outcome"] for i in result["items"]] == ["accepted"]
    assert len(transport.bodies) == 1
    assert transport.bodies[0]["max_tokens"] == 150  # the provider budget's output limit


def test_r3_f2_a_per_call_run_honours_the_provider_budget(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    transport = Recorder([])
    configured_without_policy(env, transport, calls=1)
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    with pytest.raises(PrometheosError) as over:
        svc.ai_run(pid, AGENT, task_kind=CLASSIFY_FILE.kind, scan_gen=gen,
                   paths=["meeting.txt", "baseline.md"])  # fmt: skip
    assert over.value.code == "budget_exceeded"
    assert over.value.details["cap"] == "calls_per_run" and transport.bodies == []


def test_r3_f3_the_owner_accepts_the_row_chosen(tmp_path: Path) -> None:
    env, gen, _ = _accept_ready(tmp_path)
    svc, pid = env.svc, env.project_id
    # A second sample for the same file, from a recorded reply that says something else.
    from prometheos.adoption import assist
    from prometheos.intelligence import results as stored_results

    conn = ProjectStore(env.store_dir).connect()
    try:
        bundle = assist._bundle(conn, env.research, gen, "classify_file", "meeting.txt")
    finally:
        conn.close()
    other = stored_results.save(
        ProjectStore(env.store_dir), key="f" * 64, task=CLASSIFY_FILE, model="cassette/recorded",
        params_sha256="p", input_sha256=bundle.input_sha256, sample=0, subject="meeting.txt",
        executor="cassette", outcome="accepted", reasons=(),
        output={"result": {"role": "results", "quote": None}, "abstain": None}, hint_only=True,
        served_model=None, served_provider=None, call_id=None, run_id="r", sensitivity="normal",
        scan_gen=gen,
    )  # fmt: skip
    done = svc.ai_accept(pid, CLASSIFY_FILE.kind, gen, "meeting.txt", authority=OWNER_ACT,
                         result_keys=[other.result_key])  # fmt: skip
    assert done["accepted_samples"] == 1
    conn = ProjectStore(env.store_dir).connect()
    try:
        [response] = [json.loads(r[0]) for r in conn.execute(
            "SELECT response FROM adoption_assist").fetchall()]  # fmt: skip
    finally:
        conn.close()
    assert response["output"]["role"] == "results"  # the chosen row's answer, not the first


def test_c1_replayed_replies_never_confer_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    monkeypatch.setenv("PROMETHEOS_TEST_HOOKS", "1")
    monkeypatch.setenv("PROMETHEOS_TEST_REPLAY", str(tmp_path))
    digest = evaluation.fix(store, record(golden(), purpose="development"), by="test")
    transport = T.Scripted([T.ok() for _ in range(3)])
    measured = evaluation.measure(store, CLASSIFY_FILE, digest,
                                  T.gateway_executor(store, home, transport))  # fmt: skip
    assert measured["meets_targets"] and measured["trusted"] is False


# ----- review cp22-a round 4 ----------------------------------------------------------------

GEMINI = {**PAIR, "model": "google/gemini-3.1-flash-lite", "provider": "google-vertex",
          "company": "google", "price_output_per_mtok_usd": "1.50"}  # fmt: skip
NO_STATEMENTS = {"result": {"statements": []}, "abstain": None}


def status_reply(model: str) -> tp.Reply:
    return tp.Reply(200, json.dumps(T.body(NO_STATEMENTS, model=model)).encode())


def status_grant(models: list[dict[str, Any]]) -> dict[str, Any]:
    return policy(tasks=[{"task_kind": STATUS_STATEMENTS.kind, "models": models,
                          "single_company_reason": None if len(models) > 1
                          else "one pair for the regression test",
                          "task_ceiling_usd": "0.05"}])  # fmt: skip


def test_r4_f2_a_two_sample_task_needs_two_models_in_the_policy(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    setup(svc, pid, env.store_dir, Recorder([]))
    with pytest.raises(PrometheosError) as one:
        svc.ai_policy_preview(pid, status_grant([PAIR]))
    assert one.value.code == "validation_error" and one.value.details["needed"] == 2


def test_r4_f2_the_two_samples_come_from_two_models_and_become_evidence(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    transport = Recorder([status_reply(PAIR["model"]), status_reply(GEMINI["model"])])
    setup(svc, pid, env.store_dir, transport, status_grant([PAIR, GEMINI]))
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    done = svc.ai_run(pid, OWNER, task_kind=STATUS_STATEMENTS.kind, scan_gen=gen,
                      paths=["meeting.txt"])  # fmt: skip
    [item] = done["items"]
    assert item["outcome"] == "accepted", item
    assert [b["model"] for b in transport.bodies] == [PAIR["model"], GEMINI["model"]]
    accepted = svc.ai_accept(pid, STATUS_STATEMENTS.kind, gen, "meeting.txt",
                             authority=OWNER_ACT, result_keys=item["samples"])  # fmt: skip
    assert accepted["accepted_samples"] == 2 and accepted["assist_outcome"] == "accepted"


def test_r4_f2_one_models_samples_are_refused_before_anything_is_consumed(tmp_path: Path) -> None:
    from prometheos.adoption import assist
    from prometheos.intelligence import results as stored_results

    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    gen = scan(svc, pid, "research")
    conn = ProjectStore(env.store_dir).connect()
    try:
        bundle = assist._bundle(conn, env.research, gen, "extract_status_statements",
                                "meeting.txt")  # fmt: skip
    finally:
        conn.close()

    def stored(key: str, sample: int, model: str) -> str:
        return stored_results.save(
            ProjectStore(env.store_dir), key=key * 64, task=STATUS_STATEMENTS, model=model,
            params_sha256="p", input_sha256=bundle.input_sha256, sample=sample,
            subject="meeting.txt", executor="gateway", outcome="accepted", reasons=(),
            output=NO_STATEMENTS, hint_only=True, served_model=model, served_provider="x",
            call_id=None, run_id="r", sensitivity="normal", scan_gen=gen,
        ).result_key  # fmt: skip

    a0, a1 = stored("a", 0, "m/one"), stored("b", 1, "m/one")
    stored("c", 1, "m/two")  # the other model's sample completes the set
    for keys in ([a0, a1], [a0, a0]):
        with pytest.raises(PrometheosError) as refused:
            svc.ai_accept(pid, STATUS_STATEMENTS.kind, gen, "meeting.txt", authority=OWNER_ACT,
                          result_keys=keys)  # fmt: skip
        assert refused.value.code in {"validation_error", "revision_conflict"}, keys
    # nothing was consumed: one clicked key stands for its set (R5-F2), the two-model one
    keys = svc.ai_accept_keys(pid, STATUS_STATEMENTS.kind, gen, "meeting.txt", [a0])
    done = svc.ai_accept(pid, STATUS_STATEMENTS.kind, gen, "meeting.txt", authority=OWNER_ACT,
                         result_keys=keys)  # fmt: skip
    assert done["assist_outcome"] == "accepted" and done["accepted_samples"] == 2


def test_r4_f2_a_per_call_run_cannot_do_a_two_model_task(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    transport = Recorder([])
    configured_without_policy(env, transport)
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    with pytest.raises(PrometheosError) as one:
        svc.ai_run(pid, AGENT, task_kind=STATUS_STATEMENTS.kind, scan_gen=gen,
                   paths=["meeting.txt"])  # fmt: skip
    assert one.value.code == "capability_unavailable"
    assert one.value.details["blocker"] == "not_configured" and transport.bodies == []
    assert "2 different models" in one.value.message
    assert "coding agent" in one.value.message


def test_r4_replay_answers_per_model_and_its_replies_are_recordings(tmp_path: Path) -> None:
    from prometheos.domain.canonical import canonical_sha256
    from prometheos.intelligence.replay import ReplayTransport

    data = {"a": 1}
    for model, words in ((PAIR["model"], "first"), (GEMINI["model"], "second")):
        (tmp_path / f"{words}.json").write_text(json.dumps({
            "input_sha256": canonical_sha256(data), "model": model,
            "body": T.body(words, model=model)}), encoding="utf-8")  # fmt: skip
    replay = ReplayTransport(tmp_path)

    def asked(model: str) -> str:
        request = {
            "model": model,
            "messages": [{"role": "user", "content": "DATA:\n" + json.dumps(data)}],
        }
        reply = replay.post("https://openrouter.ai/api/v1/chat/completions", {},
                            json.dumps(request).encode(), 5)  # fmt: skip
        return str(json.loads(reply.body)["choices"][0]["message"]["content"])

    assert (asked(PAIR["model"]), asked(GEMINI["model"])) == ("first", "second")
    with pytest.raises(tp.NotSent):  # neither recording is for this model
        asked("other/model")
    # Through the gateway, a replayed reply is stored as a recording, never the gateway's (D3).
    (tmp_path / "env").mkdir()
    store = ProjectStore(Env(tmp_path / "env").store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("1"))
    executor = T.gateway_executor(store, home, T.Scripted([]))
    executor.gateway.transport = replay
    bundle = T.classify_bundle()
    (tmp_path / "classify.json").write_text(json.dumps({
        "input_sha256": bundle.input_sha256,
        "body": T.body({"result": {"role": "source_code", "quote": None}, "abstain": None})}),
        encoding="utf-8")  # fmt: skip
    from prometheos.intelligence.run import run as run_task

    done = run_task(store, CLASSIFY_FILE, [T.subject(bundle)], executor)
    assert done["items"][0]["provenance"][0]["executor"] == "cassette", done["items"]


# ----- review cp22-a round 5 ----------------------------------------------------------------


def test_r5_f1_a_two_sample_task_is_trusted_as_its_set_of_two_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    from prometheos.intelligence import tasks as registered

    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    task = dataclasses.replace(CLASSIFY_FILE, kind="probe.classify_twice", agreement=2)
    monkeypatch.setitem(registered.REGISTRY, task.kind, task)  # trusted only when registered
    digest = evaluation.fix(store, record(golden(), task_kind=task.kind), by="test")
    answers = []
    for _ in range(3):
        answers += [T.ok(), T.ok(model=T.B.model, provider="Google Vertex")]
    measured = evaluation.measure(
        store, task, digest, T.gateway_executor(store, home, T.Scripted(answers))
    )
    assert measured["meets_targets"] and measured["trusted"] is True, measured
    pair = sorted([T.A.model, T.B.model])
    assert measured["trust_unit"] == "+".join(pair)
    providers = measured["served_providers"]
    assert evaluation.trusted(store, task, pair, providers) is True
    assert evaluation.trusted(store, task, T.A.model, providers) is False  # one model is not it


def test_r5_f2_the_listing_is_newest_first_and_filters_by_scan(tmp_path: Path) -> None:
    from prometheos.intelligence import results as stored_results

    env = Env(tmp_path)
    store = ProjectStore(env.store_dir)
    for i in range(25):
        stored_results.save(
            store, key=f"{i:064x}", task=CLASSIFY_FILE, model="m/x", params_sha256="p",
            input_sha256=f"in{i}", sample=0, subject=f"file{i:02d}.txt", executor="gateway",
            outcome="accepted", reasons=(), output={"result": {"role": "notes", "quote": None},
                                                    "abstain": None},
            hint_only=True, served_model="m/x", served_provider="x", call_id=None,
            run_id=f"r{i}", sensitivity="normal", scan_gen=1 if i < 20 else 2,
        )  # fmt: skip
    shown = [r["subject"] for r in env.svc.ai_results(env.project_id, None, 20)["results"]]
    assert shown[0] == "file24.txt" and "file00.txt" not in shown
    later = env.svc.ai_results(env.project_id, None, 200, 2)["results"]
    assert sorted(r["subject"] for r in later) == [f"file{i}.txt" for i in range(20, 25)]


def test_r5_f2_one_clicked_sample_is_accepted_with_its_set(tmp_path: Path) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    transport = Recorder([status_reply(PAIR["model"]), status_reply(GEMINI["model"])])
    setup(svc, pid, env.store_dir, transport, status_grant([PAIR, GEMINI]))
    svc.ai_root_sensitivity_set(pid, "research", "normal", authority=OWNER_ACT)
    gen = scan(svc, pid, "research")
    [item] = svc.ai_run(pid, OWNER, task_kind=STATUS_STATEMENTS.kind, scan_gen=gen,
                        paths=["meeting.txt"])["items"]  # fmt: skip
    # The hub's button: the paired owner sends the clicked row's key through the operation.
    hub = Ctx(service=svc, project_id=pid, caller=Caller("hub-owner", scope="owner"), owner=True,
              transport="http", authority=OWNER_ACT)  # fmt: skip
    operation = agentops.OPS["ai_accept"]
    done = operation.run(hub, operation.params.model_validate({
        "task_kind": STATUS_STATEMENTS.kind, "scan_gen": gen, "subject": "meeting.txt",
        "result_keys": item["samples"][:1]}))  # fmt: skip
    assert done["accepted_samples"] == 2 and done["assist_outcome"] == "accepted"


# ----- review cp22-a round 6 ----------------------------------------------------------------


def test_r6_f1_an_agents_one_key_request_is_approved_as_its_full_set(tmp_path: Path) -> None:
    from prometheos.adoption import assist
    from prometheos.intelligence import results as stored_results

    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    gen = scan(svc, pid, "research")
    store = ProjectStore(env.store_dir)
    conn = store.connect()
    try:
        bundle = assist._bundle(conn, env.research, gen, "extract_status_statements",
                                "meeting.txt")  # fmt: skip
    finally:
        conn.close()

    def stored(key: str, model: str, sample: int, run_id: str) -> None:
        stored_results.save(
            store, key=key, task=STATUS_STATEMENTS, model=model, params_sha256="p",
            input_sha256=bundle.input_sha256, sample=sample, subject="meeting.txt",
            executor="gateway", outcome="accepted", reasons=(), output=NO_STATEMENTS,
            hint_only=True, served_model=model, served_provider="x", call_id=None,
            run_id=run_id, sensitivity="normal", scan_gen=gen,
        )  # fmt: skip

    a0, b1, c1 = "a" * 64, "b" * 64, "c" * 64
    stored(a0, "m/model-a", 0, "run-1")
    stored(b1, "m/model-b", 1, "run-1")
    aid = _agent(svc, pid, "ai_accept", task_kind=STATUS_STATEMENTS.kind, scan_gen=gen,
                 subject="meeting.txt", result_keys=[a0])  # fmt: skip
    plan = svc.approval_get(pid, Caller("o", scope="owner"), aid)["plan"]
    assert [s["result_key"] for s in plan["samples"]] == [a0, b1]  # the owner sees both
    stored(c1, "m/model-c", 1, "run-2")  # stored after the request: never submitted
    _grant(svc, pid, aid)
    done = svc.approval_resume(pid, AGENT, aid)
    assert done.get("result", done)["accepted_samples"] == 2
    conn = store.connect()
    try:
        submitted = {str(r[0])[:24] for r in conn.execute(
            "SELECT json_extract(response, '$.submitted_by') FROM adoption_assist")}  # fmt: skip
    finally:
        conn.close()
    assert submitted == {a0[:24], b1[:24]}


# ----- decision 0104: what the live walk found the hub did not say ------------------------------


def test_0104_each_sample_says_what_it_proposes() -> None:
    from prometheos.intelligence.results import summary

    assert summary("accepted", {"result": {"role": "data_raw", "quote": None},
                                "abstain": None}) == "role: data raw"  # fmt: skip
    statements = {"result": {"statements": [
        {"referent": "data/v1.csv", "status": "superseded"},
        {"referent": None, "status": "final_claimed"}]}, "abstain": None}  # fmt: skip
    assert summary("accepted", statements) == "data/v1.csv superseded; unnamed final claimed"
    assert summary("accepted", {"result": {"statements": []}, "abstain": None}) == (
        "no status statements"
    )
    assert summary("abstained", {"result": None, "abstain": {"reason": "not_enough_evidence"}}) == (
        "abstained: not enough evidence"
    )
    assert summary("rejected", None) is None


def test_0104_a_measured_reply_is_located_in_the_scan_that_holds_its_file(tmp_path: Path) -> None:
    """Located only where a file at its path has its exact bytes (review cp22-a R8-F2)."""
    import hashlib

    from prometheos.intelligence import results as stored_results

    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    gen = scan(svc, pid, "research")
    store = ProjectStore(env.store_dir)
    digest = hashlib.sha256((env.research / "meeting.txt").read_bytes()).hexdigest()

    def stored(key: str, subject: str, subject_sha256: str | None) -> None:
        stored_results.save(
            store, key=key * 64, task=CLASSIFY_FILE, model="m/x", params_sha256="p",
            input_sha256="i" + key, sample=0, subject=subject, executor="gateway",
            outcome="accepted", reasons=(), output={"result": {"role": "notes", "quote": None},
                                                    "abstain": None},
            hint_only=False, served_model="m/x", served_provider="x", call_id=None,
            run_id="aieval_1", sensitivity="normal", scan_gen=None,  # a measurement's sample
            subject_sha256=subject_sha256,
        )  # fmt: skip

    stored("a", "meeting.txt", digest)  # these bytes: located
    stored("b", "meeting.txt", "0" * 64)  # same path, other bytes (another root, or edited)
    stored("c", "meeting.txt", None)  # no digest: never located
    rows = {r["result_key"][0]: r for r in svc.ai_results(pid, None, 50)["results"]}
    assert rows["a"]["scan_gen"] == gen
    assert rows["b"]["scan_gen"] is None and rows["c"]["scan_gen"] is None
    in_scan = svc.ai_results(pid, None, 50, gen)["results"]
    assert [r["result_key"][0] for r in in_scan] == ["a"]
    assert in_scan[0]["summary"] == "role: notes" and in_scan[0]["claim"] == '"notes"'


def test_0104_a_measurement_says_why_it_confers_no_trust() -> None:
    rec = record(golden(3))
    base = {"trusted": False, "bound_to_gateway_receipts": True, "replies_outside_the_list": 0,
            "answered": 3, "correct": 3, "coverage": 1.0, "precision_lower_bound": 0.37,
            "per_outcome": {"source_code": {"correct": 1, "given": 1, "lower_bound": 0.05}},
            "trust_unit": "m/x"}  # fmt: skip
    why = evaluation.why_not(
        base,
        rec.model_copy(
            update={"targets": evaluation.Targets(precision_lower_bound=0.8, coverage=0.8)}
        ),
    )
    assert why is not None
    assert "precision is at least 37%" in why and "source code (1/1)" in why
    assert "coverage" not in why
    assert "gateway's own receipts" in (
        evaluation.why_not(base | {"bound_to_gateway_receipts": False}, None) or ""
    )
    assert evaluation.why_not(base | {"trusted": True}, None) is None


def test_0104_one_row_per_task_version_and_model_with_registered_tasks_only(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    store = ProjectStore(env.store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    # a development record may be measured again (an acceptance record needs fresh items, R9-F1)
    digest = evaluation.fix(store, record(golden(), purpose="development"), by="test")
    for _ in range(2):  # measured twice: one row
        evaluation.measure(
            store,
            CLASSIFY_FILE,
            digest,
            T.gateway_executor(store, home, T.Scripted([T.ok() for _ in range(3)])),
        )
    rows = env.svc.ai_evaluations(env.project_id)["evaluations"]
    assert len(rows) == 1 and rows[0]["answered"] == 3
    assert rows[0]["trusted"] is False and "development record" in (rows[0]["why"] or "")


def test_0104_current_answers_are_the_policys_models_at_the_current_version(
    tmp_path: Path,
) -> None:
    from prometheos.intelligence import results as stored_results

    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    setup(svc, pid, env.store_dir, Recorder([]), policy())  # grants PAIR for classify_file
    gen = scan(svc, pid, "research")
    store = ProjectStore(env.store_dir)

    def stored(key: str, model: str, task: Any) -> None:
        stored_results.save(
            store, key=key * 64, task=task, model=model, params_sha256="p", input_sha256="i",
            sample=0, subject="meeting.txt", executor="gateway", outcome="accepted", reasons=(),
            output={"result": {"role": "notes", "quote": None}, "abstain": None},
            hint_only=False, served_model=model, served_provider="x", call_id=None,
            run_id="r", sensitivity="normal", scan_gen=gen,
        )  # fmt: skip

    import dataclasses

    stored("a", PAIR["model"], CLASSIFY_FILE)  # the policy's model, current version
    stored("b", "other/model", CLASSIFY_FILE)  # a measured model outside the policy
    stored("c", PAIR["model"], dataclasses.replace(CLASSIFY_FILE, version=1))  # an older version
    stored("d", PAIR["model"], CLASSIFY_FILE)  # newer sample of the same file, model, index
    rows = svc.ai_results(pid, None, 50, gen, True)["results"]
    assert [r["result_key"] for r in rows] == ["d" * 64]


# ----- review cp22-a round 8 ----------------------------------------------------------------


def test_r8_f1_the_record_binds_its_scoring(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    exact = record(golden())
    as_set = record(golden(), scoring="statement_set")
    assert evaluation.record_sha256(exact) != evaluation.record_sha256(as_set)  # in the digest
    digest = evaluation.fix(store, as_set, by="test")
    with pytest.raises(PrometheosError, match="scores statement_set"):
        evaluation.measure(
            store,
            CLASSIFY_FILE,
            digest,
            T.gateway_executor(store, home, T.Scripted([T.ok() for _ in range(3)])),
        )


def test_r8_f3_a_development_record_measures_but_never_confers_trust(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    digest = evaluation.fix(store, record(golden(), purpose="development"), by="test")
    measured = evaluation.measure(
        store,
        CLASSIFY_FILE,
        digest,
        T.gateway_executor(store, home, T.Scripted([T.ok() for _ in range(3)])),
    )
    assert measured["meets_targets"] and measured["trusted"] is False
    assert "development record" in (evaluation.why_not(measured, None) or "")


def test_h4_an_unregistered_task_is_never_trusted(tmp_path: Path) -> None:
    import dataclasses

    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    spike = dataclasses.replace(CLASSIFY_FILE, kind="spike.classify_once")
    digest = evaluation.fix(store, record(golden(), task_kind=spike.kind), by="test")
    measured = evaluation.measure(
        store,
        spike,
        digest,
        T.gateway_executor(store, home, T.Scripted([T.ok() for _ in range(3)])),
    )
    assert measured["meets_targets"] and measured["trusted"] is False
    assert "not a registered task" in (evaluation.why_not(measured, None) or "")


def test_h5_a_claim_includes_its_line_and_date() -> None:
    from prometheos.intelligence.results import claim, summary

    one = {"result": {"statements": [{"quote": "q" * 12, "line": 3, "referent": "a.csv",
                                      "status": "current", "date": None}]}}  # fmt: skip
    two = {"result": {"statements": [{**one["result"]["statements"][0], "line": 4}]}}
    assert summary("accepted", one) == summary("accepted", two)  # the same words…
    assert claim(one) != claim(two)  # …but not the same claim


# ----- review cp22-a round 9 ----------------------------------------------------------------


def test_r9_f1_tuned_items_never_become_acceptance_items(tmp_path: Path) -> None:
    import dataclasses

    store = ProjectStore(Env(tmp_path).store_dir)
    items = golden()
    evaluation.fix(store, record(items, purpose="development"), by="test")
    with pytest.raises(PrometheosError, match="already seen"):
        evaluation.fix(store, record(items), by="test")  # the same items as acceptance
    with pytest.raises(PrometheosError, match="already fixed"):  # same version: scoring stays
        evaluation.fix(store, record(items, purpose="development", scoring="statement_set"),
                       by="test")  # fmt: skip
    newer = dataclasses.replace(CLASSIFY_FILE, version=CLASSIFY_FILE.version + 1)
    with pytest.raises(PrometheosError, match="already seen"):
        evaluation.fix(store, record(items, task_version=newer.version), by="test")


def test_r9_f1_an_acceptance_measurement_never_rests_on_stored_replies(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    digest = evaluation.fix(store, record(golden()), by="test")
    first = evaluation.measure(
        store,
        CLASSIFY_FILE,
        digest,
        T.gateway_executor(store, home, T.Scripted([T.ok() for _ in range(3)])),
    )
    assert first["trusted"] is True
    transport = T.Scripted([T.ok() for _ in range(3)])
    with pytest.raises(PrometheosError, match="have not answered before"):
        evaluation.measure(store, CLASSIFY_FILE, digest, T.gateway_executor(store, home, transport))
    assert transport.sent == 0  # refused before anything was sent


def test_r10_j1_j2_items_are_matched_by_input_and_across_task_kinds(tmp_path: Path) -> None:
    store = ProjectStore(Env(tmp_path).store_dir)
    items = golden()
    # a spike under its own kind tuned on these items (as spike.status_once was)
    evaluation.fix(store, record(items, task_kind="spike.classify_once", purpose="development"),
                   by="test")  # fmt: skip
    relabelled = [{**g, "subject_sha256": "f" * 63 + str(i)} for i, g in enumerate(items)]
    with pytest.raises(PrometheosError, match="already seen"):  # same inputs, other declared file
        evaluation.fix(store, record(relabelled), by="test")


# ----- review cp22-a round 11 ---------------------------------------------------------------


def test_r11_f1_a_measurement_that_stops_part_way_spends_its_record(tmp_path: Path) -> None:
    import dataclasses

    from prometheos.intelligence import tasks as registered

    store = ProjectStore(Env(tmp_path).store_dir)
    home = tmp_path / "home"
    spend.set_monthly_cap(home, Decimal("5"))
    digest = evaluation.fix(store, record(golden()), by="test")
    stopped = T.Scripted([T.ok(), T.ok(), tp.Reply(429, b"{}")])
    with pytest.raises(PrometheosError, match="stopped before the golden set was done"):
        evaluation.measure(store, CLASSIFY_FILE, digest, T.gateway_executor(store, home, stopped))
    tuned = dataclasses.replace(CLASSIFY_FILE, instructions=CLASSIFY_FILE.instructions + " ")
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(registered.REGISTRY, tuned.kind, tuned)
        again = evaluation.measure(store, tuned, digest, T.gateway_executor(
            store, home, T.Scripted([T.ok() for _ in range(3)])))  # fmt: skip
    assert again["first_measurement"] is False and again["trusted"] is False


def test_k1_an_acceptance_record_is_never_replayed(tmp_path: Path) -> None:
    import sqlite3

    env = Env(tmp_path)
    store = ProjectStore(env.store_dir)
    digest = evaluation.fix(store, record(golden()), by="test")
    with pytest.raises(PrometheosError, match="measured once, through the gateway"):
        evaluation.measure(store, CLASSIFY_FILE, digest, run_module_cassettes([]))
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        assert conn.execute("SELECT count(*) FROM ai_eval_starts").fetchone()[0] == 0  # not spent
    finally:
        conn.close()


def run_module_cassettes(cassettes: list[dict[str, Any]]) -> Any:
    from prometheos.intelligence.run import CassetteExecutor

    return CassetteExecutor(cassettes)
