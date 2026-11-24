"""CP6.3 over the CLI and MCP with a real loopback model server: owner decisions need
capabilities for exact digests, the agent surface shows status and previews only."""

from __future__ import annotations

import http.server
import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from cp1_support import FIXTURES
from mcp import Client
from provider_support import completion, config_doc
from test_cp1_cli import Cli

from prometheos.app.service import Service
from prometheos.mcp_adapter import build_server


class Server:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: bytes = b""
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.send_header("Content-Length", str(len(outer.reply)))
                self.end_headers()
                self.wfile.write(outer.reply)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[Server]:
    running = Server()
    yield running
    running.close()


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    research = tmp_path / "research"
    research.mkdir()
    shutil.copyfile(FIXTURES / "meeting.txt", research / "meeting.txt")
    runner = Cli(tmp_path)
    code, init, err = runner(
        "project", "init", "--title", "Provider CLI", "--objective", "Undecided",
        "--root", f"research={research}",
    )  # fmt: skip
    assert code == 0, err
    runner.env["PROMETHEOS_PROJECT"] = init["project_id"]
    return runner


def ok(cli: Cli, *args: str) -> Any:
    code, out, err = cli(*args)
    assert code == 0, (args, out, err)
    return out


def grant(cli: Cli, tmp: Path, operation: str, digest: str, name: str) -> str:
    secret = (
        Service(home=tmp / "home")
        .grant(cli.env["PROMETHEOS_PROJECT"], operation=operation, digest=digest,
               label="SYNTHETIC-TEST cli", synthetic=True)
        .secret
    )  # fmt: skip
    path = tmp / name
    path.write_text(secret, encoding="utf-8")
    return str(path)


def meeting(cli: Cli, tmp: Path) -> dict[str, Any]:
    return ok(cli, "meeting", "capture", "--title", "Supervision", "--notes",
              str(tmp / "research" / "meeting.txt"))  # fmt: skip


def local_config(tmp: Path, endpoint: str) -> Path:
    doc = config_doc(
        policy="local-only", endpoint=endpoint, credential_ref=None,
        price_input_per_mtok_usd=None, price_output_per_mtok_usd=None,
        budget={"spend_cap_usd": None},
    )  # fmt: skip
    path = tmp / "provider.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_configure_preview_run_and_ledger_through_the_cli(
    cli: Cli, tmp_path: Path, server: Server
) -> None:
    captured = meeting(cli, tmp_path)
    status = ok(cli, "provider", "status")
    assert status["policy"] == "no-ai" and status["revision"] == 0
    code, denied, _ = cli("provider", "run", "--purpose", "meeting_extract", "--meeting",
                          captured["meeting_id"])  # fmt: skip
    assert code == 4 and denied["error"]["code"] == "policy_denied"  # no terminal, no capability

    path = local_config(tmp_path, server.endpoint)
    shown = ok(cli, "provider", "configure", "--file", str(path), "--preview")
    assert shown["route"] == "local" and shown["stale"] is False
    code, _refused, _ = cli("provider", "configure", "--file", str(path))
    assert code == 4  # an owner decision needs a terminal or a capability
    cap = grant(cli, tmp_path, "provider_configure", shown["digest"], "cfg.cap")
    done = ok(cli, "provider", "configure", "--file", str(path), "--capability-file", cap)
    assert done["revision"] == 1 and done["route"] == "local"
    status = ok(cli, "provider", "status")
    assert status["route"] == "local" and status["endpoint_host"] == "127.0.0.1"

    line = {
        "source_id": captured["source_id"], "version_id": captured["version_id"],
        "locator": {"type": "text", "start_line": 2, "end_line": 2}, "quote_sha256": None,
    }  # fmt: skip
    server.reply = completion(
        {"purpose": "meeting_extract",
         "items": [{"type": "suggestion", "text": "Compare with actuator limits.",
                    "evidence": [line], "proposed_owner": None, "due_at": None}]}
    ).body  # fmt: skip
    preview = ok(cli, "provider", "preview", "--purpose", "meeting_extract", "--meeting",
                 captured["meeting_id"])  # fmt: skip
    assert preview["would_send"] is True and preview["route"] == "local"
    assert server.requests == []  # a preview sends nothing
    run_cap = grant(cli, tmp_path, "provider_run", preview["digest"], "run.cap")
    code, wrong, _ = cli("provider", "run", "--purpose", "meeting_extract", "--meeting",
                         captured["meeting_id"], "--digest", "0" * 64,
                         "--capability-file", run_cap)  # fmt: skip
    assert code == 3 and wrong["error"]["code"] == "revision_conflict"  # not what was reviewed
    assert server.requests == []
    run_cap = grant(cli, tmp_path, "provider_run", preview["digest"], "run2.cap")
    result = ok(cli, "provider", "run", "--purpose", "meeting_extract", "--meeting",
                captured["meeting_id"], "--digest", preview["digest"],
                "--capability-file", run_cap)  # fmt: skip
    assert result["ok"] and result["route"] == "local" and len(server.requests) == 1
    calls = ok(cli, "provider", "calls")
    assert [c["state"] for c in calls["calls"]] == ["succeeded"]
    reconcile = ok(cli, "meeting", "reconcile", "--meeting", captured["meeting_id"])
    assert len(reconcile["items"]) == 1 and reconcile["items"][0]["status"] == "proposed"


