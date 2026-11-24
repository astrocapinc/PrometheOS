"""CP10.6 operation-family tools and the generated HTTP routes (decision 0022).

Exact schemas and annotations of the family tools, paging of large results, confinement of
file operands to approved research roots, what an agent may and may not create, and the routes
that act for the owner (skill trust, model install and removal, purge commit, the credential).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp10_world import World, running_routes
from hub_support import Hub, running_hub
from mcp import Client

from prometheos import agentops, registry
from prometheos.hub.app import AGENT_ROUTES
from prometheos.mcp_adapter import (
    FAMILY_ANNOTATIONS,
    READ_ONLY,
    WRITE_FILE,
    WRITE_IDEMPOTENT,
    WRITE_NEW,
    build_server,
    family_request_schema,
)
from prometheos.registry import inventory

agentops.load()


@pytest.fixture
def synthetic_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMETHEOS_ENABLE_SYNTHETIC_ENGINE", "1")
    monkeypatch.setenv("PROMETHEOS_CAPTURE_BACKEND", "synthetic")


@pytest.fixture
def world(tmp_path: Path, synthetic_audio: None) -> World:
    return World(tmp_path)


# ----- schemas and annotations -------------------------------------------------------------------


def test_family_tools_advertise_one_exact_member_per_sub_operation() -> None:
    mcp, tools = inventory.mcp_entries()
    families = {name: fam for name, fam in agentops.FAMILIES.items() if fam.ops}
    # CP17 adds adoption_read and adoption_manage; CP11 adds library and library_manage, and
    # S11.4 matrix and matrix_manage; A07 (CP22.4) intelligence_read and intelligence_manage;
    # decision 0112 evidence_manage.
    assert len(families) == 25
    for name, fam in families.items():
        schema = tools[name] if name in tools else None
        assert schema is None  # a family tool is listed by its sub-operations, not by name
        for op_id in fam.ops:
            a = agentops.OPS[op_id]
            entry = tools[f"{name}#{a.sub}"]
            member = entry["input"]
            assert member["type"] == "object" and member["additionalProperties"] is False
            assert member["properties"]["op"]["const"] == a.sub
            assert set(member["properties"]) == {"op", *a.params.model_fields}
            assert "op" in member["required"]
            shown = entry["output"]
            assert shown["properties"]["op"]["const"] == a.sub
            result = shown["properties"]["result"]
            variants = result.get("anyOf") or [result]
            assert all(v["additionalProperties"] is False and v["properties"] for v in variants)
            assert f"{name}#{a.sub}" in mcp
    # The request is a discriminated union: a client can pick the member from ``op`` alone.
    schema = family_request_schema("skills_manage")
    assert schema["discriminator"] == {"propertyName": "op"}
    assert {m["properties"]["op"]["const"] for m in schema["oneOf"]} == {
        agentops.OPS[o].sub for o in agentops.FAMILIES["skills_manage"].ops
    }


def test_family_annotations_hold_for_every_sub_operation() -> None:
    """A tool's hints must be true of each operation it carries (spec/04)."""
    by_id = registry.by_id()
    assert FAMILY_ANNOTATIONS == {
        "read": READ_ONLY,
        "write": WRITE_IDEMPOTENT,
        "write_new": WRITE_NEW,
        "destructive": WRITE_FILE,
    }
    # An export writes a new file inside a research root: it changes no record, but it is not
    # a read, so the tools that carry it are the changing ones (and say it may write files).
    file_writers = {"skill_export", "paper_export"}
    for name, fam in agentops.FAMILIES.items():
        for op_id in fam.ops:
            declared = by_id[op_id]
            if fam.effect == "read":
                assert not declared.mutates and op_id not in file_writers, (name, op_id)
            else:
                assert declared.mutates or op_id in file_writers, (name, op_id)


def test_every_operation_is_reachable_in_exactly_one_family_tool_or_already_was() -> None:
    routes = [a.mcp_route for a in agentops.OPS.values() if a.mcp_route]
    assert len(routes) == len(set(routes))
    for op_id, a in agentops.OPS.items():
        declared = registry.by_id()[op_id]
        if a.mcp_route is not None:
            assert a.mcp_route in declared.mcp
        if a.http is not None:
            assert a.http in declared.http


# ----- agent routes generated from the registry --------------------------------------------------


