"""Two JSON depth limits (decision 0029, revised after independent review).

Outside input is admitted to ``MAX_INPUT_DEPTH`` = 64 levels; everything the application
writes, wraps or re-reads is held only to ``MAX_INTERNAL_DEPTH`` = 96. The margin is what
keeps an admitted value readable after the application has put envelopes around it. These
tests drive the largest admissible input through every surface that wraps it, and cover the
other review findings: a linear scan, a bounded request body, researchers' deep JSON sources
and non-retryable internal overflow.
"""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import AGENT, OWNER, Env, create_entry
from cp4_support import import_package, install, write_package
from hub_support import Hub, running_hub
from mcp import Client
from pydantic import TypeAdapter, ValidationError
from test_cp1_cli import Cli

from prometheos.domain import canonical
from prometheos.domain.canonical import (
    CanonicalJSONError,
    dumps_canonical,
    loads_strict,
)
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import JsonObject, PrometheosError
from prometheos.hub.security import CSRF_HEADER, SESSION_COOKIE

INPUT = 64  # the documented admission limit
INTERNAL = 96  # the documented internal ceiling
FAST = 5.0  # seconds; the real costs are milliseconds, the bound only catches quadratic scans


def nest(levels: int, leaf: Any = "leaf") -> Any:
    """``levels`` nested objects around ``leaf`` (the value is ``levels`` deep)."""
    value = leaf
    for _ in range(levels):
        value = {"k": value}
    return value


def depth_of(value: Any) -> int:
    best, pending = 0, [(value, 1)]
    while pending:
        node, level = pending.pop()
        if isinstance(node, dict | list):
            best = max(best, level)
            pending.extend(
                (c, level + 1) for c in (node.values() if isinstance(node, dict) else node)
            )
    return best


# ----- the two limits ----------------------------------------------------------------------


def test_the_documented_limits() -> None:
    assert canonical.MAX_INPUT_DEPTH == INPUT
    assert canonical.MAX_INTERNAL_DEPTH == INTERNAL
    doc = canonical.__doc__ or ""
    assert "64" in doc and "96" in doc


def test_loads_strict_admits_input_depth_and_loads_internal_the_ceiling() -> None:
    at_input, at_internal = json.dumps(nest(INPUT)), json.dumps(nest(INTERNAL))
    assert depth_of(loads_strict(at_input)) == INPUT
    with pytest.raises(canonical.JSONDepthError) as caught:
        loads_strict(at_internal)
    assert caught.value.limit == INPUT and not caught.value.internal
    assert str(caught.value) == "JSON nesting is deeper than 64 levels"
    assert depth_of(canonical.loads_internal(at_internal)) == INTERNAL
    with pytest.raises(canonical.JSONDepthError) as caught:
        canonical.loads_internal(json.dumps(nest(INTERNAL + 1)))
    assert caught.value.limit == INTERNAL and caught.value.internal
    assert "96" in str(caught.value)
    assert issubclass(canonical.JSONDepthError, CanonicalJSONError)


def test_dumps_canonical_holds_values_to_the_internal_ceiling_only() -> None:
    assert dumps_canonical(nest(INTERNAL))
    with pytest.raises(canonical.JSONDepthError) as caught:
        dumps_canonical(nest(INTERNAL + 1))
    assert caught.value.internal
    # An admitted value plus any plausible envelope round-trips through the encoder and the
    # internal reader: the margin between the limits is what makes that so.
    wrapped = {"items": [{"body": {"outputs": nest(INPUT)}}]}
    assert canonical.loads_internal(dumps_canonical(wrapped)) == wrapped


def test_json_object_types_split_caller_input_from_internal_values() -> None:
    from prometheos.domain.models import JsonObjectInput

    deep = nest(80)
    assert TypeAdapter(JsonObject).validate_python(deep) == deep  # internal: stored, wrapped
    with pytest.raises(ValidationError, match="deeper than 96 levels"):
        TypeAdapter(JsonObject).validate_python(nest(INTERNAL + 1))
    assert TypeAdapter(JsonObjectInput).validate_python(nest(INPUT))  # own depth 64
    with pytest.raises(ValidationError, match="deeper than 64 levels"):
        TypeAdapter(JsonObjectInput).validate_python(nest(INPUT + 1))


# ----- B2: the scan is linear --------------------------------------------------------------


