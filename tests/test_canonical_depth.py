"""JSON nesting is bounded (decision 0029).

``loads_strict`` and ``dumps_canonical`` are the single JSON gate of every transport. Python's
own ``json`` and the old recursive value walk raised an untyped ``RecursionError`` on a deeply
nested document; now a fixed depth limit is checked in linear time before anything recurses,
and every surface answers with its existing typed validation error.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import Env, create_entry
from cp1_support import request as request_bytes
from hub_support import Hub, running_hub
from mcp import Client
from test_cp1_cli import Cli

from prometheos import agentops
from prometheos.domain import canonical
from prometheos.domain.canonical import (
    CanonicalJSONError,
    canonical_sha256,
    dumps_canonical,
    loads_strict,
)
from prometheos.domain.models import PrometheosError

LIMIT = (
    64  # the documented input limit (decision 0029); test_the_documented_limit pins the constant
)
INTERNAL = 96  # the internal ceiling dumps_canonical holds values to
HUGE = 100_000  # far beyond the interpreter's recursion limit
FAST = 5.0  # seconds; the real cost is milliseconds, the bound only catches quadratic scans


def arrays(depth: int) -> str:
    return "[" * depth + "]" * depth


def objects(depth: int) -> str:
    return '{"a":' * depth + "null" + "}" * depth


def mixed(depth: int) -> str:
    return '[{"a":' * (depth // 2) + "1" + "}]" * (depth // 2)


def nested_list(depth: int) -> list[Any]:
    value: list[Any] = []
    for _ in range(depth - 1):
        value = [value]
    return value


def nested_dict(depth: int) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(depth - 1):
        value = {"a": value}
    return value


def refused_quickly(call: Callable[[], Any]) -> None:
    """``call`` raises the typed refusal (never ``RecursionError``) within a bounded time."""
    started = time.perf_counter()
    with pytest.raises(CanonicalJSONError):
        call()
    assert time.perf_counter() - started < FAST


# ----- the limit ---------------------------------------------------------------------------


def test_the_documented_limit() -> None:
    assert canonical.MAX_INPUT_DEPTH == LIMIT
    assert "64" in (canonical.__doc__ or "")


def test_the_limit_is_comfortably_above_every_generated_document() -> None:
    """The deepest committed or generated schema is 22 levels (the MCP tool inputs)."""
    from prometheos import schemas
    from prometheos.registry import inventory

    def depth(value: Any) -> int:
        best, pending = 0, [(value, 1)]
        while pending:
            node, level = pending.pop()
            if isinstance(node, dict | list):
                best = max(best, level)
                children = node.values() if isinstance(node, dict) else node
                pending.extend((child, level + 1) for child in children)
        return best

    deepest = max(depth(schemas.schema_for(name)) for name in schemas.REGISTRY)
    _, tools = inventory.mcp_entries()
    deepest = max(deepest, *(depth(t["input"]) for t in tools.values() if "input" in t))
    assert 0 < deepest <= LIMIT // 2


# ----- decoding ----------------------------------------------------------------------------


@pytest.mark.parametrize("shape", [arrays, objects, mixed])
def test_a_document_at_the_limit_is_accepted(shape: Callable[[int], str]) -> None:
    text = shape(LIMIT)
    value = loads_strict(text)
    assert loads_strict(text.encode("utf-8")) == value
    assert loads_strict(dumps_canonical(value)) == value  # the encoder agrees with the decoder


@pytest.mark.parametrize("shape", [arrays, objects, mixed])
def test_one_level_over_the_limit_is_refused(shape: Callable[[int], str]) -> None:
    with pytest.raises(CanonicalJSONError, match=f"deeper than {LIMIT} levels"):
        loads_strict(shape(LIMIT + 2))
    with pytest.raises(CanonicalJSONError, match=f"deeper than {LIMIT} levels"):
        loads_strict(shape(LIMIT + 2).encode("utf-8"))


def test_the_exact_boundary_for_arrays() -> None:
    assert loads_strict(arrays(LIMIT)) is not None
    with pytest.raises(CanonicalJSONError, match="deeper than"):
        loads_strict(arrays(LIMIT + 1))


PATHOLOGICAL = {
    "arrays": arrays(HUGE),
    "objects": objects(HUGE),
    "mixed": mixed(HUGE),
    "unclosed-arrays": "[" * HUGE,
    "unclosed-objects": "{" * HUGE,
    "unclosed-members": '{"a":' * HUGE,
    "unclosed-elements": "[1," * HUGE,
    "separated-openers": " ".join("[" * 1000 for _ in range(100)),
}


@pytest.mark.parametrize("text", PATHOLOGICAL.values(), ids=PATHOLOGICAL.keys())
def test_a_pathological_document_is_refused_quickly_without_recursion(text: str) -> None:
    refused_quickly(lambda: loads_strict(text))
    refused_quickly(lambda: loads_strict(text.encode("utf-8")))


def test_the_boundary_holds_in_a_wide_document() -> None:
    """Many shallow siblings keep the cheap opener count out of the way, so the scan itself
    must put the boundary exactly at the limit."""
    siblings = ",".join(["[]"] * 200)

    def wide(depth: int) -> str:
        return "[" + siblings + "," + arrays(depth - 1) + "]"

    assert loads_strict(wide(LIMIT))
    with pytest.raises(CanonicalJSONError, match="deeper than"):
        loads_strict(wide(LIMIT + 1))


def test_brackets_inside_strings_do_not_count() -> None:
    assert loads_strict('["' + "[" * 5000 + '"]') == ["[" * 5000]
    assert loads_strict('{"' + "{" * 5000 + '": "' + "]" * 5000 + '"}') == {"{" * 5000: "]" * 5000}
    # An escaped quote does not end the string, so the brackets after it are still text.
    backslash = chr(92)
    text = '["a' + backslash + '"' + "[" * 5000 + '"]'
    assert loads_strict(text) == ['a"' + "[" * 5000]
    # An escaped backslash does not escape the quote after it: the string ends, and the
    # brackets that follow are structure.
    ended = '["a' + backslash * 2 + '", ' + "[" * (LIMIT - 1) + "]" * (LIMIT - 1) + "]"
    assert loads_strict(ended)[0] == "a" + backslash
    with pytest.raises(CanonicalJSONError, match="deeper than"):
        loads_strict('["a' + backslash * 2 + '", ' + "[" * LIMIT + "]" * LIMIT + "]")
    # Four backslashes then a quote: still an even run, the string ends.
    with pytest.raises(CanonicalJSONError, match="deeper than"):
        loads_strict('["a' + backslash * 4 + '", ' + "[" * LIMIT + "]" * LIMIT + "]")
    # Three backslashes then a quote: odd run, the quote is escaped and the brackets are text.
    odd = '["a' + backslash * 3 + '"' + "[" * 5000 + '"]'
    assert loads_strict(odd) == ["a" + backslash + '"' + "[" * 5000]


def test_the_scan_agrees_with_the_true_depth_of_random_documents() -> None:
    """Differential check against a value-level depth: any document json.dumps writes (with
    awkward string content) passes at exactly its depth and fails one level below it."""
    import random

    rng = random.Random(20261009)
    backslash, quote = chr(92), chr(34)
    newline, tab = chr(10), chr(9)
    alphabet = [*"[]{},: /", newline, tab, quote, backslash, backslash + quote, backslash * 2]
    alphabet += [chr(0x2028), chr(0xE9), chr(0x1F600)]

    def text() -> str:
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))

    def build(level: int) -> Any:
        kind = rng.random()
        if level >= 12 or kind < 0.3:
            return rng.choice([None, True, 1, -2.5, text()])
        if kind < 0.65:
            return [build(level + 1) for _ in range(rng.randint(0, 3))]
        return {text(): build(level + 1) for _ in range(rng.randint(0, 3))}

    def true_depth(value: Any) -> int:
        best, pending = 0, [(value, 1)]
        while pending:
            node, level = pending.pop()
            if isinstance(node, dict | list):
                best = max(best, level)
                pending.extend(
                    (c, level + 1) for c in (node.values() if isinstance(node, dict) else node)
                )
        return best

    checked = 0
    for _ in range(400):
        value = build(0)
        depth = true_depth(value)
        for options in ({}, {"indent": 2}, {"ensure_ascii": False}, {"separators": (",", ":")}):
            rendered = json.dumps(value, **options)
            canonical.check_text_depth(rendered, max(depth, 1))
            canonical.check_text_depth(rendered.encode("utf-8"), max(depth, 1))
            if depth >= 1:
                with pytest.raises(CanonicalJSONError):
                    canonical.check_text_depth(rendered, depth - 1)
                checked += 1
    assert checked > 400


def test_an_unterminated_string_cannot_hide_depth_or_run_quadratic() -> None:
    backslash = chr(92)
    for text in ('["' + "[" * HUGE, '["' + (backslash + '"') * HUGE, '"' * HUGE + "[" * HUGE):
        refused_quickly(lambda t=text: loads_strict(t))


def test_invalid_json_keeps_its_own_message() -> None:
    with pytest.raises(CanonicalJSONError, match="invalid JSON"):
        loads_strict('{"x":')
    with pytest.raises(CanonicalJSONError, match="duplicate object key"):
        loads_strict('{"a":1,"a":2}')


def test_a_recursion_error_inside_the_parser_is_still_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: with the text scan out of the way, the interpreter's own
    ``RecursionError`` becomes a typed refusal."""
    monkeypatch.setattr(canonical, "check_text_depth", lambda *a, **k: None)
    with pytest.raises(CanonicalJSONError, match="too deep"):
        loads_strict(arrays(HUGE))


