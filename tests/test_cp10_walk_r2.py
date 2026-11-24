"""Reproduced CP10.6 round-two authority, contract and harness regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env, request
from cp4_support import import_package, write_package
from hub_support import running_hub
from mcp import Client
from test_cp1_cli import Cli
from test_cp10_agentops import agent_request
from test_cp10_walk import halves, walk

from prometheos import agentops, registry
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server


def call(svc: Any, pid: str, tool: str, params: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(build_server(svc, pid)) as client:
            result = await client.call_tool(tool, params)
            if result.is_error:
                text = result.content[0].text
                return json.loads(text[text.index("{") :])["error"]
            return result.structured_content

    return anyio.run(run)


def root_request(env: Env, path: str) -> bytes:
    info = env.svc.project_info(env.project_id, OWNER)
    settings = info["settings"] | {"allowed_roots": [{"alias": "research", "path": path}]}
    return request(
        env.project_id,
        [{"op": "project_update", "base_rev": info["settings_rev"], "settings": settings}],
        env.seq(),
    )


def test_redaction_label_cannot_be_written_as_a_root_by_any_transport(tmp_path: Path) -> None:
    env = Env(tmp_path)
    before = env.svc.project_info(env.project_id, OWNER)
    raw = root_request(env, "<research root research>")
    with pytest.raises(PrometheosError, match="invalid"):
        env.svc.apply(env.project_id, raw, AGENT)
    error = call(env.svc, env.project_id, "apply", {"request": json.loads(raw)})
    assert error["code"] == "validation_error"
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        for reply in (
            agent_request(hub, "POST", "/api/v1/transactions", json.loads(raw)),
            hub.request("POST", "/api/v1/decisions/preview", {"request": json.loads(raw)}),
        ):
            assert reply.json()["error"]["code"] == "validation_error"
            assert reply.status == 400 or reply.json()["would_apply"] is False
    cli = Cli(tmp_path)
    code, error, _ = cli("apply", "--file", "-", "--project", env.project_id, stdin=raw)
    assert code != 0 and error["error"]["code"] == "validation_error"
    assert env.svc.project_info(env.project_id, OWNER) == before


def test_only_owner_typed_root_preview_exposes_exact_old_and_new_paths(tmp_path: Path) -> None:
    env = Env(tmp_path)
    new = tmp_path / "new-root-canary"
    new.mkdir()
    raw = root_request(env, str(new))
    expected = [{"alias": "research", "old_path": str(env.research), "new_path": str(new)}]
    assert env.svc.review_request(env.project_id, raw, OWNER)["root_changes"] == expected
    assert env.svc.review_request(env.project_id, raw, AGENT)["root_changes"] == []
    # Arbitrary preview strings still scrub; only the typed root-change field is exempt.
    original = env.svc.review_request

    def preview(*args: Any, **kwargs: Any) -> Any:
        return original(*args, **kwargs) | {"canary": str(new)}

    env.svc.review_request = preview
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        reply = hub.request("POST", "/api/v1/decisions/preview", {"request": json.loads(raw)})
        assert reply.status == 200, reply.body
        shown = reply.json()
        assert shown["root_changes"] == expected
        assert str(new) not in shown["canary"]
        asked = agent_request(hub, "POST", "/api/v1/transactions", json.loads(raw)).json()
        assert asked["error"]["code"] == "approval_required"
        aid = asked["error"]["details"]["approval_request_id"]
        owner = hub.request("GET", f"/api/v1/approvals/{aid}").json()
        assert owner["preview"]["root_changes"] == expected
        for result in (
            agent_request(hub, "GET", f"/api/v1/approvals/{aid}"),
            hub.request("GET", "/api/v1/project"),
        ):
            text = result.body.decode().replace("\\\\", "\\")
            assert str(env.research) not in text and str(new) not in text


@pytest.mark.parametrize("operation", ["skill_install", "skill_update", "skill_remove"])
def test_projection_apply_requires_review_and_is_bound_rechecked_and_replayed(
    tmp_path: Path, operation: str
) -> None:
    env = Env(tmp_path)
    svc, pid = env.svc, env.project_id
    ref = "synthetic-evidence@1.0.0"
    import_package(svc, write_package(tmp_path / "package"))
    target = env.research / "host"
    target.mkdir()
    if operation in ("skill_update", "skill_remove"):
        plan = svc.skill_install(ref, "claude-code", target, None, None, None)["plan"]
        svc.skill_install(
            ref, "claude-code", target, plan["plan_sha256"], None, None, authority=OWNER_ACT
        )
    params = {"host": "claude-code", "root_alias": "research", "folder": "host"}
    params.update({"name": "synthetic-evidence"} if operation == "skill_remove" else {"ref": ref})
    ctx = agentops.Ctx(svc, pid, AGENT)
    op = agentops.OPS[operation]
    plan = agentops.execute(op, ctx, params)["plan"]
    params["plan_sha256"] = plan["plan_sha256"]
    with pytest.raises(PrometheosError) as denied:
        agentops.execute(op, ctx, params)
    assert denied.value.code == "approval_required"
    aid = denied.value.details["approval_request_id"]
    shown = svc.approval_owner_view(pid, aid)
    stored = shown["plan"]
    assert stored["package_digest"] == svc.catalog.get(ref)["package_digest"]
    assert stored["status"] == svc.catalog.get(ref)["status"]
    assert stored["files"] and "executable" in stored["files"][0]
    assert "findings" in stored and "unknown_rights" in stored
    svc.approval_decide(pid, aid, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT)
    result = svc.approval_resume(pid, AGENT, aid)
    assert result["state"] == "used"
    assert svc.approval_resume(pid, AGENT, aid)["replayed"] is True


def test_projection_changed_catalogue_and_target_cannot_spend_a_grant(tmp_path: Path) -> None:
    env = Env(tmp_path)
    ref = "synthetic-evidence@1.0.0"
    import_package(env.svc, write_package(tmp_path / "package"))
    target = env.research / "host"
    target.mkdir()
    plan = env.svc.skill_install(ref, "claude-code", target, None, None, None)["plan"]
    params = {
        "ref": ref,
        "host": "claude-code",
        "root_alias": "research",
        "folder": "host",
        "plan_sha256": plan["plan_sha256"],
        "record_in_project": False,
        "resolve": None,
    }
    with pytest.raises(PrometheosError) as denied:
        env.svc.approval_request(env.project_id, AGENT, "skill_install", params)
    aid = denied.value.details["approval_request_id"]
    shown = env.svc.approval_owner_view(env.project_id, aid)
    env.svc.approval_decide(
        env.project_id, aid, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )
    env.svc.skill_revoke(ref)
    with pytest.raises(PrometheosError):
        env.svc.approval_resume(env.project_id, AGENT, aid)
    assert env.svc.approval_owner_view(env.project_id, aid)["state"] == "granted"
    assert not list(target.iterdir())


def test_noninteractive_credential_set_refuses_even_a_supplied_capability(tmp_path: Path) -> None:
    env = Env(tmp_path)
    digest = env.svc.provider_secret_digest("set", "synthetic-key")
    cap = env.svc.grant(
        env.project_id,
        operation="provider_configure",
        digest=digest,
        label="SYNTHETIC-TEST credential",
        synthetic=True,
    )
    path = tmp_path / "capability"
    path.write_text(cap.secret, encoding="utf-8")
    cli = Cli(tmp_path)
    code, error, _ = cli(
        "provider",
        "secret",
        "set",
        "--reference",
        "synthetic-key",
        "--project",
        env.project_id,
        "--capability-file",
        str(path),
        stdin=b"synthetic-secret",
    )
    assert code != 0 and error["error"]["code"] == "policy_denied"
    conn = env.svc.open(env.project_id).connect(readonly=True)
    try:
        assert (
            conn.execute(
                "SELECT uses FROM capabilities WHERE id = ?", (cap.capability_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


@pytest.mark.parametrize("field", ["required_scope", "owner_route"])
def test_wa1_authority_detail_mutants_fail_the_full_response_judge(field: str) -> None:
    from prometheos.domain.canonical import canonical_sha256

    doc, outcomes = halves()
    envelope = {
        "code": "approval_required",
        "details": {
            "required_scope": "exact_request",
            "owner_route": {"hub": "/approvals/request", "cli": "prometheos decision review"},
        },
    }
    changed = json.loads(json.dumps(envelope))
    changed["details"][field] = "mutated"
    hub_step = next(s for s in doc["hub"]["steps"] if s["id"] == "K2.2")
    mcp_step = next(s for s in doc["mcp"]["steps"] if s["id"] == "K2.2")
    hub_step["normalised_response_sha256"] = canonical_sha256(walk.stable(envelope))
    mcp_step["normalised_response_sha256"] = canonical_sha256(walk.stable(changed))
    assert any("K2.2" in f for f in walk.judge(doc, outcomes)[1])


def test_wa2_context_section_reassignment_is_not_normalised_away() -> None:
    first = "## findings\n- CLAIM [supported_within_conditions] result\n## assumptions\n- drag"
    mutant = "## findings\n- drag\n## assumptions\n- CLAIM [supported_within_conditions] result"
    assert walk.stable(first) != walk.stable(mutant)


def test_registry_http_contracts_are_concrete_and_credential_route_is_absent() -> None:
    from prometheos.hub.app import ProviderConfigBody, ProviderRunBody
    from prometheos.schemas import REGISTRY

    op = registry.by_id()["provider_approve"]
    bindings = {route: (req, res) for route, req, res in op.http_contracts}
    assert REGISTRY[bindings["POST /api/v1/provider/run"][0].split(":")[1]] is ProviderRunBody
    assert (
        REGISTRY[bindings["POST /api/v1/provider/configure"][0].split(":")[1]] is ProviderConfigBody
    )
    assert registry.by_id()["provider_secret_set"].http == ()


def test_unexpected_mcp_resource_exception_never_exposes_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("private-resource-canary " + str(tmp_path / "secret-file"))

    monkeypatch.setattr(env.svc, "context", broken)

    async def read() -> None:
        async with Client(build_server(env.svc, env.project_id)) as client:
            with pytest.raises(Exception) as error:
                await client.read_resource(f"prometheos://project/{env.project_id}/context")
            text = str(error.value)
            assert "private-resource-canary" not in text and "secret-file" not in text
            assert "internal error in the MCP server" in text

    anyio.run(read)


def test_other_projects_are_hidden_from_bound_agent_sessions(tmp_path: Path) -> None:
    env = Env(tmp_path)
    other = env.svc.init_project(
        title="Private other project",
        objective="Private objective",
        roots={},
        store=None,
        declare_local=False,
    )
    shown = call(env.svc, env.project_id, "administration_read", {"request": {"op": "projects"}})
    assert [p["project_id"] for p in shown["result"]["projects"]] == [env.project_id]
    assert other["project_id"] not in json.dumps(shown)
    with running_hub(env.svc, env.project_id) as hub:
        reply = agent_request(hub, "GET", "/api/v1/projects")
        assert reply.status == 200
        assert other["project_id"] not in reply.body.decode()
        hub.pair()
        assert len(hub.request("GET", "/api/v1/projects").json()["projects"]) == 2


def test_provider_owner_http_replies_validate_against_their_actual_contracts(
    tmp_path: Path,
) -> None:
    from prometheos.domain.models import ProviderOwnerConfigureResult
    from prometheos.providers.config import default_config
    from prometheos.store.operational import RunView

    env = Env(tmp_path)
    config = default_config().model_dump(mode="json")
    digest = env.svc.provider_configure_preview(env.project_id, config, 0)["digest"]
    run = env.svc.run_start(
        env.project_id,
        OWNER,
        "source_import_plan",
        {"root_alias": "research", "paths": ["baseline.md"]},
        mode="none",
    )
    env.svc.provider_preview = lambda *a, **kw: {"digest": "0" * 64, "needs_egress_grant": False}
    env.svc.provider_run = lambda *a, **kw: run
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        configured = hub.request(
            "POST", "/api/v1/provider/configure", {"config": config, "digest": digest}
        )
        assert configured.status == 200
        ProviderOwnerConfigureResult.model_validate(configured.json(), strict=True)
        sent = hub.request(
            "POST", "/api/v1/provider/run", {"purpose": "next_steps", "digest": "0" * 64}
        )
        assert sent.status == 200
        RunView.model_validate(sent.json(), strict=True)


def test_checkpoint_run_directory_expands_as_argv_and_walk_evidence_is_tree_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("cp106_runner", root / "tools/run_checkpoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The repository-relative POSIX run folder (review cp10-browser R3-F1): commands run in the
    # repository root, and the verifier expands the declared template with the receipt's folder.
    target = "development/receipts/CP10/run directory"
    expanded = module.expand_command(
        [sys.executable, "script.py", "--out", "{run_dir}/walk", "literal$()`text"], target
    )
    assert expanded[-2] == "development/receipts/CP10/run directory/walk"
    assert expanded[-1] == "literal$()`text"
    import tree_manifest

    before = tree_manifest.tree_sha256(tmp_path)
    evidence = tmp_path / "development/receipts/CP10/runner-r2-unit/walk"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "transcript.json").write_text("{}", encoding="utf-8")
    assert tree_manifest.tree_sha256(tmp_path) == before


def test_checkpoint_runner_passes_expanded_argv_and_keeps_the_suite_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import subprocess

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "cp106_stub_runner", root / "tools/run_checkpoint.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "tools").mkdir()
    suite = {
        "suites": {
            "CP10": {
                "C10-WALK": {
                    "summary": "stub walk",
                    "commands": {"walk": ["stub-command", "--out", "{run_dir}/walk"]},
                }
            }
        }
    }
    template = json.dumps(suite).encode()
    (tmp_path / "tools/checkpoint_suites.json").write_bytes(template)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    seen = []

    def stub(argv: list[str], **kwargs: Any) -> Any:
        seen.append(argv)
        if argv[argv.index("--name") + 1] == "env":
            # The runner reads the code under test from the report (decision 0051).
            report = {
                "code_under_test": {
                    "package_in_candidate": True,
                    "pythonpath": [],
                    "prometheos_package": "src/prometheos",
                    "environment": ".venv",
                }
            }
            run = Path(argv[argv.index("--run-dir") + 1])
            (run / "env.stdout.txt").write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.subprocess, "run", stub)
    monkeypatch.setattr(module.tree_manifest, "write_manifest", lambda path, root: "synthetic-tree")
    monkeypatch.setattr(module.tree_manifest, "tree_sha256", lambda root: "synthetic-tree")
    assert module.main(["CP10", "synthetic-run"]) == 0
    run_dir = tmp_path / "development/receipts/CP10/synthetic-run"
    assert seen[-1][-3:] == [
        "stub-command",
        "--out",
        "development/receipts/CP10/synthetic-run/walk",
    ]
    assert (run_dir / "suite.json").read_bytes() == template
