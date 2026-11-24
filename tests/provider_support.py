"""Helpers for CP6.3 tests: a scripted transport, an in-memory credential store, a configured
project with a captured meeting. No network, no real credential."""

from __future__ import annotations

import json
from typing import Any

from cp1_support import AGENT, FIXTURES, OWNER, Env

from prometheos.providers import transport as tp
from prometheos.providers.secrets import MemorySecretStore

SECRET = "sk-CANARY-SECRET-9d41c7"
REF = "provider-key"
ENDPOINT = "https://api.example.test/v1"
LOCAL = "http://127.0.0.1:11434/v1"


class FakeTransport:
    """Replays scripted replies (a Reply, or an exception instance to raise); records every
    request as (url, headers, parsed body)."""

    def __init__(self, *script: tp.Reply | Exception) -> None:
        self.script = list(script)
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> tp.Reply:
        self.requests.append((url, dict(headers), json.loads(body)))
        if not self.script:
            raise AssertionError("the provider was called more often than the test scripted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def completion(
    content: Any,
    *,
    usage: tuple[int, int] | None = (1000, 200),
    finish: str = "stop",
    model: str = "test-model-2026",
) -> tp.Reply:
    text = content if isinstance(content, str) else json.dumps(content)
    document: dict[str, Any] = {
        "id": "chatcmpl-test-1",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": finish}],
    }  # fmt: skip
    if usage is not None:
        document["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1]}
    return tp.Reply(200, json.dumps(document).encode())


def config_doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "policy": "remote-allowed",
        "endpoint": ENDPOINT,
        "model": "test-model",
        "credential_ref": REF,
        "price_input_per_mtok_usd": "3.00",
        "price_output_per_mtok_usd": "15.00",
        "budget": {
            "max_input_chars": 60000,
            "max_output_tokens": 1024,
            "max_calls_per_run": 2,
            "spend_cap_usd": "1.00",
            "timeout_seconds": 30,
        },
    }
    budget = overrides.pop("budget", None)
    doc.update(overrides)
    if budget:
        doc["budget"] = {**doc["budget"], **budget}
    return doc


class ProviderEnv(Env):
    def __init__(self, tmp: Any) -> None:
        super().__init__(tmp)
        self.secrets = MemorySecretStore()
        self.svc.secret_store = self.secrets
        self.transport = FakeTransport()
        self.svc.provider_transport = self.transport
        meeting = self.svc.capture_meeting(
            self.project_id,
            title="Supervision meeting",
            text=(FIXTURES / "meeting.txt").read_text(encoding="utf-8"),
            held_at="2026-09-28T10:00:00Z",
        )
        self.meeting = meeting

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.store_dir / "record.sqlite")) as conn:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]

    def script(self, *steps: tp.Reply | Exception) -> FakeTransport:
        self.transport.script = list(steps)
        self.transport.requests.clear()
        return self.transport

    def revision(self) -> int:
        return int(self.svc.provider_status(self.project_id, AGENT)["revision"])

    def configure(self, doc: dict[str, Any] | None = None, *, store_secret: bool = True) -> dict:
        doc = doc if doc is not None else config_doc()
        base = self.revision()
        shown = self.svc.provider_configure_preview(self.project_id, doc, base)
        grant = self.svc.grant(
            self.project_id,
            operation="provider_configure",
            digest=shown["digest"],
            label="SYNTHETIC-TEST",
            synthetic=True,
        )
        result = self.svc.provider_configure(self.project_id, doc, base, grant.secret)
        if store_secret and doc.get("credential_ref"):
            digest = self.svc.provider_secret_digest("set", doc["credential_ref"])
            cap = self.svc.grant(
                self.project_id, operation="provider_configure", digest=digest,
                label="SYNTHETIC-TEST", synthetic=True,
            )  # fmt: skip
            self.svc.provider_secret_set(self.project_id, doc["credential_ref"], SECRET, cap.secret)
        return result

    def ref(self, line: int) -> dict[str, Any]:
        return {
            "source_id": self.meeting["source_id"],
            "version_id": self.meeting["version_id"],
            "locator": {"type": "text", "start_line": line, "end_line": line},
            "quote_sha256": None,
        }

    def items_reply(self, *lines: int, text: str = "Compare the controllers.") -> Any:
        return {
            "purpose": "meeting_extract",
            "items": [
                {
                    "type": "suggestion",
                    "text": f"{text} ({line})",
                    "evidence": [self.ref(line)],
                    "proposed_owner": None,
                    "due_at": None,
                }
                for line in lines
            ],
        }

    def preview(self, caller: Any = AGENT, **kwargs: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "purpose": "meeting_extract",
            "meeting_id": self.meeting["meeting_id"],
        }
        values.update(kwargs)
        return self.svc.provider_preview(self.project_id, caller, **values)

    def run(self, shown: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Owner approves exactly the previewed digest, then runs."""
        values: dict[str, Any] = {
            "purpose": "meeting_extract",
            "meeting_id": self.meeting["meeting_id"],
            "context_budget": 12000,
            "include_restricted": False,
        }
        values.update(kwargs)
        preview_args = {
            k: v for k, v in values.items() if k in ("purpose", "meeting_id", "include_restricted")
        }
        preview_args["context_budget"] = values["context_budget"]
        shown = shown or self.svc.provider_preview(
            self.project_id, OWNER if values["include_restricted"] else AGENT, **preview_args
        )
        grant = self.svc.grant(
            self.project_id, operation="provider_run", digest=shown["digest"],
            label="SYNTHETIC-TEST", synthetic=True,
        )  # fmt: skip
        egress = None
        if shown["needs_egress_grant"]:
            egress = self.svc.grant(
                self.project_id, operation="provider_egress",
                digest=shown["transmits"]["payload_sha256"], label="SYNTHETIC-TEST", synthetic=True,
            ).secret  # fmt: skip
        return self.svc.provider_run(
            self.project_id,
            approved_digest=shown["digest"],
            capability=grant.secret,
            egress_capability=egress,
            **values,
        )