def test_loads_bounded_keeps_lenient_json_but_bounds_depth() -> None:
    assert canonical.loads_bounded('{"a":1,"a":2}') == {"a": 2}  # as json.loads
    assert canonical.loads_bounded("NaN") != canonical.loads_bounded("NaN")  # as json.loads
    assert canonical.loads_bounded(arrays(LIMIT)) is not None
    for text in (arrays(LIMIT + 1), arrays(HUGE)):
        with pytest.raises(CanonicalJSONError, match="deeper than"):
            canonical.loads_bounded(text)


# ----- encoding ----------------------------------------------------------------------------


@pytest.mark.parametrize("build", [nested_list, nested_dict])
def test_encoding_at_the_limit_works_and_beyond_it_is_refused(
    build: Callable[[int], Any],
) -> None:
    assert dumps_canonical(build(INTERNAL))
    assert canonical_sha256(build(INTERNAL))
    with pytest.raises(CanonicalJSONError, match=f"deeper than {INTERNAL} levels"):
        dumps_canonical(build(INTERNAL + 1))
    with pytest.raises(CanonicalJSONError, match="deeper than"):
        canonical_sha256(build(INTERNAL + 1))
    refused_quickly(lambda: dumps_canonical(build(HUGE)))


def test_encoding_counts_tuples_and_a_cycle_is_refused() -> None:
    deep: Any = ()
    for _ in range(INTERNAL):
        deep = (deep,)
    with pytest.raises(CanonicalJSONError, match="deeper than"):
        dumps_canonical(deep)
    cycle: list[Any] = []
    cycle.append(cycle)
    with pytest.raises(CanonicalJSONError):
        dumps_canonical(cycle)