def test_generated_agent_routes_cover_agent_operations_and_never_owner_ones() -> None:
    for a in agentops.OPS.values():
        if a.http is None:
            continue
        operation = registry.by_id()[a.id]
        if operation.authority in ("owner_channel", "owner_scope"):
            assert a.http not in AGENT_ROUTES, a.id
        else:
            assert a.http in AGENT_ROUTES, a.id
    assert "POST /api/v1/jobs" in AGENT_ROUTES
    assert "POST /api/v1/retention/purge/plan" not in AGENT_ROUTES  # owner scope
    assert "PUT /api/v1/provider/secrets" not in AGENT_ROUTES  # owner channel
    assert "DELETE /api/v1/provider/secrets" not in AGENT_ROUTES
    assert "POST /api/v1/retention/purge/{plan_id}/commit" in AGENT_ROUTES  # an approval request
    assert "POST /api/v1/skills/{ref}/trust" in AGENT_ROUTES


# ----- large results page ------------------------------------------------------------------------


def mcp(world: World, name: str, args: dict[str, Any]) -> dict[str, Any]:
    async def go() -> Any:
        async with Client(build_server(world.svc, world.pid)) as client:
            return await client.call_tool(name, args)

    result = anyio.run(go)
    assert not result.is_error, result.content[0].text  # type: ignore[union-attr]
    return result.structured_content["result"]  # type: ignore[no-any-return]


def test_a_large_text_reads_page_by_page_to_the_same_whole(world: World) -> None:
    from cp1_support import OWNER

    whole = world.svc.version_text(world.pid, OWNER, world.venue_version)
    text, offset, pages = "", 0, 0
    while True:
        page = mcp(
            world,
            "record_read",
            {"request": {"op": "version_text", "version_id": world.venue_version,
                         "offset": offset, "limit": 50}},
        )  # fmt: skip
        text += page["text"]
        pages += 1
        assert page["total_chars"] == len(whole["text"]) and page["offset"] == offset
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + len(page["text"]) and page["truncated"] is True
        offset = page["next_offset"]
    assert pages > 3 and text == whole["text"]


def test_bytes_read_page_by_page_verify_against_the_whole(world: World) -> None:
    from cp1_support import OWNER

    data, _media = world.svc.version_content(world.pid, OWNER, world.manuscript_version)
    out, offset = b"", 0
    while True:
        page = mcp(
            world,
            "record_read",
            {"request": {"op": "version_content", "version_id": world.manuscript_version,
                         "offset": offset, "limit": 100}},
        )  # fmt: skip
        chunk = base64.b64decode(page["data"])
        assert page["length"] == len(chunk) and page["encoding"] == "base64"
        assert page["total_bytes"] == len(data)
        out += chunk
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert out == data


def test_a_notebook_export_stays_on_its_snapshot_between_pages(world: World) -> None:
    from cp1_support import create_entry

    for i in range(3):
        world.env.apply([create_entry(title=f"Note {i}", text="A paragraph of text. " * 8)])
    first = mcp(world, "record_read", {"request": {"op": "notebook_export", "limit": 200}})
    pinned = first["project_seq"]
    assert first["next_offset"] is not None
    world.env.apply(
        [create_entry(title="Written between the pages", text="x")]
    )  # the project moves
    text, offset = first["text"], first["next_offset"]
    while offset is not None:
        page = mcp(
            world,
            "record_read",
            {"request": {"op": "notebook_export", "limit": 200, "offset": offset,
                         "at_seq": pinned}},
        )  # fmt: skip
        assert page["project_seq"] == pinned
        text += page["text"]
        offset = page["next_offset"]
    assert "Written between the pages" not in text
    whole = mcp(world, "record_read", {"request": {"op": "notebook_export", "limit": 100000,
                                                    "at_seq": pinned}})  # fmt: skip
    assert text == whole["text"]


# ----- file operands stay inside approved roots --------------------------------------------------