def trailing_backslash_families(size: int) -> dict[str, str]:
    """Unterminated strings, most ending in a lone backslash, behind 65 openers (so the cheap
    opener count cannot skip the scan and the document is over the limit)."""
    backslash, quote = chr(92), chr(34)
    openers = "[" * 65
    return {
        "escaped-quotes-then-backslash": openers
        + quote
        + (backslash + quote) * (size // 2)
        + backslash,
        "alternating": openers + (quote + backslash) * (size // 2),
        "backslash-run": openers + quote + backslash * size,
        "plain": openers + quote + "a" * size,
    }


def test_the_scan_is_linear_on_an_unterminated_string_ending_in_a_backslash() -> None:
    """A string left open by a final backslash made the old pattern fail at the end and the
    scan restart at every later quote: 16 KB took 0.5 s, 64 KB 7 s, 1 MiB hours. The small
    size comes first, so a regression fails fast instead of hanging the suite."""
    for size in (64 * 1024, 1024 * 1024):
        for name, text in trailing_backslash_families(size).items():
            for scan in (canonical.check_text_depth, loads_strict):
                started = time.perf_counter()
                with pytest.raises(canonical.JSONDepthError):
                    scan(text)
                assert time.perf_counter() - started < FAST, (name, size, scan.__name__)


# ----- B1: an admitted value stays readable after the application wraps it -----------------


@pytest.fixture
def world(tmp_path: Path) -> tuple[Env, str, str]:
    env = Env(tmp_path)
    import_package(env.svc, write_package(tmp_path / "pkg-src", version="1.0.0"))
    target = tmp_path / "proj"
    target.mkdir()
    pkg = install(env.svc, "synthetic-evidence@1.0.0", "claude-code", target, env.project_id)
    package_id = str(pkg["record"]["results"][0]["id"])
    note = new_id("entry")
    env.apply([create_entry(note)])
    return env, package_id, note


def ready_run(env: Env, package_id: str, note: str) -> str:
    """A recipe run with every step resolved, ready to be finished."""
    started = env.svc.workflow_start(
        env.project_id,
        package_id=package_id,
        recipe_id="evidence-pass",
        inputs={"question": "Ready?"},
        request_id=new_request_id(),
    )
    run_id = str(started["results"][0]["id"])

    def step(step_id: str, status: str, **extra: Any) -> None:
        run = env.svc.get(env.project_id, OWNER, run_id, None)
        env.svc.workflow_step(
            env.project_id,
            run_id=run_id,
            base_rev=run["rev"],
            step_id=step_id,
            status=status,
            artifact_refs=extra.get("artifact_refs", []),
            check_results=extra.get("check_results", []),
            reason=extra.get("reason"),
            request_id=new_request_id(),
        )

    def passes(prefix: str, count: int) -> list[dict[str, Any]]:
        return [
            {"id": f"{prefix}.{i}", "result": "pass", "evidence": []} for i in range(1, count + 1)
        ]

    step("optional-plot", "skipped", reason="no plotting needed")
    step("gather", "done", check_results=passes("gather", 1))
    step(
        "record",
        "done",
        artifact_refs=[{"id": note, "rev": 1}],
        check_results=passes("record", 2),
    )
    return run_id


def mcp_call(env: Env, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    from prometheos.mcp_adapter import build_server

    async def go() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await client.call_tool(name, arguments)

    result = anyio.run(go)
    return bool(result.is_error), result.content[0].text if result.content else ""


DEPTH_MESSAGE = "not valid JSON: JSON nesting is deeper than 64 levels"


def mcp_error(text: str) -> dict[str, Any]:
    """The PrometheOS error object inside an MCP tool error (the SDK's own argument-validation
    text has no such object, so this raises for it)."""
    error: dict[str, Any] = json.loads(text[text.index("{") :])["error"]
    return error


def depth_error_envelope() -> dict[str, Any]:
    """What every transport answers for outside input nested past the input limit."""
    return {"code": "validation_error", "message": DEPTH_MESSAGE, "retryable": False, "details": {}}


def read_over_http(hub: Hub, run_id: str) -> None:
    for path in (
        f"/api/v1/records/{run_id}",
        "/api/v1/records?kind=run",
        "/api/v1/records",
        f"/api/v1/records/{run_id}/history",
        f"/api/v1/workflows/{run_id}",
        f"/api/v1/workflows/{run_id}/export",
        "/api/v1/context",
    ):
        reply = hub.request("GET", path)
        assert reply.status == 200, (path, reply.status, reply.body[:200])


def read_over_mcp(env: Env, run_id: str) -> None:
    for tool, arguments in (
        ("record_get", {"record_id": run_id}),
        ("record_list", {"kind": "run"}),
        ("workflow_status", {"run_id": run_id}),
    ):
        failed, text = mcp_call(env, tool, arguments)
        assert not failed, (tool, text[:300])


def read_over_cli(tmp_path: Path, run_id: str) -> None:
    cli = Cli(tmp_path)
    exported = tmp_path / f"{run_id}.export"
    for args in (
        ("record", "get", run_id),
        ("record", "list", "--kind", "run"),
        ("record", "history", run_id),
        ("workflow", "status", run_id),
        ("workflow", "export", run_id, "--out", str(exported)),
    ):
        code, document, stderr = cli(*args)
        assert code == 0 and "error" not in document, (args, document, stderr[-300:])


def test_the_largest_admissible_hub_body_is_readable_through_every_surface(
    world: tuple[Env, str, str], tmp_path: Path
) -> None:
    env, package_id, note = world
    run_id = ready_run(env, package_id, note)
    rev = env.svc.get(env.project_id, OWNER, run_id, None)["rev"]

    def body(levels: int) -> dict[str, Any]:
        outputs = {"entry_id": note, "x": nest(levels)}
        return {
            "base_rev": rev,
            "outcome": "completed",
            "request_id": new_request_id(),
            "outputs": outputs,
        }

    assert depth_of(body(62)) == INPUT  # the body, as the hub guard counts it
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        over = hub.request("POST", f"/api/v1/workflows/{run_id}/finish", body(63))
        assert over.status == 400 and over.json()["error"]["code"] == "validation_error"
        assert "deeper than 64" in over.json()["error"]["message"]
        assert env.svc.get(env.project_id, OWNER, run_id, None)["rev"] == rev  # nothing committed
        done = hub.request("POST", f"/api/v1/workflows/{run_id}/finish", body(62))
        assert done.status == 200, done.body[:300]
        stored = env.svc.get(env.project_id, OWNER, run_id, None)
        assert stored["body"]["status"] == "completed" and depth_of(stored["body"]) == INPUT
        read_over_http(hub, run_id)
        # The agent view of the same record reads too.
        assert dumps_canonical(env.svc.get(env.project_id, AGENT, run_id, None))
    read_over_mcp(env, run_id)
    read_over_cli(tmp_path, run_id)


def test_the_largest_admissible_mcp_argument_is_readable_through_every_surface(
    world: tuple[Env, str, str], tmp_path: Path
) -> None:
    env, package_id, note = world
    run_id = ready_run(env, package_id, note)
    rev = env.svc.get(env.project_id, OWNER, run_id, None)["rev"]

    def finish(levels: int) -> tuple[bool, str]:
        outputs = {"entry_id": note, "x": nest(levels)}
        return mcp_call(
            env,
            "workflow_finish",
            {
                "run_id": run_id,
                "base_rev": rev,
                "outcome": "completed",
                "request_id": new_request_id(),
                "outputs": outputs,
            },
        )

    failed, text = finish(INPUT)  # the argument is 65 levels deep
    assert failed and mcp_error(text) == depth_error_envelope(), text[:300]
    assert env.svc.get(env.project_id, OWNER, run_id, None)["rev"] == rev  # nothing committed
    failed, text = finish(INPUT - 1)  # exactly 64
    assert not failed, text[:300]
    stored = env.svc.get(env.project_id, OWNER, run_id, None)
    assert stored["body"]["status"] == "completed" and depth_of(stored["body"]) == INPUT + 1
    read_over_mcp(env, run_id)
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        read_over_http(hub, run_id)
    read_over_cli(tmp_path, run_id)


def test_a_row_stored_deeper_than_the_input_limit_stays_readable(
    world: tuple[Env, str, str], tmp_path: Path
) -> None:
    """Rows written before the limit existed (the service layer admits up to the internal
    ceiling) are still read: stored rows and envelopes use the internal ceiling."""
    env, package_id, note = world
    run_id = ready_run(env, package_id, note)
    rev = env.svc.get(env.project_id, OWNER, run_id, None)["rev"]
    env.svc.workflow_finish(
        env.project_id,
        run_id=run_id,
        base_rev=rev,
        outcome="completed",
        outputs={"entry_id": note, "x": nest(80)},
        request_id=new_request_id(),
    )
    stored = env.svc.get(env.project_id, OWNER, run_id, None)
    assert depth_of(stored["body"]) > INPUT + 10
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        read_over_http(hub, run_id)
    read_over_mcp(env, run_id)
    read_over_cli(tmp_path, run_id)


# ----- L2: an internal overflow is a deterministic, non-retryable failure ------------------


def internal_overflow() -> canonical.JSONDepthError:
    try:
        dumps_canonical(nest(INTERNAL + 1))
    except canonical.JSONDepthError as exc:
        return exc
    raise AssertionError("no overflow")


def test_hub_reports_an_internal_overflow_as_non_retryable(
    env_hub: tuple[Env, Hub], monkeypatch: pytest.MonkeyPatch
) -> None:
    env, hub = env_hub

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise internal_overflow()

    monkeypatch.setattr(env.svc, "get", boom)
    reply = hub.request("GET", f"/api/v1/records/{new_id('entry')}")
    error = reply.json()["error"]
    assert reply.status == 422 and error["code"] == "budget_exceeded", reply.body[:300]
    assert error["retryable"] is False
    assert "96" in error["message"] and "not valid JSON" not in error["message"]


@pytest.fixture
def env_hub(tmp_path: Path) -> Iterator[tuple[Env, Hub]]:
    env = Env(tmp_path)
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        yield env, hub


def test_mcp_reports_an_internal_overflow_as_non_retryable(tmp_path: Path) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    from prometheos.app.pathscrub import build_redactor
    from prometheos.mcp_adapter import call_with
    from prometheos.mcp_schemas import Out

    env = Env(tmp_path)
    for redactor in (None, build_redactor(env.svc, env.project_id)):

        def boom() -> Any:
            raise internal_overflow()

        with pytest.raises(ToolError) as caught:
            call_with(boom, Out, redactor)
        text = str(caught.value)
        error = json.loads(text[text.index("{") :])["error"]
        assert error["code"] == "budget_exceeded" and error["retryable"] is False, error
        assert "not valid JSON" not in error["message"]
        # A caller's over-deep input is still the validation error.
        with pytest.raises(ToolError) as caught:
            call_with(lambda: loads_strict(json.dumps(nest(INPUT + 1))), Out, redactor)
        text = str(caught.value)
        assert json.loads(text[text.index("{") :])["error"]["code"] == "validation_error"


# ----- parity: one typed error for an over-deep argument on every transport ----------------


def test_every_transport_answers_an_over_deep_argument_with_the_same_typed_error(
    world: tuple[Env, str, str], tmp_path: Path
) -> None:
    """The MCP SDK would answer a value error on a permissive argument type in its own words;
    the depth check runs inside the tool, so the reply is the PrometheOS envelope: the same
    code and message as the hub's body guard and the CLI's file reader."""
    env, package_id, _ = world
    deep = nest(70)
    for tool, arguments in (
        (
            "workflow_render",
            {"package": "synthetic-evidence", "recipe_id": "evidence-pass", "inputs": deep},
        ),
        (
            "workflow_start",
            {
                "package_id": package_id,
                "recipe_id": "evidence-pass",
                "inputs": deep,
                "request_id": new_request_id(),
            },
        ),
        ("provider_configure", {"config": deep, "revision": 0}),
    ):
        failed, text = mcp_call(env, tool, arguments)
        assert failed and mcp_error(text) == depth_error_envelope(), (tool, text[:300])
    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        body = {"package": "synthetic-evidence", "recipe_id": "evidence-pass", "inputs": deep}
        reply = hub.request("POST", "/api/v1/workflows/render", body)
    assert reply.status == 400
    assert {k: reply.json()["error"][k] for k in ("code", "message")} == {
        "code": "validation_error",
        "message": DEPTH_MESSAGE,
    }
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(deep), encoding="utf-8")
    code, document, _ = Cli(tmp_path)(
        "workflow", "render", "synthetic-evidence", "evidence-pass", "--inputs", str(inputs)
    )
    assert code != 0
    assert {k: document["error"][k] for k in ("code", "message")} == {
        "code": "validation_error",
        "message": DEPTH_MESSAGE,
    }


def test_mcp_workflow_step_lists_are_admitted_at_the_input_limit(
    world: tuple[Env, str, str],
) -> None:
    """An entry nested past the input limit but inside the internal one used to reach the
    encoder and be reported as the application's own ceiling (budget_exceeded)."""
    env, package_id, note = world
    started = env.svc.workflow_start(
        env.project_id,
        package_id=package_id,
        recipe_id="evidence-pass",
        inputs={"question": "Ready?"},
        request_id=new_request_id(),
    )
    run_id = str(started["results"][0]["id"])
    rev = env.svc.get(env.project_id, OWNER, run_id, None)["rev"]

    def step(field: str, value: Any) -> tuple[bool, str]:
        arguments = {
            "run_id": run_id,
            "base_rev": rev,
            "step_id": "gather",
            "status": "done",
            "request_id": new_request_id(),
            field: value,
        }
        return mcp_call(env, "workflow_step", arguments)

    refs = {"id": note, "rev": 1}
    result = {"id": "gather.1", "result": "pass", "evidence": []}
    for field, entry in (("artifact_refs", refs), ("check_results", result)):
        deep = dict(entry, x=nest(93))  # the entry is 94 levels deep
        failed, text = step(field, [deep])
        assert failed and mcp_error(text) == depth_error_envelope(), (field, text[:300])
        failed, text = step(field, [dict(entry, x=nest(10))])  # not refused for depth
        assert "deeper than" not in text, (field, text[:300])
    assert env.svc.get(env.project_id, OWNER, run_id, None)["rev"] == rev  # nothing committed


# ----- M2: researchers' own JSON sources ---------------------------------------------------


def test_a_retained_json_source_deeper_than_the_input_limit_can_be_cited(tmp_path: Path) -> None:
    env = Env(tmp_path)
    leaf = {"value": "2.40 kJ"}
    document = nest(79, leaf)  # 80 levels
    (env.research / "tree.json").write_text(json.dumps(document), encoding="utf-8")
    (env.research / "toodeep.json").write_text(
        json.dumps(nest(INTERNAL + 1, leaf)), encoding="utf-8"
    )
    pointer = "/k" * 79 + "/value"
    item = env.import_files(["tree.json"])["plan"]["items"][0]
    ref = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "json", "pointer": pointer},
        "quote_sha256": None,
    }
    env.apply([create_entry(evidence=[ref])])  # cited by pointer
    quantity = {
        "decimal": "2.40",
        "unit": "kJ",
        "source_ref": ref,
        "source_pointer": f"json:{pointer}",
    }
    env.apply([create_entry(quantities=[quantity])])  # and bound as a structured quantity
    # Beyond the internal ceiling the refusal says why, in the application's own words.
    item = env.import_files(["toodeep.json"])["plan"]["items"][0]
    deep_pointer = "/k" * (INTERNAL + 1) + "/value"
    ref = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "json", "pointer": deep_pointer},
        "quote_sha256": None,
    }
    with pytest.raises(PrometheosError) as caught:
        env.apply([create_entry(evidence=[ref])])
    assert caught.value.code == "validation_error"
    assert "nested" in caught.value.message and "96" in caught.value.message
    bound = {
        "decimal": "2.40",
        "unit": "kJ",
        "source_ref": ref,
        "source_pointer": f"json:{deep_pointer}",
    }
    with pytest.raises(PrometheosError) as caught:
        env.apply([create_entry(quantities=[bound])])
    assert "nested" in caught.value.message and "96" in caught.value.message