def test_encoding_still_rejects_what_it_always_rejected() -> None:
    with pytest.raises(CanonicalJSONError):
        dumps_canonical({"a": [float("nan")]})
    with pytest.raises(CanonicalJSONError):
        dumps_canonical([{"k": chr(0xD800)}])
    with pytest.raises(CanonicalJSONError):
        dumps_canonical({chr(0xDFFF): 1})
    assert dumps_canonical({"b": [1, {"c": "x"}], "a": None}) == b'{"a":null,"b":[1,{"c":"x"}]}'


def test_a_recursion_error_inside_the_encoder_is_still_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canonical, "_check_value", lambda *a, **k: None)
    with pytest.raises(CanonicalJSONError, match="too deep"):
        dumps_canonical(nested_list(HUGE))


# ----- the other entry points that read outside bytes --------------------------------------


def test_the_strict_entry_points_refuse_a_deep_document_with_their_typed_error(
    tmp_path: Path,
) -> None:
    from prometheos.app import apply, receipts
    from prometheos.app.approval_kinds import KINDS, parse_params
    from prometheos.audio.transcript import parse_payload
    from prometheos.domain.models import RecordRef, parse_model
    from prometheos.store import backup

    deep = arrays(HUGE).encode("utf-8")
    for call in (
        lambda: apply.parse_request(deep),
        lambda: receipts.parse(deep),
        lambda: parse_payload(deep),
        lambda: parse_params(KINDS["job_resolve"], deep),
    ):
        with pytest.raises(PrometheosError) as caught:
            call()
        assert caught.value.code == "validation_error"
    with pytest.raises(CanonicalJSONError, match="deeper than"):
        parse_model(RecordRef, deep)
    with pytest.raises(ValueError, match="deeper than"):  # its documented contract
        apply.parse_model_value(RecordRef, deep)
    archive = tmp_path / "deep.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("manifest.json", deep)
        handle.writestr(backup.DB_NAME, b"")
    with pytest.raises(PrometheosError) as caught:
        backup.verify_backup(archive, tmp_path)
    assert caught.value.code == "validation_error"
    assert "manifest" in caught.value.message