@pytest.mark.parametrize(
    "operand",
    [
        {"root_alias": "research", "path": "../outside"},
        {"root_alias": "research", "path": "C:\\Windows\\win.ini"},
        {"root_alias": "research", "path": "/etc/passwd"},
        {"root_alias": "research", "path": "sub/../../outside"},
        {"root_alias": "unapproved", "path": "pkgs/synthetic-evidence"},
    ],
)
def test_file_operands_are_refused_outside_the_approved_roots(
    world: World, operand: dict[str, str]
) -> None:
    with running_routes(world) as routes:
        for op in ("skill_inspect", "skill_import_plan", "skill_validate"):
            for via in (routes.mcp, routes.http):
                outcome = via(op, dict(operand))
                assert outcome["ok"] is False and outcome["code"] == "policy_denied", (op, outcome)
        write = routes.mcp(
            "skill_create",
            {
                "name": "x-skill",
                "description": "x",
                "license": "MIT",
                "root_alias": operand["root_alias"],
                "folder": operand["path"],
            },
        )
        assert write["ok"] is False and write["code"] == "policy_denied"
    assert not (world.root.parent / "outside").exists()


def test_a_reply_names_files_by_root_and_relative_path(world: World) -> None:
    created = mcp(
        world,
        "skills_manage",
        {"request": {"op": "create", "name": "named-skill", "description": "d", "license": "MIT",
                     "root_alias": "research", "folder": "exports"}},
    )  # fmt: skip
    assert created["out"] == {"root_alias": "research", "path": "exports"}
    text = json.dumps(created)
    assert str(world.root) not in text and str(world.root).replace("\\", "\\\\") not in text
    exported = mcp(
        world,
        "papers_manage",
        {"request": {"op": "export", "paper_id": world.paper_id, "root_alias": "research",
                     "path": "exports/b.zip"}},
    )  # fmt: skip
    assert exported["out"] == {"root_alias": "research", "path": "exports/b.zip"}
    assert (world.root / "exports" / "b.zip").is_file()
    assert str(world.root) not in json.dumps(exported)


def test_an_agent_cannot_create_another_project(world: World) -> None:
    before = world.svc.list_projects()
    with running_routes(world) as routes:
        params = {"title": "Agent study", "objective": "Compare."}
        assert routes.mcp("project_init", params)["code"] == "policy_denied"
        assert routes.http("project_init", params)["code"] == "policy_denied"
        for extra in (
            {"roots": {"x": "C:\\"}},
            {"store": "C:\\elsewhere"},
            {"declare_local": True},
        ):
            refused = routes.mcp("project_init", {"title": "t", "objective": "o", **extra})
            assert refused["ok"] is False and refused["code"] == "validation_error"
            assert (
                routes.http("project_init", {"title": "t", "objective": "o", **extra})["code"]
                == "policy_denied"
            )
    assert world.svc.list_projects() == before


# ----- routes that act for the owner -------------------------------------------------------------


def agent_request(hub: Hub, method: str, path: str, body: Any = None) -> Any:
    token = hub.gate.new_agent_token()
    return hub.request(
        method, path, body, cookie=None, csrf=None, origin=None,
        headers={"Authorization": f"Bearer {token}"},
    )  # fmt: skip


def test_skill_trust_is_an_approval_request_for_agents_and_the_owners_act_in_the_hub(
    world: World,
) -> None:
    ref = "synthetic-evidence@1.0.0"
    world.svc.skill_revoke(ref)
    digest = world.svc.catalog.get(ref)["package_digest"]
    with running_hub(world.svc, world.pid) as hub:
        hub.pair()
        asked = agent_request(
            hub, "POST", f"/api/v1/skills/{ref}/trust", {"package_digest": digest}
        )
        assert asked.status == 403 and asked.json()["error"]["code"] == "approval_required"
        assert world.svc.catalog.get(ref)["status"] == "revoked"  # nothing changed
        stale = hub.request("POST", f"/api/v1/skills/{ref}/trust", {"package_digest": "0" * 64})
        assert stale.status == 409
        assert world.svc.catalog.get(ref)["status"] == "revoked"
        done = hub.request("POST", f"/api/v1/skills/{ref}/trust", {"package_digest": digest})
        assert done.status == 200, done.body
        assert world.svc.catalog.get(ref)["status"] != "revoked"


