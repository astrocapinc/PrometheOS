"""The one documented generation adapter: OpenAI-style Chat Completions with a JSON-schema
response format, against an explicitly configured endpoint and model.

Wire format: ``POST {endpoint}/chat/completions`` with ``model``, ``messages``,
``response_format = {type: json_schema, json_schema: {name, strict, schema}}``,
``max_tokens`` and ``temperature 0``; the reply is read from ``choices[0].message.content``
and ``usage``. This is *one* wire format. It is not claimed to work with every service that
uses the label "OpenAI-compatible"; the same adapter serves a local server on loopback and a
remote service because the route is decided by the configured endpoint, not by the adapter.

The adapter exposes capabilities. It supports generation only; asking it to embed, classify
or transcribe is an ``unsupported`` error that names the capability, not a silent no-op.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from prometheos.domain.canonical import canonical_sha256, loads_strict
from prometheos.domain.models import PrometheosError
from prometheos.providers import transport as tp
from prometheos.providers.config import ProviderConfig, host_of
from prometheos.providers.secrets import SecretStore

MAX_PROMPT_CHARS_HARD = 400_000


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True)
class GenerationRequest:
    system: str
    user: str
    schema: dict[str, Any]
    max_output_tokens: int
    timeout_seconds: int
    follow_up: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (role, content)


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model_id: str
    usage: Usage
    finish_reason: str | None
    receipt: dict[str, Any]


class ProviderFailure(Exception):
    """A call that produced no usable reply. ``kind`` drives the ledger state and the fallback."""

    def __init__(self, kind: str, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.kind = (
            kind  # not_sent | unknown_outcome | http_error | invalid_response | no_credential
        )
        self.detail = detail
        self.status = status


class ChatCompletionsProvider:
    name = "chat-completions"
    capabilities = frozenset({"generation"})

    def __init__(
        self, config: ProviderConfig, secrets: SecretStore, transport: tp.Transport
    ) -> None:
        if config.endpoint is None or config.model is None:
            raise PrometheosError("validation_error", "no endpoint and model are configured", {})
        self.config, self.secrets, self.transport = config, secrets, transport

    # ----- capabilities ---------------------------------------------------------------------

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def _unsupported(self, capability: str) -> PrometheosError:
        return PrometheosError(
            "unsupported",
            f"the {self.name} adapter does not provide {capability}",
            {"capability": capability, "supported": sorted(self.capabilities)},
        )

    def embed(self, *_: Any, **__: Any) -> Any:
        raise self._unsupported("embedding")

    def classify(self, *_: Any, **__: Any) -> Any:
        raise self._unsupported("classification")

    def transcribe(self, *_: Any, **__: Any) -> Any:
        raise self._unsupported("transcription")

    # ----- generation -----------------------------------------------------------------------

    def payload(self, request: GenerationRequest) -> dict[str, Any]:
        """The exact JSON that would be sent (deterministic; its digest is the egress digest)."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ]
        messages += [{"role": role, "content": content} for role, content in request.follow_up]
        return {
            "model": self.config.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": request.schema},
            },
            "max_tokens": request.max_output_tokens,
            "temperature": 0,
        }

    def url(self) -> str:
        return f"{self.config.endpoint}/chat/completions"

    def generate(self, request: GenerationRequest) -> GenerationResult:
        body = self.payload(request)
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        reference = self.config.credential_ref
        if reference is not None:
            secret = self.secrets.get(reference)
            if secret is None:
                raise ProviderFailure(
                    "no_credential", f"no credential is stored for reference {reference}"
                )
            headers["Authorization"] = f"Bearer {secret}"
        try:
            reply = self.transport.post(self.url(), headers, raw, float(request.timeout_seconds))
        except tp.NotSent as exc:
            raise ProviderFailure("not_sent", f"nothing was sent ({exc})") from None
        except tp.OutcomeUnknown as exc:
            raise ProviderFailure(
                "unknown_outcome", f"the request may have been received ({exc})"
            ) from None
        if reply.status != 200:
            raise ProviderFailure(
                "http_error", f"the provider answered HTTP {reply.status}", reply.status
            )
        try:
            document = loads_strict(reply.body)
            choice = document["choices"][0]
            text = choice["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("content is not text")
            usage = document.get("usage") or {}
            model_id = str(document.get("model") or self.config.model)
            finish = choice.get("finish_reason")
            response_id = str(document.get("id") or "")[:100]
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ProviderFailure(
                "invalid_response", "the reply is not a chat completion"
            ) from None
        receipt = {
            "adapter": self.name,
            "endpoint_host": host_of(self.config.endpoint or ""),
            "model_id": model_id[:200],
            "request_sha256": canonical_sha256(body),
            "response_id": response_id,
            "finish_reason": finish if isinstance(finish, str) else None,
        }
        return GenerationResult(
            text=text,
            model_id=model_id[:200],
            usage=Usage(
                prompt if isinstance(prompt, int) else None,
                completion if isinstance(completion, int) else None,
            ),
            finish_reason=finish if isinstance(finish, str) else None,
            receipt=receipt,
        )


class LocalTranscriptionProvider:
    """The local speech engine as a provider that declares only transcription."""

    name = "local-whisper"
    capabilities = frozenset({"transcription"})

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


def capability_table(config: ProviderConfig) -> list[dict[str, Any]]:
    """Which route serves each capability under the configured policy (no network, no secrets)."""
    rows = []
    generation_ok = config.policy != "no-ai" and config.endpoint is not None
    for capability, provider, route, note in (
        (
            "generation",
            ChatCompletionsProvider.name,
            config.route if generation_ok else "none",
            "needs policy local-only or remote-allowed and a configured endpoint",
        ),
        (
            "transcription",
            LocalTranscriptionProvider.name,
            "local",
            "runs on this computer; remote transcription is not offered",
        ),
        ("embedding", None, "none", "no adapter yet (planned with the local AI checkpoint)"),
        ("classification", None, "none", "no adapter yet (planned with the local AI checkpoint)"),
    ):
        usable = provider is not None and route != "none"
        rows.append(
            {
                "capability": capability,
                "provider": provider,
                "route": route,
                "available": usable,
                "note": note,
            }
        )
    return rows