def test_a_project_local_host_config_that_is_too_deep_is_not_json(tmp_path: Path) -> None:
    from prometheos import skills

    config = tmp_path / "settings.json"
    config.write_text(arrays(HUGE), encoding="utf-8")
    with pytest.raises(PrometheosError) as caught:
        skills._load_json(config)
    assert caught.value.code == "validation_error" and "not JSON" in caught.value.message
    config.write_text('{"a":1,"a":2}', encoding="utf-8")  # leniency is unchanged
    assert skills._load_json(config) == {"a": 2}


def test_a_compile_log_header_that_is_too_deep_names_no_inputs() -> None:
    from prometheos.latex import diagnostics

    assert diagnostics.known_inputs(diagnostics.INPUTS_PREFIX + '["a.tex"]') == {"a.tex"}
    assert diagnostics.known_inputs(diagnostics.INPUTS_PREFIX + arrays(HUGE)) is None


def test_the_cli_request_peeks_survive_a_deep_file() -> None:
    from prometheos import cli

    deep = arrays(HUGE).encode("utf-8")
    assert cli._peek_project(deep) is None
    assert cli._ops_summary(deep) == "  (unparseable request)"
    assert cli._peek_project(b'{"project_id": "p1"}') == "p1"


# ----- transports --------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def hub(env: Env) -> Iterator[Hub]:
    with running_hub(env.svc, env.project_id) as running:
        running.pair()
        yield running


def refused(reply: Any) -> dict[str, Any]:
    assert reply.status == 400, (reply.status, reply.body[:300])
    error: dict[str, Any] = reply.json()["error"]
    assert error["code"] == "validation_error", error
    return error


@pytest.mark.parametrize("size", [LIMIT + 2, HUGE])
def test_hub_transaction_body_too_deep_is_a_validation_error(hub: Hub, size: int) -> None:
    error = refused(hub.request("POST", "/api/v1/transactions", arrays(size).encode()))
    assert "deeper than" in error["message"]


@pytest.mark.parametrize("size", [LIMIT + 2, HUGE])
def test_hub_agent_route_body_too_deep_is_a_validation_error(hub: Hub, size: int) -> None:
    body = ('{"inputs":' + arrays(size) + "}").encode()
    error = refused(hub.request("POST", "/api/v1/workflows/render", body))
    assert "deeper than" in error["message"]


@pytest.mark.parametrize("size", [LIMIT + 2, HUGE])
def test_hub_model_route_body_too_deep_is_a_validation_error(hub: Hub, size: int) -> None:
    """Routes whose body the framework parses itself (pydantic models) are guarded too:
    the framework's own JSON parser would otherwise recurse."""
    body = ('{"request":' + objects(size) + "}").encode()
    error = refused(hub.request("POST", "/api/v1/decisions/preview", body))
    assert "deeper than" in error["message"]
    pairing = hub.request(
        "POST", "/api/v1/pair", ('{"token":' + arrays(size) + "}").encode(), cookie=False
    )
    if size > 2048:  # the pairing body is capped at 4 KiB, which comes first
        assert pairing.status == 413
    else:
        assert "deeper than" in refused(pairing)["message"]


def test_hub_still_serves_ordinary_bodies_after_the_guard(env: Env, hub: Hub) -> None:
    """The guard reads the body before the route does; the route must still receive it."""
    wrong = hub.request("POST", "/api/v1/pair", {"token": "wrong"}, cookie=False)
    assert wrong.status == 403 and wrong.json()["error"]["code"] == "policy_denied"
    nested = {"request": {"schema_version": 1, "meta": nested_dict(LIMIT - 6)}}
    reply = hub.request("POST", "/api/v1/decisions/preview", nested)
    assert reply.status == 400 and "deeper than" not in reply.body.decode()  # a schema error
    assert reply.json()["error"]["code"] == "validation_error"
    document = json.loads(request_bytes(env.project_id, [create_entry()], env.seq()))
    served = hub.request("POST", "/api/v1/transactions", document)
    assert served.status == 200, served.body[:300]