def test_model_install_and_removal_are_requests_for_agents(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMETHEOS_MODELS_DIR", str(world.root / "empty-models"))
    plan = world.svc.audio_models_plan()
    with running_hub(world.svc, world.pid) as hub:
        hub.pair()
        for path, body in (
            ("/api/v1/audio/models/install", {"plan_sha256": plan["plan_sha256"]}),
            ("/api/v1/audio/models/remove", {}),
        ):
            asked = agent_request(hub, "POST", path, body)
            assert asked.status == 403 and asked.json()["error"]["code"] == "approval_required"
        # The owner's install of a stale plan is refused before anything is downloaded.
        stale = hub.request("POST", "/api/v1/audio/models/install", {"plan_sha256": "0" * 64})
        assert stale.status >= 400 and stale.json()["error"]["code"] in (
            "revision_conflict",
            "validation_error",
        )
        assert not world.svc.audio_models_status()["models"][0]["installed"]


def test_purge_is_planned_by_the_owner_and_only_requested_by_an_agent(world: World) -> None:
    from cp1_support import OWNER, create_entry

    made = world.env.apply([create_entry(title="Purge me", text="Gone soon.")])
    target = made["results"][0]["id"]
    with running_hub(world.svc, world.pid) as hub:
        hub.pair()
        # An agent token never reaches the owner-scope plan route.
        denied = agent_request(hub, "POST", "/api/v1/retention/purge/plan", {"ids": [target]})
        assert denied.status == 403 and denied.json()["error"]["code"] == "policy_denied"
        plan = hub.request("POST", "/api/v1/retention/purge/plan", {"ids": [target]}).json()
        assert plan["closure"] == [target]
        asked = agent_request(
            hub, "POST", f"/api/v1/retention/purge/{plan['plan_id']}/commit",
            {"plan_sha256": plan["plan_sha256"]},
        )  # fmt: skip
        assert asked.status == 403 and asked.json()["error"]["code"] == "approval_required"
        assert world.svc.get(world.pid, OWNER, target, None)["body"]["title"] == "Purge me"
        stale = hub.request(
            "POST", f"/api/v1/retention/purge/{plan['plan_id']}/commit", {"plan_sha256": "0" * 64}
        )
        assert stale.status == 409
        done = hub.request(
            "POST", f"/api/v1/retention/purge/{plan['plan_id']}/commit",
            {"plan_sha256": plan["plan_sha256"]},
        )  # fmt: skip
        assert done.status == 200 and done.json()["purge_receipt"]["records_purged"] == 1
        assert world.svc.get(world.pid, OWNER, target, None)["body"] == {
            "purged": True,
            "purged_at_seq": done.json()["commit"]["project_seq"],
        }


def test_the_credential_is_entered_only_by_the_owner_and_never_returned(world: World) -> None:
    from prometheos.providers.secrets import MemorySecretStore

    store = MemorySecretStore()
    world.svc.secret_store = store
    value = "synthetic-credential-value-0042"
    with running_hub(world.svc, world.pid) as hub:
        hub.pair()
        for method, path, body in (
            ("PUT", "/api/v1/provider/secrets", {"reference": "demo-key", "value": value}),
            ("DELETE", "/api/v1/provider/secrets?reference=demo-key", None),
        ):
            refused = agent_request(hub, method, path, body)
            assert refused.status == 403 and refused.json()["error"]["code"] == "policy_denied"
        assert store.get("demo-key") is None
        refused = hub.request(
            "PUT", "/api/v1/provider/secrets", {"reference": "demo-key", "value": value}
        )
        assert refused.status == 405 and store.get("demo-key") is None
        store.set("demo-key", value)  # OS fixture; entry never crosses HTTP
        gone = hub.request("DELETE", "/api/v1/provider/secrets?reference=demo-key")
        assert gone.status == 200 and gone.json()["removed"] is True
        assert store.get("demo-key") is None
    # The value is in no file of the store.
    for path in world.env.store_dir.rglob("*"):
        if path.is_file():
            assert value.encode() not in path.read_bytes(), path


# ----- recording control over the agent transports (decisio --------------------------------------


def test_an_agent_controls_only_a_recording_it_started(world: World) -> None:
    with running_routes(world) as routes:
        listed = routes.mcp("recording_list", {"include_done": True})
        ids = {x["session_id"] for x in listed["result"]["sessions"]}
        assert world.session in ids  # a normal-sensitivity session reads like any other record
        paused = routes.mcp("recording_control", {"session_id": world.session, "action": "pause"})
        assert paused["ok"] and paused["result"]["pause_requested"] is True
        denied = routes.mcp(
            "recording_control", {"session_id": world.owner_session, "action": "stop"}
        )
        assert denied["ok"] is False and denied["code"] in ("policy_denied", "not_found")
        # Starting stays an approval request: there is no start in the family tool.
        subs = {agentops.OPS[o].sub for o in agentops.FAMILIES["recording_manage"].ops}
        assert subs == {"control", "recover", "discard"}
