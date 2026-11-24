"""Regressions for the CP3 r1 review (reviewer-subagent-cp3-a)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env, create_entry
from mcp import Client

from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.mcp_adapter import build_server
from prometheos.skills import host_apply, host_plan, validate_skill


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _meeting_task(env: Env) -> tuple[dict[str, Any], str]:
    notes = (env.research / "meeting.txt").read_text("utf-8")
    captured = env.svc.capture_meeting(
        env.project_id, title="Supervision", text=notes, held_at=None,
        request_id=new_request_id(),
    )  # fmt: skip
    task = env.svc.task_prepare(
        env.project_id,
        purpose="meeting_extract",
        meeting_id=captured["meeting_id"],
        request_id=new_request_id(),
    )
    return captured, str(task["task_id"])


def _mcp(env: Env, name: str, args: dict[str, Any]) -> Any:
    async def main() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await client.call_tool(name, args)

    return anyio.run(main)


def _envelope(result: Any) -> dict[str, Any]:
    text = result.content[0].text
    return dict(json.loads(text[text.index("{") :]))


@pytest.mark.parametrize("bad", ["not-a-uuid", "x" * 5000, "00000000-0000-1000-8000-000000000000"])
def test_request_ids_are_uuid4_on_every_transport(env: Env, bad: str) -> None:
    _, task_id = _meeting_task(env)
    for tool in ("task_cancel", "task_submit"):
        args: dict[str, Any] = {"task_id": task_id, "request_id": bad}
        if tool == "task_submit":
            args["result"] = {"purpose": "meeting_extract", "items": []}
        result = _mcp(env, tool, args)
        assert result.is_error and _envelope(result)["error"]["code"] == "validation_error"
    with pytest.raises(PrometheosError) as exc:
        env.svc.task_cancel(env.project_id, task_id, bad)
    assert exc.value.code == "validation_error"
    task = env.svc.task_get(env.project_id, task_id)["task"]
    assert task["body"]["status"] == "prepared" and task["rev"] == 1


def _assert_closed_output_members(schema: dict[str, Any], name: str) -> None:
    def member(value: Any, seen: tuple[str, ...] = ()) -> None:
        assert isinstance(value, dict) and value, name
        if "$ref" in value:
            ref = value["$ref"]
            assert set(value) == {"$ref"}, name
            assert isinstance(ref, str) and ref.startswith("#/$defs/"), name
            key = ref.removeprefix("#/$defs/")
            definitions = schema.get("$defs", {})
            assert isinstance(definitions, dict), name
            assert key in definitions and ref not in seen, name
            member(definitions[key], (*seen, ref))
            return
        union_keys = set(value) & {"anyOf", "oneOf"}
        if union_keys:
            assert len(union_keys) == 1, name
            alternatives = value[next(iter(union_keys))]
            assert isinstance(alternatives, list) and alternatives, name
            for alternative in alternatives:
                member(alternative, seen)
            return
        assert value.get("type") == "object", name
        properties = value.get("properties")
        assert isinstance(properties, dict) and properties, name
        assert value.get("additionalProperties") is False, name
        assert any(
            isinstance(field, dict)
            and set(field) & {"type", "$ref", "const", "enum", "anyOf", "oneOf", "allOf"}
            for field in properties.values()
        ), name

    member(schema)


_CLOSED_OUTPUT = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "schema",
    [
        _CLOSED_OUTPUT,
        {
            "$defs": {"A": _CLOSED_OUTPUT, "B": _CLOSED_OUTPUT},
            "anyOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}],
        },
        {
            "$defs": {"A": _CLOSED_OUTPUT, "B": _CLOSED_OUTPUT},
            "oneOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}],
            "discriminator": {"propertyName": "op"},
        },
    ],
)
def test_closed_object_and_resolved_union_output_members_are_supported(schema: Any) -> None:
    _assert_closed_output_members(schema, "synthetic contract")


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"anyOf": []},
        {"oneOf": []},
        {"anyOf": [_CLOSED_OUTPUT], "oneOf": [_CLOSED_OUTPUT]},
        {"oneOf": [_CLOSED_OUTPUT, {}]},
        {"oneOf": [_CLOSED_OUTPUT, {"$ref": "#/$defs/Unknown"}]},
        {"oneOf": [_CLOSED_OUTPUT, _CLOSED_OUTPUT | {"additionalProperties": True}]},
        {"oneOf": [_CLOSED_OUTPUT, _CLOSED_OUTPUT | {"properties": {"payload": {}}}]},
        {"anyOf": [_CLOSED_OUTPUT, {}]},
        {"anyOf": [_CLOSED_OUTPUT, {"type": "string"}]},
        {"anyOf": [_CLOSED_OUTPUT, {"$ref": "#/$defs/Unknown"}]},
        {"anyOf": [_CLOSED_OUTPUT, {"$ref": "https://example.invalid/schema"}]},
        {"$defs": [], "anyOf": [{"$ref": "#/$defs/A"}]},
        {"$defs": {"A": {"$ref": "#/$defs/A"}}, "anyOf": [{"$ref": "#/$defs/A"}]},
        {"anyOf": [_CLOSED_OUTPUT, _CLOSED_OUTPUT | {"additionalProperties": True}]},
        {"anyOf": [_CLOSED_OUTPUT, _CLOSED_OUTPUT | {"properties": {}}]},
        {"anyOf": [_CLOSED_OUTPUT, _CLOSED_OUTPUT | {"properties": {"payload": {}}}]},
    ],
)
def test_missing_open_or_unconstrained_output_alternatives_are_refused(bad: Any) -> None:
    with pytest.raises(AssertionError):
        _assert_closed_output_members(bad, "synthetic invalid contract")


def test_mcp_tools_advertise_exact_schemas(env: Env) -> None:
    async def main() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    tools = anyio.run(main)
    submit = tools["task_submit"].input_schema["properties"]
    assert "pattern" in submit["request_id"] and "pattern" in submit["task_id"]
    result_schema = json.dumps(submit["result"])
    assert "meeting_extract" in result_schema and "next_steps" in result_schema
    request = tools["apply"].input_schema["properties"]["request"]
    assert {"schema_version", "request_id", "expected_seq", "operations"} <= set(
        request["properties"]
    )
    assert "$ref" not in json.dumps(request)
    budget = tools["context_get"].input_schema["properties"]["budget"]
    assert budget["minimum"] >= 1 and budget["maximum"] <= 200_000
    prepare = tools["task_prepare"].input_schema["properties"]["purpose"]
    assert set(prepare["enum"]) == {"meeting_extract", "notebook_draft", "next_steps"}
    for name, tool in tools.items():
        schema = tool.output_schema
        assert schema is not None, name
        _assert_closed_output_members(schema, name)
    record = tools["record_get"].output_schema
    assert {"id", "kind", "rev", "body"} <= set(record["properties"])


def test_malformed_task_result_is_a_validation_error(env: Env, tmp_path: Path) -> None:
    _, task_id = _meeting_task(env)
    bad = tmp_path / "result.json"
    for content in (b"{not json", b'{"purpose": "a", "purpose": "b"}'):
        bad.write_bytes(content)
        proc = subprocess.run(
            [sys.executable, "-m", "prometheos", "task", "submit", task_id, "--file", str(bad),
             "--request-id", new_request_id(), "--project", env.project_id, "--json"],
            env={**os.environ, "PROMETHEOS_HOME": str(env.home), "PYTHONIOENCODING": "utf-8"},
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        assert proc.returncode == 2 and out["error"]["code"] == "validation_error", out
        assert "Traceback" not in proc.stderr
    with pytest.raises(PrometheosError) as exc:
        env.svc.task_submit(env.project_id, task_id, b" " * 1_000_001, new_request_id())
    assert exc.value.code == "budget_exceeded"


def test_purged_and_restricted_tasks_read_correctly(env: Env) -> None:
    captured, task_id = _meeting_task(env)
    # Restricting the meeting makes the derived task restricted: owner-visible only.
    meeting = env.svc.get(env.project_id, OWNER, captured["meeting_id"], None)
    env.apply_protected(
        [
            {
                "op": "revise", "id": captured["meeting_id"], "base_rev": meeting["rev"],
                "sensitivity": "restricted", "occurred_at": meeting["occurred_at"],
                "body": meeting["body"],
            }
        ]
    )  # fmt: skip
    task = env.svc.get(env.project_id, OWNER, task_id, None)
    assert task["sensitivity"] == "restricted"
    with pytest.raises(PrometheosError):
        env.svc.task_get(env.project_id, task_id)
    assert env.svc.task_get(env.project_id, task_id, OWNER)["task_id"] == task_id
    plan = env.svc.purge_plan(env.project_id, [captured["source_id"]], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    for caller in (None, OWNER):
        with pytest.raises(PrometheosError) as exc:
            env.svc.task_get(env.project_id, task_id, caller)
        assert exc.value.code == "not_found"


@pytest.mark.parametrize(
    ("front", "message"),
    [
        ("name: s\ndescription:\n  - a\n", "required"),
        ("name: s\ndescription: [a, b]\n", "not supported"),
        ("name: s\ndescription: {a: b}\n", "not supported"),
        ("name: s\ndescription: null\n", "not a string"),
        ("name: s\ndescription: ~\n", "not a string"),
        ("name: s\ndescription: 12\n", "not a string"),
        ('name: s\ndescription: "unclosed\n', "unclosed"),
        ("name: s\ndescription: &a anchored\n", "not supported"),
        ("name: s\ndescription: x\nmetadata:\n  a: x\n  a: y\n", "duplicate"),
        ("name: s\ndescription: x\nmetadata:\n  a: x\n    b: c\n", "indentation"),
        ("name: s\ndescription: a: b\n", "ambiguous"),
        ("name: s\ndescription: x\ncompatibility:\n", "required"),
    ],
)
def test_validator_rejects_unsupported_yaml(tmp_path: Path, front: str, message: str) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text(f"---\n{front}---\n\nBody.\n", "utf-8")
    errors = validate_skill(skill)
    assert errors and any(message in e for e in errors), errors


def test_validator_accepts_supported_forms(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        '---\nname: s\ndescription: "Plain: quoted with colon"\n'
        "compatibility: 'Needs it''s tools'\nmetadata:\n  license-status: pending\n---\n\nBody.\n",
        "utf-8",
    )
    assert validate_skill(skill) == []


def test_host_setup_refuses_odd_config_and_is_not_partial(tmp_path: Path) -> None:
    pid = "project_00000000-0000-4000-8000-000000000001"
    for content in ("[1, 2]", '{"mcpServers": []}', '{"mcpServers": "x"}'):
        target = tmp_path / f"t{abs(hash(content))}"
        target.mkdir()
        (target / ".mcp.json").write_text(content, "utf-8")
        with pytest.raises(PrometheosError) as exc:
            host_plan("claude-code", target, pid)
        assert exc.value.code == "validation_error"
        assert (target / ".mcp.json").read_text("utf-8") == content
    if sys.platform == "win32":
        target = tmp_path / "junction"
        (target / ".claude").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target / ".claude" / "skills"), str(outside)],
            check=True, capture_output=True,
        )  # fmt: skip
        plan = host_plan("claude-code", target, pid)
        with pytest.raises(PrometheosError):
            host_apply(plan)
        assert not (target / ".mcp.json").exists()  # nothing written before the refusal
        assert list(outside.iterdir()) == []


def test_meeting_notes_in_task_context_redact_hidden_ids(env: Env) -> None:
    hidden = new_id("entry")
    env.apply([create_entry(hidden, sensitivity="restricted", title="Hidden")], caller=OWNER)
    notes = f"Discussed {hidden} and next steps.\n"
    captured = env.svc.capture_meeting(
        env.project_id, title="Sync", text=notes, held_at=None, request_id=new_request_id()
    )
    task = env.svc.task_prepare(
        env.project_id, purpose="meeting_extract", meeting_id=captured["meeting_id"],
        request_id=new_request_id(),
    )  # fmt: skip
    assert hidden not in json.dumps(task)
    assert hidden not in json.dumps(env.svc.task_get(env.project_id, task["task_id"], AGENT))