def test_cli_json_argument_too_deep_is_a_validation_error(tmp_path: Path) -> None:
    Env(tmp_path)  # a project in the home the CLI uses
    cli = Cli(tmp_path)
    deep = tmp_path / "deep.json"
    deep.write_text(arrays(HUGE), encoding="utf-8")
    code, document, stderr = cli(
        "workflow", "render", "no-such-package", "recipe", "--inputs", str(deep)
    )
    assert code != 0 and document["error"]["code"] == "validation_error", document
    assert "deeper than" in document["error"]["message"]
    assert "Traceback" not in stderr and "RecursionError" not in stderr


def test_cli_apply_and_decision_preview_too_deep_are_validation_errors(tmp_path: Path) -> None:
    env = Env(tmp_path)
    cli = Cli(tmp_path)
    deep = tmp_path / "request.json"
    deep.write_text(arrays(HUGE), encoding="utf-8")
    for args in (
        ("apply", "--file", str(deep)),  # no --project: the file is peeked at first
        ("apply", "--file", str(deep), "--project", env.project_id),
        ("decision", "preview", "--file", str(deep)),
    ):
        code, document, stderr = cli(*args)
        assert code != 0 and document["error"]["code"] == "validation_error", (args, document)
        assert "deeper than" in document["error"]["message"], (args, document)
        assert "Traceback" not in stderr and "RecursionError" not in stderr, (args, stderr[-300:])


def test_cli_stdin_too_deep_is_a_validation_error(tmp_path: Path) -> None:
    Env(tmp_path)
    cli = Cli(tmp_path)
    code, document, stderr = cli(
        "workflow",
        "render",
        "no-such-package",
        "recipe",
        "--inputs",
        "-",
        stdin=objects(HUGE).encode(),
    )
    assert code != 0 and document["error"]["code"] == "validation_error", document
    assert "Traceback" not in stderr


def test_cli_module_never_exits_with_a_traceback_for_a_deep_file(tmp_path: Path) -> None:
    """Through the real entry point, not only the Cli helper: one JSON document on stdout."""
    Env(tmp_path)
    deep = tmp_path / "deep.json"
    deep.write_text(arrays(HUGE), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "prometheos", "apply", "--file", str(deep), "--json"],
        env=Cli(tmp_path).env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    lines = [x for x in proc.stdout.decode("utf-8").splitlines() if x.strip()]
    assert len(lines) == 1 and json.loads(lines[0])["error"]["code"] == "validation_error"
    assert b"Traceback" not in proc.stderr


def mcp_call(env: Env, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    from prometheos.mcp_adapter import build_server

    async def go() -> Any:
        async with Client(build_server(env.svc, env.project_id)) as client:
            return await client.call_tool(name, arguments)

    result = anyio.run(go)
    return bool(result.is_error), result.content[0].text if result.content else ""


def test_mcp_tool_argument_too_deep_is_refused_as_a_validation_error(env: Env) -> None:
    """The adapter never parses JSON text itself: the SDK has already built the arguments
    (pydantic-core bounds the JSON it parses at 200 levels and the request it serializes at
    about as many). A value between our limit and that reaches the typed parameter models,
    whose JSON-object fields apply the shared limit."""
    a = agentops.OPS["provider_configure_preview"]
    assert a.tool == "provider_read" and a.sub == "configure_preview"
    failed, text = mcp_call(
        env, a.tool, {"request": {"op": a.sub, "config": nested_dict(LIMIT + 16)}}
    )
    assert failed and "deeper than" in text and "Traceback" not in text, text[:400]
    # The same tool is served for a shallow configuration: the refusal is about depth only.
    failed, text = mcp_call(env, a.tool, {"request": {"op": a.sub, "config": nested_dict(10)}})
    assert "deeper than" not in text


def test_mcp_call_maps_a_json_depth_refusal_to_the_validation_error(env: Env) -> None:
    """Any service code that meets an over-deep value answers like every other bad
    request: the shared validation_error envelope, not an internal error."""
    from mcp.server.mcpserver.exceptions import ToolError

    from prometheos.app.pathscrub import build_redactor
    from prometheos.mcp_adapter import call_with
    from prometheos.mcp_schemas import Out

    for redactor in (None, build_redactor(env.svc, env.project_id)):
        with pytest.raises(ToolError) as caught:
            call_with(lambda: loads_strict(arrays(LIMIT + 1)), Out, redactor)
        text = str(caught.value)
        envelope = json.loads(text[text.index("{") :])["error"]
        assert envelope["code"] == "validation_error" and "deeper than" in envelope["message"]