def test_an_unreachable_provider_returns_the_fallback_and_exit_code(
    cli: Cli, tmp_path: Path
) -> None:
    captured = meeting(cli, tmp_path)
    path = local_config(tmp_path, "http://127.0.0.1:9/v1")  # nothing listens on port 9
    shown = ok(cli, "provider", "configure", "--file", str(path), "--preview")
    cap = grant(cli, tmp_path, "provider_configure", shown["digest"], "cfg.cap")
    ok(cli, "provider", "configure", "--file", str(path), "--capability-file", cap)
    preview = ok(cli, "provider", "preview", "--purpose", "meeting_extract", "--meeting",
                 captured["meeting_id"])  # fmt: skip
    run_cap = grant(cli, tmp_path, "provider_run", preview["digest"], "run.cap")
    code, out, _ = cli("provider", "run", "--purpose", "meeting_extract", "--meeting",
                       captured["meeting_id"], "--digest", preview["digest"],
                       "--capability-file", run_cap)  # fmt: skip
    assert code == 5 and out["error"]["code"] == "source_unavailable"
    details = out["error"]["details"]
    assert details["fallback"]["model_free"] is True and details["task_id"].startswith("task_")
    # The model-free path keeps working from the same CLI.
    assert ok(cli, "context", "show", "--budget", "2000")["text"]
    task = ok(cli, "task", "get", details["task_id"])
    assert task["task"]["body"]["status"] == "prepared"


def test_secret_set_needs_a_capability_and_never_prints_the_value(cli: Cli, tmp_path: Path) -> None:
    from provider_support import SECRET

    cli.env["PROMETHEOS_SECRET_VALUE"] = SECRET
    code, out, _ = cli("provider", "secret", "set", "--reference", "unit-test-key",
                       "--from-env", "PROMETHEOS_SECRET_VALUE")  # fmt: skip
    assert code == 4 and out["error"]["code"] == "policy_denied"
    assert SECRET not in json.dumps(out)


def test_mcp_offers_status_and_preview_but_no_way_to_run_configure_or_store(
    cli: Cli, tmp_path: Path
) -> None:
    svc = Service(home=tmp_path / "home", actor="mcp")
    project = cli.env["PROMETHEOS_PROJECT"]

    async def main() -> Any:
        async with Client(build_server(svc, project)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            status = await client.call_tool("provider_status", {})
            preview = await client.call_tool("provider_preview", {"purpose": "notebook_draft"})
            run = await client.call_tool(
                "provider_run",
                {"purpose": "notebook_draft", "digest": preview.structured_content["digest"]},
            )
            return names, status, preview, run

    names, status, preview, run = anyio.run(main)
    assert {"provider_status", "provider_preview"} <= names
    for forbidden in ("provider_secret_set", "provider_approve"):
        assert forbidden not in names
    # CP10.2 (decision 0016): run and configure exist only as approval requests; under the
    # no-ai policy the request itself is refused and nothing is sent or stored.
    assert {"provider_run", "provider_configure"} <= names
    assert run.is_error and "policy_denied" in run.content[0].text
    assert status.structured_content["policy"] == "no-ai"
    assert preview.structured_content["would_send"] is False
    assert "no-ai" in preview.structured_content["blockers"][0]