# ----- M1: a bounded request body, read only where a route takes one -----------------------


def raw_request(
    hub: Hub,
    method: str,
    path: str,
    *,
    body: Any = b"",
    declared: int | None = None,
    chunks: list[bytes] | None = None,
    authed: bool = True,
    timeout: float = 20,
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=timeout)
    headers = {
        "Host": f"127.0.0.1:{hub.port}",
        "Origin": hub.origin,
        "Content-Type": "application/json",
    }
    if authed:
        headers["Cookie"] = f"{SESSION_COOKIE}={hub.cookie}"
        headers[CSRF_HEADER] = str(hub.csrf)
    if declared is not None:
        headers["Content-Length"] = str(declared)
    try:
        if chunks is not None:
            conn.request(method, path, body=iter(chunks), headers=headers, encode_chunked=True)
        else:
            conn.request(method, path, body=body, headers=headers)
        reply = conn.getresponse()
        payload = reply.read()
        try:
            return reply.status, json.loads(payload)
        except ValueError:
            return reply.status, {"raw": payload[:200].decode("utf-8", "replace")}
    finally:
        conn.close()


def test_an_oversized_declared_body_is_refused_before_it_is_read(
    env_hub: tuple[Env, Hub],
) -> None:
    _, hub = env_hub
    # Only a few bytes are sent; a server that waited for the declared length would time out.
    status, document = raw_request(
        hub, "POST", "/api/v1/pair", body=b'{"token":"x"}', declared=10 * 1024 * 1024,
        authed=False, timeout=10,
    )  # fmt: skip
    assert status == 413 and document["error"]["code"] == "budget_exceeded", document
    assert document["error"]["retryable"] is False
    status, document = raw_request(
        hub, "POST", "/api/v1/transactions", body=b"{}", declared=64 * 1024 * 1024, timeout=10
    )
    assert status == 413 and document["error"]["code"] == "budget_exceeded", document


def test_a_chunked_body_is_capped_as_it_streams(env_hub: tuple[Env, Hub]) -> None:
    _, hub = env_hub
    status, document = raw_request(
        hub, "POST", "/api/v1/pair", chunks=[b"x" * 1024] * 16, authed=False
    )
    assert status == 413 and document["error"]["code"] == "budget_exceeded", document
    small = [b'{"token":', b'"wrong"}']
    status, document = raw_request(hub, "POST", "/api/v1/pair", chunks=small, authed=False)
    assert status == 403 and document["error"]["code"] == "policy_denied", document
    status, document = raw_request(
        hub, "POST", "/api/v1/transactions", chunks=[b"x" * (1 << 20)] * 10
    )
    assert status == 413, document  # 10 MiB: over the 8 MiB request limit plus slack


def test_the_request_limit_still_belongs_to_the_application(env_hub: tuple[Env, Hub]) -> None:
    """A body over 8 MiB but inside the hub's slack reaches the application, which answers
    with its own typed refusal, as it did before the hub cap existed."""
    _, hub = env_hub
    body = b" " * (8 * 1024 * 1024 + 1024)
    status, document = raw_request(hub, "POST", "/api/v1/transactions", body=body)
    assert status == 422 and document["error"]["code"] == "budget_exceeded", document
    assert "8 MiB" in document["error"]["message"]


def test_a_route_with_a_larger_legitimate_body_has_its_own_cap(env_hub: tuple[Env, Hub]) -> None:
    """Workspace text is up to 2 Mi characters; JSON-escaped astral characters take 12 bytes
    each, so that one route is allowed a body of about 24 MiB. The default cap is 8 MiB."""
    _, hub = env_hub
    nine_mib = b'{"path":"main.tex","text":"' + b"a" * (9 * 1024 * 1024) + b'","base_sha256":null}'
    status, document = raw_request(
        hub, "PUT", f"/api/v1/papers/{new_id('paper')}/workspace/file", body=nine_mib
    )
    assert status == 400 and document["error"]["code"] == "validation_error", document
    status, document = raw_request(hub, "POST", "/api/v1/transactions", body=nine_mib)
    assert status == 413, document


def test_a_body_is_not_read_where_no_route_takes_one(env_hub: tuple[Env, Hub]) -> None:
    _, hub = env_hub
    # GET-only paths: the router answers 405 without the hub ever reading the body.
    for authed in (False, True):
        status, _ = raw_request(
            hub, "POST", "/api/v1/session", body=b"x", declared=100 * 1024 * 1024,
            authed=authed, timeout=10,
        )  # fmt: skip
        assert status == 405
    # A POST route that takes no body at all.
    status, document = raw_request(
        hub, "POST", "/api/v1/approvals/approval-none/withdraw", body=b"x",
        declared=100 * 1024 * 1024, timeout=10,
    )  # fmt: skip
    assert status != 413, document


def test_the_body_guard_still_replays_a_body_to_its_route(
    world: tuple[Env, str, str],
) -> None:
    env, _, _ = world
    from cp1_support import request as request_bytes

    with running_hub(env.svc, env.project_id) as hub:
        hub.pair()
        raw = request_bytes(env.project_id, [create_entry()], env.seq())
        assert raw_request(hub, "POST", "/api/v1/transactions", body=raw)[0] == 200
        raw = request_bytes(env.project_id, [create_entry()], env.seq())
        halves = [raw[: len(raw) // 2], raw[len(raw) // 2 :]]
        assert raw_request(hub, "POST", "/api/v1/transactions", chunks=halves)[0] == 200
