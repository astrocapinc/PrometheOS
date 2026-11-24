"""Loopback researcher hub: typed HTTP API over the same application services plus the
built web assets (spec/04 "Local HTTP", spec/06, spec/08).

The paired browser session is the project owner at this computer (the pairing token is
only shown in the terminal that launched the hub). Owner decisions still follow the
two-step shape of the CLI channel: a preview shows protected effects and the exact
request digest, and an explicit second call carrying that digest issues a single-use
capability bound to it and applies the request in one step.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import Field, StringConstraints
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from prometheos import __version__, agentops, devices
from prometheos.agentops import skills as skills_ops
from prometheos.app import maintenance as maintenance_mod
from prometheos.app import runs as runs_mod
from prometheos.app.authority import OwnerAct
from prometheos.app.pathscrub import build_redactor
from prometheos.app.reads import Caller
from prometheos.audio import capture, engines, recorder, transcribe
from prometheos.domain.bodies import VerificationBudgetChange
from prometheos.domain.canonical import (
    JSONDepthError,
    check_text_depth,
    dumps_canonical,
    loads_strict,
)
from prometheos.domain.ids import new_request_id
from prometheos.domain.models import (
    MAX_INT,
    MAX_REQUEST_BYTES,
    ApprovalDecision,
    CapabilityRevoke,
    CapabilityRevokeReceipt,
    DeviceRevokeRequest,
    JsonObjectInput,
    PrometheosError,
    ProviderOwnerConfigureRequest,
    ProviderOwnerRunRequest,
    RecommendedRequest,
    StrictModel,
    depth_error,
)
from prometheos.hub.redaction import Redactor
from prometheos.hub.security import (
    CSRF_HEADER,
    SAFE_METHODS,
    SECURITY_HEADERS,
    SESSION_COOKIE,
    SESSION_TTL,
    Gate,
)
from prometheos.mcp_schemas import _inline

API = "/api/v1"
LONG_POLL_MAX = 20.0
LONG_POLL_TICK = 0.25
RUN_WATCH_TICK = 0.1  # an in-memory check, no store read
RUN_WATCH_SETTLE = 0.2
STATUS = {
    "validation_error": 400,
    "not_found": 404,
    "revision_conflict": 409,
    "request_key_conflict": 409,
    "stale_task": 409,
    "policy_denied": 403,
    "unsupported": 422,
    "budget_exceeded": 422,
    "busy": 503,
    "source_unavailable": 503,
    "io_error": 503,
    "approval_required": 403,
    "capability_unavailable": 503,
}
OWNER = Caller(actor="hub-owner", scope="owner")
AGENT = Caller(actor="http-agent", scope="agent")
UNPAIRED_PATHS = frozenset({f"{API}/pair", f"{API}/session", f"{API}/device/resume"})
LAUNCHER_PATHS = frozenset({f"{API}/launcher/code", f"{API}/launcher/stop"})
"""The owner channel (A07, CP21): reached only with the hub's user-only secret, on a request
that carries no Origin, fetch metadata or cookie (``launcher.owner_channel_ok``)."""
_CALLER: contextvars.ContextVar[Caller | None] = contextvars.ContextVar(
    "prometheos_hub_caller", default=None
)
AGENT_ACTOR = "http-agent"


def agent_routes() -> frozenset[str]:
    """Every HTTP route the registry declares for an operation agents may use (any authority
    but owner_channel and owner_scope): generated from the registry, so an agent-authority
    route can never be left out (review F4). Owner-channel routes and the changes long-poll
    (an exemption, not an operation) are never in it (decision 0011 R2-L1)."""
    from prometheos.registry import operations

    owner_only = ("owner_channel", "owner_scope")
    # A route shared by an owner-channel operation and an agent one (push/apply, provider
    # run/configure) is admitted: its handler branches on the caller and an agent only ever
    # reaches the request path. A route that only owner operations declare is never admitted.
    return frozenset(r for o in operations() if o.authority not in owner_only for r in o.http)


# A02, CP10.2 (decision 0016): the routes an agent bearer token reaches, at agent scope.
AGENT_ROUTES = agent_routes()
"""Generated from the registry (review F4); see ``agent_routes``."""

HUB_OWNER_ACT = OwnerAct(channel="hub-owner", actor="hub-owner")


class PairBody(StrictModel):
    token: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class DecisionBody(StrictModel):
    request: JsonObjectInput
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


class MeetingBody(StrictModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=200_000)]
    held_at: str | None = None
    sensitivity: str = "normal"
    request_id: str | None = None


class TaskPrepareBody(StrictModel):
    purpose: str
    budget: int = Field(default=12000, ge=1, le=200_000)
    meeting_id: str | None = None
    request_id: str | None = None


class TaskSubmitBody(StrictModel):
    result: JsonObjectInput
    request_id: str


class SourcePlanBody(StrictModel):
    root_alias: str
    paths: list[str] = Field(min_length=1, max_length=200)
    retention: str | None = None
    sensitivity: str = "normal"
    description: str = ""
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class AdoptionScanBody(StrictModel):
    root_alias: str = Field(min_length=1, max_length=64)
    exclusions: list[str] = Field(default_factory=list, max_length=200)
    limits: dict[str, int] | None = None
    profile: str = "generic"
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class LibraryPlanBody(StrictModel):
    root_alias: str = Field(min_length=1, max_length=64)
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    sensitivity: str = "normal"
    csv_columns: dict[str, str] | None = None
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


ZoteroKey = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{8}$")]


class ZoteroPlanBody(StrictModel):
    collection: ZoteroKey | None = None
    item_keys: list[ZoteroKey] | None = Field(default=None, min_length=1, max_length=200)
    sensitivity: str = "normal"
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class ZoteroCommitBody(StrictModel):
    item_keys: list[ZoteroKey] | None = Field(default=None, min_length=1, max_length=200)
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once
    request_id: str | None = None  # agent requests only (idempotent retry)


class AdoptionImportBody(StrictModel):
    package: JsonObjectInput  # an exported baseline package (CP17.6; decision 0045)


class MatrixExtractBody(StrictModel):
    rules: JsonObjectInput  # the rule set (decision 0064 §2; checked by the rule engine)
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class WaitBody(StrictModel):
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class RunResumeBody(StrictModel):
    budget_amount: Annotated[str, StringConstraints(pattern=r"^\d{1,9}(\.\d{1,6})?$")] | None = None
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class RunAcknowledgeBody(StrictModel):
    note: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None


# ----- CP10.5: maintenance routes (decision 0021) -------------------------------------------

Location = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class MaintenanceVerifyBody(StrictModel):
    backup: Location  # an archive name from inspect, or an absolute path


class MaintenanceRestoreBody(StrictModel):
    backup: Location
    into: Location | None = None  # absolute; default: a new folder under the home


class MaintenanceSwitchBody(StrictModel):
    backup: Location
    target: Location  # the restored store: its label from restore, or an absolute path
    request_id: str | None = None  # agent requests only (idempotent retry)


class RequestIdBody(StrictModel):
    request_id: str | None = None  # agent requests only (idempotent retry)


MAINTENANCE_ROUTES = frozenset(
    {
        f"GET {API}/maintenance/inspect",
        f"POST {API}/maintenance/verify",
        f"POST {API}/maintenance/restore",
        f"POST {API}/maintenance/journal-recovery",
        f"POST {API}/maintenance/switch",
        f"POST {API}/maintenance/migrate",
        f"GET {API}/maintenance/approvals",
        f"GET {API}/maintenance/approvals/{{approval_id}}",
        f"POST {API}/maintenance/approvals/{{approval_id}}/resume",
        f"POST {API}/maintenance/approvals/{{approval_id}}/withdraw",
        f"POST {API}/maintenance/approvals/{{approval_id}}/grant",
        f"POST {API}/maintenance/approvals/{{approval_id}}/deny",
    }
)


class InstallPreviewBody(StrictModel):
    host: str = "claude-code"


class SaveBody(StrictModel):
    request_id: str | None = None


# ----- CP9: LaTeX workspace and Overleaf Git (decision 0012) -----------------------------------

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
WsPath = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class WorkspaceWriteBody(StrictModel):
    path: WsPath
    text: Annotated[str, StringConstraints(max_length=2 * 1024 * 1024)]
    base_sha256: Sha256 | None


class WorkspaceSnapshotBody(StrictModel):
    paths: list[WsPath] | None = Field(default=None, max_length=200)


class WorkspaceBindBody(StrictModel):
    root_alias: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    main: WsPath


class WorkspaceInsertBody(StrictModel):
    kind: Annotated[str, StringConstraints(pattern=r"^(cite|figure)$")]
    version_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    bib_file: WsPath | None = None


class CompileBody(StrictModel):
    fetch: bool = False
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class EngineInstallBody(StrictModel):
    digest: Sha256
    request_id: str | None = None  # agent requests only (idempotent retry)
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class OverleafLinkBody(StrictModel):
    url: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    root_alias: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    folder: WsPath
    main: WsPath = "main.tex"


class PreviewBody(StrictModel):
    fetch: bool = True


class DigestBody(StrictModel):
    digest: Sha256
    request_id: str | None = None  # agent requests only (idempotent retry)


ApprovalDecisionBody = ApprovalDecision  # the exported contract (decision 0016)


class JobResolveBody(StrictModel):
    due_utc: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    resolution: Annotated[str, StringConstraints(pattern=r"^(succeeded|failed|abandoned)$")]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    request_id: str | None = None


class EventAcknowledgeBody(StrictModel):
    event_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] = Field(
        min_length=1, max_length=50
    )
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    request_id: str | None = None  # agent requests only (idempotent retry)


class SkillTrustBody(StrictModel):
    package_digest: Sha256  # the digest the owner reviewed
    request_id: str | None = None  # agent requests only (idempotent retry)


class ModelInstallBody(StrictModel):
    plan_sha256: Sha256
    model: Annotated[str, StringConstraints(min_length=1, max_length=100)] = "faster-whisper-base"
    request_id: str | None = None  # agent requests only (idempotent retry)
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class ModelRemoveBody(StrictModel):
    model: Annotated[str, StringConstraints(min_length=1, max_length=100)] = "faster-whisper-base"
    request_id: str | None = None


class PurgeCommitBody(StrictModel):
    plan_sha256: Sha256  # the plan digest the owner reviewed
    request_id: str | None = None  # agent requests only (idempotent retry)


class ExecutionRegisterBody(StrictModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    host_label: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    reporter: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    command_summary: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    working_root_alias: Annotated[str, StringConstraints(max_length=64)] | None = None
    external_ref: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None
    expected_outputs: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=50
    )
    sensitivity: str = "normal"
    request_id: str | None = None


class ExecutionReportBody(StrictModel):
    state: Annotated[
        str,
        StringConstraints(
            pattern=r"^(reported_running|reported_completed|reported_failed|reported_cancelled"
            r"|status_unknown)$"
        ),
    ]
    reported_at: str | None = None
    receipt_version_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] = (
        Field(default_factory=list, max_length=50)
    )
    request_id: str | None = None


class AwaitingResolveBody(StrictModel):
    resolution: Annotated[str, StringConstraints(pattern=r"^(interpreted|dismissed)$")]
    text: Annotated[str, StringConstraints(min_length=1, max_length=65536)]
    request_id: str | None = None


class SourceCheckBody(StrictModel):
    ids: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] = Field(
        min_length=1, max_length=200
    )
    save: bool = False
    request_id: str | None = None


class CommitBody(StrictModel):
    message: Annotated[str, StringConstraints(min_length=1, max_length=500)]


Uuidish = Annotated[str, StringConstraints(min_length=1, max_length=80)]


class AudioImportBody(StrictModel):
    root_alias: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    path: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    sensitivity: str = "restricted"
    title: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None
    wait_seconds: int = Field(default=0, ge=0, le=20)  # long operations: run at once


class RecordingStartBody(StrictModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    device_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=2
    )
    sensitivity: str = "restricted"
    segment_seconds: int = Field(default=30, ge=5, le=300)
    consent_acknowledged: bool = False
    request_id: str | None = None  # agent requests only (idempotent retry)


class TranscribeBody(StrictModel):
    version_id: Uuidish
    engine: str = "faster-whisper"
    model: str | None = None
    language: str | None = None


class SegmentEditBody(StrictModel):
    segment_id: Annotated[str, StringConstraints(pattern=r"^seg-[0-9]{5,6}$")]
    text: Annotated[str, StringConstraints(max_length=5000)] | None = None
    speaker_label: Annotated[str, StringConstraints(max_length=100)] | None = None
    uncertain: bool | None = None


class CorrectionBody(StrictModel):
    edits: list[SegmentEditBody] = Field(min_length=1, max_length=500)
    request_id: str | None = None


class MeetingAudioBody(StrictModel):
    transcript_version_id: Uuidish
    audio_version_id: Uuidish | None = None
    title: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    held_at: str | None = None
    request_id: str | None = None


class ProviderPreviewBody(StrictModel):
    purpose: str
    meeting_id: str | None = None
    context_budget: int = Field(default=12000, ge=1000, le=200_000)
    include_restricted: bool = False


ProviderRunBody = ProviderOwnerRunRequest
ProviderConfigBody = ProviderOwnerConfigureRequest


# A previewed document gets a policy of its own instead of the app's: nothing previewed
# may run script, load or frame anything; a PDF may use the browser's built-in viewer.
TEXT_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"
CONTENT_CSP = {
    "application/pdf": "default-src 'none'; object-src 'self'; frame-ancestors 'none'",
    "audio/wav": TEXT_CSP,
    "audio/mpeg": TEXT_CSP,
    "audio/mp4": TEXT_CSP,
    "audio/ogg": TEXT_CSP,
    "audio/flac": TEXT_CSP,
    "image/png": TEXT_CSP,
    "image/jpeg": TEXT_CSP,
    "image/gif": TEXT_CSP,
    "image/webp": TEXT_CSP,
}


def _error(exc: PrometheosError) -> JSONResponse:
    body = exc.to_response().model_dump(mode="json")
    return JSONResponse(body, status_code=STATUS.get(exc.code, 400))


def _denied(message: str, status: int = 403) -> JSONResponse:
    body = PrometheosError("policy_denied", message, {}).to_response().model_dump(mode="json")
    return JSONResponse(body, status_code=status)


DEFAULT_BODY_BYTES = MAX_REQUEST_BYTES + 64 * 1024
"""The largest JSON request body of any route but the two below: a whole transaction request
(8 MiB, the application's own limit, which still answers a body just over it) plus slack."""

ROUTE_BODY_BYTES = {
    # {"token": ...} with at most 200 characters; the route is open before pairing.
    f"POST {API}/pair": 4 * 1024,
    # Workspace text is up to 2 Mi characters, and a JSON writer may escape each astral
    # character as a 12-byte surrogate pair.
    f"PUT {API}/papers/{{paper_id}}/workspace/file": 24 * 1024 * 1024 + 64 * 1024,
}
"""Routes whose legitimate body is smaller or larger than the default, with their own cap."""


def depth_refusal(exc: JSONDepthError) -> JSONResponse:
    return _error(depth_error(exc))


class _BodyBounds:
    """Pure ASGI middleware, inner to the hub's request guard (so only requests the guard
    would serve reach it). For a route that takes a body it enforces the size cap before and
    while reading (a ``Content-Length`` over the cap is refused unread; a chunked body is
    counted as it streams), then refuses nesting deeper than the shared input limit before a
    handler or the framework's own (recursive) JSON parser sees it, and replays the body. A
    route that takes no body, or a method it does not serve, is passed through unread."""

    def __init__(self, app: ASGIApp, limit_for: Callable[[Scope], int | None]) -> None:
        self.app = app
        self.limit_for = limit_for

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] in SAFE_METHODS
            or not scope["path"].startswith(API)
        ):
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return
        refusal: Response | None = None
        declared = Headers(scope=scope).get("content-length")
        if (
            declared is not None
            and declared.isascii()
            and declared.isdigit()
            and int(declared) > limit
        ):
            refusal = self.too_large(limit)
        chunks: list[bytes] = []
        total = 0
        more = refusal is None
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            part = message.get("body", b"")
            total += len(part)
            if total > limit:
                refusal = self.too_large(limit)
                break
            chunks.append(part)
            more = message.get("more_body", False)
        raw = b"".join(chunks)
        if refusal is None:
            try:
                check_text_depth(raw)
            except JSONDepthError as exc:
                refusal = depth_refusal(exc)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    def too_large(limit: int) -> Response:
        refusal = _error(
            PrometheosError(
                "budget_exceeded",
                f"the request body is larger than the {limit} bytes this route accepts",
                {"limit": limit},
            )
        )
        refusal.status_code = 413
        return refusal


def _json(value: Any) -> Response:
    """A JSON reply. Local paths are removed later, for every JSON body in one place (the
    ``guard`` middleware, :mod:`prometheos.hub.redaction`)."""
    return Response(dumps_canonical(value), media_type="application/json")


_redactor = build_redactor  # shared with the MCP adapter (decision 0022)


async def _without_local_paths(
    response: Response, redactor: Redactor, *, owner_root_preview: bool = False
) -> Response:
    """Apply the redaction to a JSON API body, success or error, whichever route or handler
    produced it. A previewed document (it carries its own CSP) is the researcher's bytes and
    is left alone. A JSON body that cannot be read is not sent at all."""
    if "content-security-policy" in response.headers:
        return response
    if not response.headers.get("content-type", "").startswith("application/json"):
        return response
    raw = getattr(response, "body", None)
    if not isinstance(raw, bytes):
        raw = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
    if not raw.strip():  # e.g. HEAD: no body, nothing to redact
        value = shown = None
    else:
        try:
            value = json.loads(raw)
            shown = redactor.body(value, envelope=response.status_code >= 400)
            if owner_root_preview and response.status_code < 400 and isinstance(value, dict):
                shown = redactor.body(shown, envelope=True)
                from prometheos.domain.models import RootChangePreview

                # This exception restores only this closed, typed field on an authenticated
                # owner decision preview. No plan, error, list or arbitrary text is exempted.
                preview = value.get("preview") if "preview" in value else value
                rendered = shown.get("preview") if "preview" in value else shown
                if isinstance(preview, dict) and isinstance(rendered, dict):
                    changes = preview.get("root_changes", [])
                    if isinstance(changes, list):
                        rendered["root_changes"] = [
                            RootChangePreview.model_validate(c).model_dump(mode="json")
                            for c in changes
                        ]
        except Exception:
            failed = _error(PrometheosError("io_error", "internal error in the hub", {}))
            failed.status_code = 500
            return failed
    body = raw if shown == value else json.dumps(shown, ensure_ascii=False).encode("utf-8")
    redacted = Response(body, status_code=response.status_code)
    redacted.raw_headers = [
        (name, value_) for name, value_ in response.raw_headers if name.lower() != b"content-length"
    ] + [(b"content-length", str(len(body)).encode("latin-1"))]
    return redacted


def _current() -> Caller:
    """The caller of the request being served (set by the middleware)."""
    caller = _CALLER.get()
    if caller is None:
        raise PrometheosError("policy_denied", "no session for this request", {})
    return caller


class _ChangeFeed:
    """The waiting side of the ``/changes`` long-poll, shared by every poll of one hub
    (decision 0074). Each store read takes the project's admission gate, so waiting polls
    share one sequence probe, read at most once per ``LONG_POLL_TICK``, instead of each
    reading the store on its own; a write served by this hub wakes them at once. A write by
    another process (CLI, MCP) is seen on the next probe."""

    def __init__(self, probe: Callable[[], int]) -> None:
        self._probe = probe
        self._seq: int | None = None
        self._read_at = 0.0
        self._generation = 0  # counts pokes, so a probe that raced one is not cached
        # Created on first use: anyio primitives need the running event loop.
        self._lock: anyio.Lock | None = None
        self._wake: anyio.Event | None = None

    async def latest(self) -> int:
        """The project sequence, from the store unless a probe within the last tick read it."""
        if self._lock is None:
            self._lock = anyio.Lock()
        async with self._lock:
            if self._seq is not None and anyio.current_time() - self._read_at < LONG_POLL_TICK:
                return self._seq
            for _ in range(2):
                # A write served during the probe may be missing from it: probe once more,
                # and never cache a result that raced a write.
                generation = self._generation
                seq = await anyio.to_thread.run_sync(self._probe)
                if generation == self._generation:
                    self._seq, self._read_at = seq, anyio.current_time()
                    break
            return seq

    async def wait(self, timeout: float) -> None:
        """Up to ``timeout`` seconds, or until this hub serves a write."""
        if self._wake is None:
            self._wake = anyio.Event()
        with anyio.move_on_after(timeout):
            await self._wake.wait()

    def poke(self) -> None:
        """A write was served: the next probe reads the store, and waiting polls wake."""
        self._seq = None
        self._generation += 1
        if self._wake is not None:
            self._wake.set()
            self._wake = None


class _ByCaller:
    """The application service as seen by the current caller: agent-token requests use a copy
    whose actor is the agent HTTP transport (commits and audit rows say so)."""

    def __init__(self, owner: Any, agent: Any) -> None:
        self._owner = owner
        self._agent = agent

    def __getattr__(self, name: str) -> Any:
        target = self._agent if _CALLER.get() is AGENT else self._owner
        return getattr(target, name)


def _static_dir() -> Path:
    return Path(str(resources.files("prometheos.hub") / "static"))


ASSET_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
}


SERVICE_WORKER = """\
// The PrometheOS hub's service worker (A07, CP21.3; decision 0090), written by the hub.
// It keeps only the built page and its assets, in a cache named by the build digest; it
// never handles /api, never stores a response it did not precache, and fetches pages
// network-first.
const CACHE = %(cache)s;
const SHELL = %(shell)s;
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => names.filter((name) => name !== CACHE))
      .then((old) => Promise.all(old.map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  );
});
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match("/", { cacheName: CACHE }).then((cached) => cached || Response.error()),
      ),
    );
    return;
  }
  if (SHELL.includes(url.pathname)) {
    event.respondWith(
      caches.match(request, { cacheName: CACHE }).then((cached) => cached || fetch(request)),
    );
  }
});
"""


def service_worker(static: Path) -> str | None:
    """The service worker for the shipped build, or None when no build is shipped. The cache
    name carries the build's source digest, so a new build replaces the old cache."""
    try:
        info = json.loads((static / "build-info.json").read_text(encoding="utf-8"))
        digest = str(info["source_sha256"])[:16]
        files = sorted(str(name) for name in info["files"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    shell = ["/"] + ["/" + name for name in files if name != "index.html"]
    return SERVICE_WORKER % {
        "cache": json.dumps(f"prometheos-hub-{digest}"),
        "shell": json.dumps(shell),
    }


def asset_media_type(name: str) -> str:
    """A built asset's media type from a fixed table, never from the host: on Windows Python's
    ``mimetypes`` reads the registry, where another program may map ``.mjs`` to ``text/plain``
    (seen on the owner's PC), and a module script with that type is refused by the browser."""
    return ASSET_MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def create_app(
    service: Any,
    project_id: str,
    gate: Gate,
    static_dir: Path | None = None,
    maintenance: bool | None = None,
) -> FastAPI:
    """``service`` is an app.service.Service bound to the user's PrometheOS home.

    ``maintenance`` (CP10.5): ``True`` serves the maintenance mode, which opens no project
    database (only the maintenance routes answer; every other API route answers with the
    recovery pointer and the app shows its recovery screen). ``None`` chooses it when the
    project store does not open."""
    if maintenance is None:
        try:
            service.open(project_id)
            maintenance = False
        except PrometheosError as exc:
            if "recovery" not in exc.details:
                raise
            maintenance = True
    elif maintenance:
        service.registry.lookup(project_id)  # an unknown project fails fast (registry only)
    else:
        service.open(project_id)  # fail fast on an unknown project
    # Agent-token requests commit and audit as the agent HTTP transport (review F11); the
    # paired session keeps the hub's own label.
    agent_service = copy.copy(service)
    agent_service.actor = AGENT_ACTOR
    service = _ByCaller(service, agent_service)
    app = FastAPI(
        title="PrometheOS hub",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        # Not served (decision 0015 round 2, R5): spec/04's exemption list has no API
        # description route. The OpenAPI document stays available offline: app.openapi().
        openapi_url=None,
    )

    redactor = _redactor(service, project_id, maintenance)
    app.state.maintenance = maintenance
    feed = _ChangeFeed(lambda: service.latest_seq(project_id))

    def _maintenance_blocked(request: Request) -> Response | None:
        """In maintenance mode only the maintenance routes run; any other API route answers
        the recovery pointer without touching the project store."""
        path = request.url.path
        # Admission is explicit: orphan collection and whole-store backups open the
        # database, so neither may run while the hub is in maintenance mode.
        if not maintenance or path == f"{API}/logout":
            return None
        if _route_key(request) in MAINTENANCE_ROUTES:
            return None
        why = maintenance_mod.recovery_error(
            project_id, "unopenable", "the hub was started in maintenance mode"
        )
        return _error(why)

    # Routes that read ``request.body()`` themselves (the transaction route and every agent
    # route with a JSON body); a framework route takes a body when it declares one.
    raw_body_routes: set[str] = {f"POST {API}/transactions"}

    def _body_limit(scope: Scope) -> int | None:
        """The body cap of the route that will serve this request; ``None`` if no route
        takes a body here (an unknown path, a method the path does not serve, a route
        without a body)."""
        for route in app.router.routes:
            matched, _ = route.matches(scope)
            if matched == Match.FULL:
                key = f"{scope['method']} {getattr(route, 'path', '')}"
                if key in ROUTE_BODY_BYTES:
                    return ROUTE_BODY_BYTES[key]
                if key in raw_body_routes or getattr(route, "body_field", None) is not None:
                    return DEFAULT_BODY_BYTES
                return None
        return None

    # Added before the guard, so it is inner to it: the guard's host, origin and session
    # checks come first.
    app.add_middleware(_BodyBounds, limit_for=_body_limit)

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        try:
            response = await _guarded(request, call_next)
        except Exception:  # never a bare 500 without the envelope or security headers
            response = _error(PrometheosError("io_error", "internal error in the hub", {}))
            response.status_code = 500
        if request.method not in SAFE_METHODS and request.url.path.startswith(API):
            feed.poke()  # a write may have committed: waiting change polls look now
        if request.url.path.startswith(API):
            # Every JSON body, of every route, method and status, passes here.
            route = _route_key(request)
            root_preview = getattr(request.state, "caller", None) is OWNER and route in {
                f"POST {API}/decisions/preview",
                f"GET {API}/approvals/{{approval_id}}",
            }
            response = await _without_local_paths(
                response, redactor, owner_root_preview=root_preview
            )
        for name, value in SECURITY_HEADERS.items():
            if name == "content-security-policy" and name in response.headers:
                continue  # a previewed document carries its own, stricter policy
            response.headers[name] = value
        if request.url.path.startswith(API):
            response.headers["cache-control"] = "no-store"
        return response

    def _home() -> Path:  # read when needed: some test doubles of the service have no home
        return Path(service.home)

    device_cookie = devices.cookie_name(gate.home_host.split(".")[0]) if gate.home_host else None
    revoked_seen: dict[str, tuple[float, bool]] = {}

    def _device_revoked(device_id: str) -> bool:
        """Read the devices store at most every 2 seconds per device (A07, CP21.2)."""
        now = time.monotonic()
        cached = revoked_seen.get(device_id)
        if cached is None or now - cached[0] > 2.0:
            cached = (now, devices.is_revoked(_home(), device_id))
            revoked_seen[device_id] = cached
        return cached[1]

    def _home_origin_ok(request: Request) -> bool:
        """The home's own host name, port and Origin, exactly (spec/08; review cp21-a F2)."""
        return gate.is_home_host(request.headers.get("host")) and (
            request.headers.get("origin") == gate.home_origin()
        )

    def _set_device_cookie(response: Response, value: str) -> None:
        assert device_cookie is not None
        response.set_cookie(
            device_cookie,
            value,
            max_age=int(devices.MAX_SECONDS),
            httponly=True,
            samesite="strict",
            path="/",
        )  # host-only: no Domain attribute, so only the home's own host name receives it

    def _set_session_cookie(response: Response, session_id: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=int(SESSION_TTL),
            httponly=True,
            samesite="strict",
            path="/",
        )

    def _route_key(request: Request) -> str | None:
        """``"METHOD /template"`` of the route that will serve this request."""
        for route in app.router.routes:
            matched, _ = route.matches(request.scope)
            if matched == Match.FULL:
                return f"{request.method} {getattr(route, 'path', '')}"
        return None

    async def _guarded(request: Request, call_next: Any) -> Response:
        if not gate.host_ok(request.headers.get("host")):
            return _denied("unexpected Host header", 421)
        path = request.url.path
        if path in LAUNCHER_PATHS:
            from prometheos.launcher import owner_channel_ok

            if request.method != "POST" or not owner_channel_ok(
                getattr(app.state, "owner_secret", None), request.headers
            ):
                return _denied("the owner channel needs the launcher's secret", 403)
            return await call_next(request)  # type: ignore[no-any-return]
        bearer = request.headers.get("authorization")
        agent_request = bearer is not None and path.startswith(API) and path not in UNPAIRED_PATHS
        origin = request.headers.get("origin")
        # A browser cannot attach an Authorization header cross-site without a CORS preflight
        # (never granted), so an agent's non-browser request may omit Origin; a sent Origin
        # must still be exact.
        if not (agent_request and origin is None) and not gate.origin_ok(
            request.method, origin, request.headers.get("sec-fetch-site")
        ):
            return _denied("cross-origin request refused")
        if not path.startswith(API) or path in UNPAIRED_PATHS:
            return await call_next(request)  # type: ignore[no-any-return]
        if agent_request:
            if not gate.agent_ok(bearer):
                return _denied("unknown agent token", 401)
            if request.cookies.get(SESSION_COOKIE):
                return _denied("send an agent token or a paired session, not both")
            if _route_key(request) not in AGENT_ROUTES:
                return _denied("this route is for the paired owner session")
            request.state.caller = AGENT
            blocked = _maintenance_blocked(request)
            if blocked is not None:
                return blocked
            token = _CALLER.set(AGENT)
            try:
                return await call_next(request)  # type: ignore[no-any-return]
            finally:
                _CALLER.reset(token)
        session = gate.session(request.cookies.get(SESSION_COOKIE))
        if (
            session is not None
            and session.device_id
            and await run_in_threadpool(_device_revoked, session.device_id)  # off the loop (A6)
        ):
            gate.end_device(session.device_id)  # A07, CP21.2: revoked from the CLI or elsewhere
            session = None
        if session is None:
            return _denied("pair this browser first", 401)
        if request.method not in SAFE_METHODS and not gate.csrf_ok(
            session, request.headers.get(CSRF_HEADER)
        ):
            return _denied("missing or wrong CSRF token")
        request.state.caller = OWNER
        blocked = _maintenance_blocked(request)
        if blocked is not None:
            return blocked
        token = _CALLER.set(OWNER)
        try:
            return await call_next(request)  # type: ignore[no-any-return]
        finally:
            _CALLER.reset(token)

    def _who(request: Request) -> Caller:
        """The caller the middleware established (paired owner or agent token)."""
        caller = getattr(request.state, "caller", None)
        if not isinstance(caller, Caller):
            raise PrometheosError("policy_denied", "no session for this request", {})
        return caller

    def _require_owner(request: Request) -> None:
        """Owner-channel handlers check the session themselves too, not only the middleware's
        allow-list (review F14)."""
        if _who(request) is not OWNER:
            raise PrometheosError("policy_denied", "this route is for the paired owner session", {})

    def _is_owner(request: Request) -> bool:
        return _who(request) is OWNER

    @app.exception_handler(PrometheosError)
    async def domain_error(_: Request, exc: PrometheosError) -> JSONResponse:
        return _error(exc)

    @app.exception_handler(JSONDepthError)
    async def depth_error_handler(_: Request, exc: JSONDepthError) -> JSONResponse:
        # Outside input that is too deep is the validation error; the application's own
        # ceiling is a deterministic, non-retryable limit (decision 0029).
        return depth_refusal(exc)

    @app.exception_handler(sqlite3.DatabaseError)
    async def store_error(_: Request, exc: sqlite3.DatabaseError) -> JSONResponse:
        # CP10.5: a store that fails while it is read points to the maintenance service.
        return _error(maintenance_mod.database_error(project_id, exc))

    @app.exception_handler(OSError)
    async def file_error(_: Request, exc: OSError) -> JSONResponse:
        # Round 2 (F4): a file-system failure is a typed envelope, never a bare 500.
        return _error(maintenance_mod.file_error(exc))

    @app.exception_handler(RequestValidationError)
    async def request_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": "/".join(str(x) for x in e.get("loc", ())), "msg": str(e.get("msg", ""))}
            for e in exc.errors()[:20]
        ]
        return _error(
            PrometheosError(
                "validation_error", "request parameters are invalid", {"errors": errors}
            )
        )

    # ----- the owner channel (A07, CP21; decision 0088) ------------------------------------

    @app.post(f"{API}/launcher/code")
    def launcher_code() -> Response:
        from prometheos.launcher import LAUNCH_TTL

        return _json({"code": gate.new_pairing_token(ttl=LAUNCH_TTL), "ttl_seconds": LAUNCH_TTL})

    @app.post(f"{API}/launcher/stop")
    def launcher_stop() -> Response:
        stop = getattr(app.state, "request_stop", None)
        if stop is None:
            return _denied("this hub was not started with a stop hook", 409)
        stop()
        return _json({"stopping": True})

    # ----- pairing ------------------------------------------------------------------------

    @app.post(f"{API}/pair")
    def pair(body: PairBody, request: Request) -> Response:
        paired = gate.pair(body.token)
        if paired is None:
            return _denied("pairing token is wrong, used or expired; start a new pairing")
        session_id, csrf = paired
        response = _json({"paired": True, "csrf_token": csrf})
        _set_session_cookie(response, session_id)
        if device_cookie and _home_origin_ok(request):
            label = devices.browser_label(request.headers.get("user-agent"))
            remembered = devices.remember(_home(), label)
            gate.bind_device(session_id, remembered.split(".", 1)[0])
            _set_device_cookie(response, remembered)
        return response

    @app.post(f"{API}/device/resume")
    def device_resume(request: Request) -> Response:
        """Exchange this browser's remembered device credential for a new session (A07,
        CP21.2): only on the home's own host name, never with an agent token."""
        if request.headers.get("authorization") is not None:
            return _denied("an agent token cannot use a remembered device")
        if device_cookie is None or not _home_origin_ok(request):
            return _denied("remembered devices answer only on the home's own host name and origin")
        resumed = devices.resume(_home(), request.cookies.get(device_cookie))
        if resumed is None:
            refused = _denied("this browser is not remembered; start a new pairing", 401)
            refused.delete_cookie(device_cookie, path="/")
            return refused
        device_id, rotated = resumed
        session_id, csrf = gate.new_session(device_id)
        response = _json({"paired": True, "csrf_token": csrf})
        _set_session_cookie(response, session_id)
        if rotated is not None:
            _set_device_cookie(response, rotated)
        return response

    @app.get(f"{API}/session")
    def session_info(request: Request) -> Response:
        session = gate.session(request.cookies.get(SESSION_COOKIE))
        mode = "maintenance" if maintenance else "normal"  # CP10.5: the recovery screen
        if session is None:
            return _json({"paired": False, "mode": mode})
        return _json(
            {"paired": True, "csrf_token": session.csrf, "project_id": project_id, "mode": mode}
        )

    @app.post(f"{API}/logout")
    def logout(request: Request) -> Response:
        gate.end(request.cookies.get(SESSION_COOKIE))
        response = _json({"paired": False})
        response.delete_cookie(SESSION_COOKIE, path="/")
        presented = request.cookies.get(device_cookie) if device_cookie else None
        if device_cookie and presented and "." in presented and _home_origin_ok(request):
            with contextlib.suppress(PrometheosError):  # signing out forgets this browser
                device_id = presented.split(".", 1)[0]
                if devices.resume(_home(), presented) is not None:
                    devices.revoke(_home(), device_id)
                    gate.end_device(device_id)
            response.delete_cookie(device_cookie, path="/")
        return response

    # ----- remembered devices (A07, CP21.2; decision 0089) --------------------------------

    @app.get(f"{API}/devices")
    def device_list(request: Request) -> Response:
        _require_owner(request)
        return _json(service.device_list())

    @app.post(f"{API}/devices/revoke")
    def device_revoke(body: DeviceRevokeRequest, request: Request) -> Response:
        _require_owner(request)
        result = service.device_revoke(body.device_id)
        gate.end_device(body.device_id)
        revoked_seen.pop(body.device_id, None)
        return _json(result)

    # ----- reads --------------------------------------------------------------------------

    @app.get(f"{API}/project")
    def project(request: Request) -> Response:
        info = service.project_info(project_id, _who(request))
        return _json(info)

    @app.get(f"{API}/capabilities")
    def capabilities() -> Response:
        # A02, CP10.1: the same agent-view discovery report as the CLI and MCP.
        return _json(service.capabilities(project_id))

    # ----- CP10.4: research continuity (decision 0018) -------------------------------------

    @app.get(f"{API}/now")
    def now_view(
        since_seq: Annotated[int, Query(ge=0, le=MAX_INT)] | None = None,
        due_within_days: Annotated[int, Query(ge=1, le=365)] = 7,
        silence_after_hours: Annotated[int, Query(ge=1, le=2160)] = 24,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Response:
        return _json(
            service.now(
                project_id,
                _current(),
                since_seq=since_seq,
                due_within_days=due_within_days,
                silence_after_hours=silence_after_hours,
                limit=limit,
            )
        )

    # ----- CP18.2-CP18.3: research events and the agent-visible queue (decision 0035) ----

    @app.get(f"{API}/queue")
    def work_queue(
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        proposed: Annotated[str, Query(pattern=r"^(fold|list)$")] = "fold",
    ) -> Response:
        return _json(service.work_queue(project_id, _current(), limit, proposed))

    @app.get(f"{API}/events")
    def event_list(
        type: Annotated[str, Query(max_length=64)] | None = None,
        subject_id: Annotated[str, Query(max_length=80)] | None = None,
        state: Annotated[str, Query(pattern=r"^(open|acknowledged|all)$")] = "open",
        cursor: Annotated[str, Query(max_length=120)] | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Response:
        return _json(
            service.event_list(
                project_id,
                _current(),
                event_type=type,
                subject_id=subject_id,
                state=state,
                cursor=cursor,
                limit=limit,
            )
        )

    @app.post(f"{API}/events/acknowledge")
    def event_acknowledge(body: EventAcknowledgeBody, request: Request) -> Response:
        # The paired owner acknowledges at once with its own act; an agent token gets an
        # approval request (the owner decides it in Approvals).
        if not _is_owner(request):
            params = {"event_ids": list(body.event_ids), "note": body.note}
            return _json(
                service.approval_request(
                    project_id, AGENT, "event_acknowledge", params, body.request_id
                )
            )
        return _json(
            service.event_acknowledge(
                project_id, list(body.event_ids), body.note, authority=HUB_OWNER_ACT
            )
        )

    @app.get(f"{API}/executions")
    def executions_list(
        state: str | None = None,
        awaiting_interpretation: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        silence_after_hours: Annotated[int, Query(ge=1, le=2160)] = 24,
    ) -> Response:
        return _json(
            service.execution_list(
                project_id,
                _current(),
                state=state,
                awaiting=awaiting_interpretation,
                limit=limit,
                silence_after_hours=silence_after_hours,
            )
        )

    @app.post(f"{API}/executions")
    def execution_register(body: ExecutionRegisterBody) -> Response:
        return _json(
            service.execution_register(
                project_id,
                _current(),
                title=body.title,
                host_label=body.host_label,
                reporter=body.reporter,
                command_summary=body.command_summary,
                working_root_alias=body.working_root_alias,
                external_ref=body.external_ref,
                expected_outputs=body.expected_outputs,
                sensitivity=body.sensitivity,
                request_id=body.request_id or new_request_id(),
            )
        )

    @app.post(f"{API}/executions/{{execution_id}}/report")
    def execution_report(execution_id: str, body: ExecutionReportBody) -> Response:
        return _json(
            service.execution_report(
                project_id,
                _current(),
                execution_id,
                state=body.state,
                reported_at=body.reported_at,
                receipt_version_ids=body.receipt_version_ids,
                request_id=body.request_id or new_request_id(),
            )
        )

    @app.get(f"{API}/awaiting-interpretation")
    def awaiting_list() -> Response:
        return _json(service.awaiting_list(project_id, _current()))

    @app.post(f"{API}/awaiting-interpretation/{{target_id}}/resolve")
    def awaiting_resolve(target_id: str, body: AwaitingResolveBody, request: Request) -> Response:
        """Take one item out of the queue with an interpretation or a dismissal reason (a
        reviewed entry citing it). The paired owner decides at once, like ``decisions/apply``;
        an agent token gets ``approval_required`` (the entry is a protected effect)."""
        caller = _current()
        rid = body.request_id or new_request_id()
        if not _is_owner(request):
            return _json(
                service.awaiting_resolve(
                    project_id, caller, target_id, body.resolution, body.text, rid
                )
            )
        replay = service.awaiting_resolve_replay(project_id, caller, rid)
        if replay is not None:
            return _json({"preview": None, "result": replay})
        raw = service.awaiting_resolve_request(
            project_id, caller, target_id, body.resolution, body.text, rid
        )
        preview = service.review_request(project_id, raw, caller)
        secret = None
        if preview["protected_effects"]:
            grant = service.grant(
                project_id,
                operation="exact_request",
                digest=preview["digest"],
                ttl_seconds=60,
                max_uses=1,
                label="hub owner decision",
            )
            secret = grant.secret
        result = service.apply(project_id, raw, caller, secret)
        return _json({"preview": preview, "result": result})

    @app.get(f"{API}/artifacts/coverage")
    def artifact_coverage(
        source_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        cursor: Annotated[str, Query(max_length=100)] | None = None,
        newest_first: bool = False,
    ) -> Response:
        return _json(
            service.artifact_coverage(
                project_id,
                _current(),
                source_id=source_id,
                limit=limit,
                cursor=cursor,
                newest_first=newest_first,
            )
        )

    @app.post(f"{API}/sources/check")
    def source_check(body: SourceCheckBody) -> Response:
        return _json(
            service.source_check(project_id, body.ids, body.save, _current(), body.request_id)
        )

    @app.get(f"{API}/records")
    def records(
        kind: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
        include_archived: bool = False,
        at_seq: Annotated[int, Query(ge=0, le=MAX_INT)] | None = None,
    ) -> Response:
        return _json(
            service.list_records(
                project_id, _current(), kind=kind, limit=limit, cursor=cursor,
                include_archived=include_archived, at_seq=at_seq,
            )
        )  # fmt: skip

    @app.get(f"{API}/records/{{record_id}}")
    def record(
        record_id: str, at_seq: Annotated[int, Query(ge=0, le=MAX_INT)] | None = None
    ) -> Response:
        return _json(service.get(project_id, _current(), record_id, at_seq))

    @app.get(f"{API}/records/{{record_id}}/history")
    def record_history(record_id: str) -> Response:
        return _json(service.history(project_id, _current(), record_id))

    @app.get(f"{API}/context")
    def context(
        purpose: str = "orient",
        budget: Annotated[int, Query(ge=1, le=200_000)] = 12000,
        at_seq: Annotated[int, Query(ge=0, le=MAX_INT)] | None = None,
    ) -> Response:
        return _json(
            service.context(project_id, _current(), purpose=purpose, budget=budget, at_seq=at_seq)
        )

    @app.get(f"{API}/next")
    def next_steps(at_seq: Annotated[int, Query(ge=0, le=MAX_INT)] | None = None) -> Response:
        return _json(service.next_steps(project_id, _current(), at_seq))

    @app.get(f"{API}/notebook")
    def notebook(
        subject: str | None = None, at_seq: Annotated[int, Query(ge=0, le=MAX_INT)] | None = None
    ) -> Response:
        return _json(service.notebook(project_id, _current(), subject=subject, at_seq=at_seq))

    @app.get(f"{API}/knowledge/search")
    def search(q: Annotated[str, Query(min_length=1, max_length=500)],
               limit: Annotated[int, Query(ge=1, le=100)] = 20) -> Response:  # fmt: skip
        return _json(service.search(project_id, _current(), q, limit))

    @app.get(f"{API}/knowledge/claim-status")
    def claim_status(
        paper_id: Annotated[str, Query(max_length=80)] | None = None,
        # The Query marker sits inside the optional type, or FastAPI ignores the parameter
        # (found in CP20.3: every claim came back whatever was asked).
        claim_id: Annotated[list[str] | None, Query()] = None,
    ) -> Response:
        if claim_id is not None and (len(claim_id) > 200 or any(len(c) > 80 for c in claim_id)):
            raise PrometheosError("validation_error", "at most 200 claim IDs", {})
        return _json(
            service.claim_status(project_id, _current(), claim_ids=claim_id, paper_id=paper_id)
        )

    @app.get(f"{API}/knowledge/research-map")
    def research_map(paper_id: Annotated[str, Query(max_length=80)] | None = None) -> Response:
        return _json(service.research_map(project_id, _current(), paper_id=paper_id))

    # ----- A06.1, CP20.3: planned checks and decision cards (decision 0084) -----------------

    @app.get(f"{API}/verification/plan")
    def verification_plan() -> Response:
        return _json(service.verification_plan(project_id, _current()))

    @app.post(f"{API}/verification/propose")
    def verification_propose() -> Response:
        return _json(service.verification_propose(project_id, _current()))

    # A06.1, CP20.4 (gap audit #16): the owner's budget control. The request is the one
    # ``prometheos verify budget`` builds (``verification_budget_request``), so the browser never
    # resends the settings' local paths; the apply needs the digest the preview showed.

    @app.post(f"{API}/verification/budget/preview")
    def verification_budget_preview(body: VerificationBudgetChange, request: Request) -> Response:
        _require_owner(request)
        raw = service.verification_budget_request(project_id, body.budget.model_dump(mode="json"))
        return _json(service.review_request(project_id, raw, _current()))

    @app.post(f"{API}/verification/budget")
    def verification_budget_set(body: VerificationBudgetChange, request: Request) -> Response:
        _require_owner(request)
        if body.digest is None:
            raise PrometheosError("validation_error", "the previewed digest is required", {})
        raw = service.verification_budget_request(project_id, body.budget.model_dump(mode="json"))
        preview = service.review_request(project_id, raw, _current())
        if not preview["would_apply"] or preview["digest"] != body.digest:
            raise PrometheosError(
                "revision_conflict",
                "the budget change no longer matches the preview; review it again",
                {"digest": preview["digest"]},
            )
        secret = None
        if preview["protected_effects"]:
            grant = service.grant(
                project_id,
                operation="exact_request",
                digest=preview["digest"],
                ttl_seconds=60,
                max_uses=1,
                label="hub owner verification budget",
            )
            secret = grant.secret
        return _json(service.apply(project_id, raw, _current(), secret))

    @app.get(f"{API}/decisions/cards")
    def decision_cards() -> Response:
        return _json(service.decision_cards(project_id, _current()))

    @app.post(f"{API}/decisions/recommended/preview")
    def recommended_preview(body: RecommendedRequest, request: Request) -> Response:
        _require_owner(request)
        return _json(
            service.recommended_preview(
                project_id, _current(), body.approval_request_ids, HUB_OWNER_ACT
            )
        )

    @app.post(f"{API}/decisions/recommended/apply")
    def recommended_apply(body: RecommendedRequest, request: Request) -> Response:
        """Apply exactly the previewed set: the digest the owner saw must still match."""
        _require_owner(request)
        if body.digest is None:
            raise PrometheosError("validation_error", "the previewed digest is required", {})
        return _json(
            service.recommended_apply(
                project_id, _current(), body.approval_request_ids, body.digest, HUB_OWNER_ACT
            )
        )

    @app.get(f"{API}/knowledge/links/{{record_id}}")
    def links(record_id: str) -> Response:
        return _json(service.links(project_id, _current(), record_id))

    @app.get(f"{API}/meetings/{{meeting_id}}/reconcile")
    def reconcile(meeting_id: str) -> Response:
        return _json(service.reconcile(project_id, _current(), meeting_id))

    @app.get(f"{API}/meetings/{{meeting_id}}/agenda")
    def agenda(meeting_id: str) -> Response:
        return _json(service.agenda(project_id, _current(), meeting_id))

    @app.get(f"{API}/versions/{{version_id}}/text")
    def version_text(
        version_id: str,
        offset: Annotated[int, Query(ge=0, le=2**40)] = 0,
        limit: Annotated[int, Query(ge=1, le=200_000)] = 200_000,
    ) -> Response:
        """One page of the retained text (the whole of it unless it is longer than ``limit``)."""
        return _json(service.version_text(project_id, _current(), version_id, offset, limit))

    @app.get(f"{API}/papers/{{paper_id}}")
    def paper(paper_id: str) -> Response:
        return _json(service.paper_overview(project_id, _current(), paper_id))

    @app.get(f"{API}/papers/{{paper_id}}/check")
    def paper_check(paper_id: str) -> Response:
        """Pure readiness findings (no write, no sequence change)."""
        return _json(service.paper_check(project_id, _current(), paper_id))

    @app.get(f"{API}/research/compass")
    def research_compass(paper_id: str | None = None, limit: int = 12) -> Response:
        """Where to look next (decision 0113; a pure read)."""
        return _json(
            service.research_compass(project_id, _current(), paper_id=paper_id, limit=limit)
        )

    @app.get(f"{API}/papers/{{paper_id}}/numbers")
    def paper_numbers(paper_id: str) -> Response:
        """Where every number in the manuscript comes from (decision 0111; a pure read)."""
        return _json(service.paper_numbers(project_id, _current(), paper_id))

    @app.get(f"{API}/versions/{{version_id}}/content")
    def version_content(version_id: str, request: Request) -> Response:
        """Retained bytes for the browser's own viewer (PDF, images, plain text only).

        The document gets its own policy: it may not run script, load or frame anything,
        and text is always text/plain."""
        status_code = 200
        extra: dict[str, str] = {}
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", ""))
        ranged = None
        if match and (match.group(1) or match.group(2)):
            # Seeking in audio: only the requested slice is read (the file is verified once).
            first = int(match.group(1)) if match.group(1) else None
            last = int(match.group(2)) if match.group(2) else None
            try:
                ranged = service.version_audio_range(
                    project_id, _current(), version_id, first, last
                )
            except PrometheosError as exc:
                if exc.code == "validation_error" and exc.message == "range not satisfiable":
                    return Response(
                        status_code=416,
                        headers={"content-range": f"bytes */{exc.details.get('total', 0)}"},
                    )
                if exc.code != "unsupported":
                    raise
        if ranged is not None:
            data, total, media_type, first_byte, last_byte = ranged
            extra["accept-ranges"] = "bytes"
            extra["content-range"] = f"bytes {first_byte}-{last_byte}/{total}"
            status_code = 206
        else:
            data, media_type = service.version_content(project_id, _current(), version_id)
            if media_type.startswith("audio/"):
                extra["accept-ranges"] = "bytes"
        response = Response(data, media_type=media_type, status_code=status_code, headers=extra)
        response.headers["content-disposition"] = "inline"
        response.headers["content-security-policy"] = CONTENT_CSP.get(media_type, TEXT_CSP)
        return response

    @app.get(f"{API}/jobs")
    def jobs_list() -> Response:
        return _json(service.job_list(project_id, _current()))

    @app.get(f"{API}/inbox")
    def inbox(include_dismissed: bool = False) -> Response:
        return _json(service.inbox(project_id, _current(), include_dismissed))

    @app.get(f"{API}/tasks/{{task_id}}")
    def task_get(task_id: str) -> Response:
        return _json(service.task_get(project_id, task_id))

    @app.get(f"{API}/workflows/{{run_id}}")
    def workflow(run_id: str) -> Response:
        return _json(service.workflow_status(project_id, run_id, _current()))

    @app.get(f"{API}/skills")
    def skills_catalog() -> Response:
        return _json(service.skill_catalog())

    @app.get(f"{API}/skills/{{ref}}")
    def skill_detail(ref: str) -> Response:
        return _json(skills_ops.skill_detail(service, ref))

    @app.post(f"{API}/skills/{{ref}}/install-preview")
    def skill_install_preview(ref: str, body: InstallPreviewBody) -> Response:
        """What a project-local installation would contain, planned against an empty
        temporary folder: no browser-supplied path is touched, nothing is written to a
        project and nothing executes. Apply it with `prometheos skill install`."""
        with tempfile.TemporaryDirectory(prefix="prometheos-preview-") as tmp:
            plan = service.skill_install(ref, body.host, Path(tmp), None)["plan"]
        plan.pop("target", None)
        return _json({"plan": plan})

    @app.get(f"{API}/changes")
    async def changes(
        request: Request,
        after_seq: Annotated[int, Query(ge=0, le=MAX_INT)] = 0,
        wait: Annotated[float, Query(ge=0, le=LONG_POLL_MAX)] = LONG_POLL_MAX,
    ) -> Response:
        """Metadata-only change feed; waits up to ``wait`` seconds for a newer sequence.

        The wait reads only the shared sequence probe, and a poll whose client went away
        (the hub restarts its poll on every navigation) stops at once, so abandoned polls
        do not hold the admission gate against writes (decision 0074)."""
        deadline = anyio.current_time() + wait
        gone = False
        failure: Exception | None = None
        async with anyio.create_task_group() as group:

            async def watch() -> None:
                # Waits for the client to go. ``request.is_disconnected()`` cannot see it
                # here: its pre-cancelled receive loses the message inside the guard
                # middleware's task group.
                nonlocal gone
                while (await request.receive())["type"] != "http.disconnect":
                    pass  # the (empty) GET body; the next message is the disconnect
                gone = True
                group.cancel_scope.cancel()

            group.start_soon(watch)
            try:
                while await feed.latest() == after_seq:
                    left = deadline - anyio.current_time()
                    if left <= 0:
                        break
                    await feed.wait(min(LONG_POLL_TICK, left))
            except Exception as exc:  # raised below, outside the group, so that it is not
                failure = exc  # wrapped in an ExceptionGroup and keeps its typed envelope
            group.cancel_scope.cancel()
        if failure is not None:
            raise failure
        if gone:
            return Response(status_code=204)  # nobody reads it; no further store read
        result = await anyio.to_thread.run_sync(
            lambda: service.changes(project_id, _current(), after_seq)
        )
        if result["latest_seq"] < after_seq:
            result["reset"] = True  # cursor from another store or a restore: reload now
        return _json(result)

    # ----- writes -------------------------------------------------------------------------

    @app.post(f"{API}/transactions")
    async def transactions(request: Request) -> Response:
        raw = await request.body()
        caller = _who(request)
        return _json(await anyio.to_thread.run_sync(lambda: service.apply(project_id, raw, caller)))

    @app.post(f"{API}/decisions/preview")
    def decision_preview(body: DecisionBody, request: Request) -> Response:
        raw = json.dumps(body.request, ensure_ascii=False).encode("utf-8")
        return _json(service.review_request(project_id, raw, _who(request)))

    # ----- CP10.2: pending approvals (decision 0016) ---------------------------------------
    # Listing and reading: the owner sees the whole queue with its owner rendering; an agent
    # sees its own requests. Grant, deny and revoke are owner-channel routes (never in
    # AGENT_ROUTES); resume and withdraw act at the caller's own scope.

    @app.get(f"{API}/approvals")
    def approvals_list(
        request: Request,
        state: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> Response:
        if _is_owner(request):
            return _json(service.approval_queue(project_id, state))
        return _json(service.approval_list(project_id, _who(request), state, limit))

    @app.get(f"{API}/approvals/{{approval_id}}")
    def approval_get(approval_id: str, request: Request) -> Response:
        if _is_owner(request):
            return _json(service.approval_owner_view(project_id, approval_id))
        return _json(service.approval_get(project_id, _who(request), approval_id))

    @app.post(f"{API}/approvals/{{approval_id}}/grant")
    def approval_grant(approval_id: str, body: ApprovalDecisionBody, request: Request) -> Response:
        """The paired owner grants exactly the plan the queue showed; the grant is a
        single-use exact-plan capability held by the server. Answers the grant receipt
        (``capability_grant_receipt``; no secret exists to return)."""
        _require_owner(request)
        service.approval_decide(
            project_id, approval_id, grant=True, plan_sha256=body.plan_sha256,
            owner=HUB_OWNER_ACT, note=body.note,
        )  # fmt: skip
        return _json(service.approval_grant_receipt(project_id, approval_id))

    @app.post(f"{API}/approvals/{{approval_id}}/start")
    def approval_start(approval_id: str, body: ApprovalDecisionBody, request: Request) -> Response:
        """The paired owner starts a granted recording here, where the hub's recording banner
        shows it with a stop control (review F1). Capture runs in this hub process."""
        _require_owner(request)
        backend = capture.get_backend()
        service.approval_start_recording(
            project_id, approval_id, owner=HUB_OWNER_ACT, plan_sha256=body.plan_sha256,
            backend=backend,
        )  # fmt: skip
        # The approval_view contract: the used request, its result naming the session.
        return _json(service.approval_get(project_id, OWNER, approval_id))

    @app.post(f"{API}/approvals/{{approval_id}}/deny")
    def approval_deny(approval_id: str, body: ApprovalDecisionBody, request: Request) -> Response:
        _require_owner(request)
        return _json(
            service.approval_decide(
                project_id, approval_id, grant=False, plan_sha256=body.plan_sha256,
                owner=HUB_OWNER_ACT, note=body.note,
            )
        )  # fmt: skip

    @app.post(f"{API}/approvals/{{approval_id}}/revoke")
    def approval_revoke(approval_id: str, body: CapabilityRevoke, request: Request) -> Response:
        """Revoke an unused grant; the body names the same request (``capability_revoke``).
        Answers ``capability_revoke_receipt``."""
        _require_owner(request)
        if body.approval_request_id != approval_id:
            raise PrometheosError("validation_error", "the body names another request", {})
        revoked = service.approval_revoke(project_id, approval_id, owner=HUB_OWNER_ACT)
        receipt = CapabilityRevokeReceipt(
            revoked=True, approval_request_id=approval_id, state=revoked["state"]
        )
        return _json(receipt.model_dump(mode="json"))

    @app.post(f"{API}/approvals/{{approval_id}}/stop")
    def approval_stop(approval_id: str, request: Request, cancel: bool = False) -> Response:
        """Stop (or cancel) the recording an approved request started: the requesting agent
        or the owner (review F1)."""
        return _json(service.approval_stop(project_id, _who(request), approval_id, cancel))

    @app.post(f"{API}/approvals/{{approval_id}}/resume")
    async def approval_resume(approval_id: str, request: Request) -> Response:
        caller = _who(request)
        return _json(
            await anyio.to_thread.run_sync(
                lambda: service.approval_resume(project_id, caller, approval_id)
            )
        )

    @app.post(f"{API}/approvals/{{approval_id}}/withdraw")
    def approval_withdraw(approval_id: str, request: Request) -> Response:
        return _json(service.approval_withdraw(project_id, _who(request), approval_id))

    @app.post(f"{API}/jobs/{{job_id}}/resolve")
    def job_resolve(job_id: str, body: JobResolveBody, request: Request) -> Response:
        """Resolve an unknown slot. The paired owner decides at once (a single-use exact-plan
        capability is issued and consumed in one step); an agent's call becomes an approval
        request the owner grants in the queue."""
        if not _is_owner(request):
            params = {
                "job_id": job_id, "due_utc": body.due_utc, "resolution": body.resolution,
                "reason": body.reason,
            }  # fmt: skip
            return _json(
                service.approval_request(project_id, AGENT, "job_resolve", params, body.request_id)
            )
        digest = service.job_resolve_digest(
            project_id, job_id, body.due_utc, body.resolution, body.reason
        )
        grant = service.grant(
            project_id, operation="exact_request", digest=digest, ttl_seconds=60, max_uses=1,
            label="hub owner job resolution",
        )  # fmt: skip
        try:
            return _json(
                service.job_resolve_slot(
                    project_id,
                    _current(),
                    job_id,
                    body.due_utc,
                    body.resolution,
                    body.reason,
                    grant.secret,
                )
            )
        finally:
            service.revoke(project_id, grant.capability_id)

    @app.post(f"{API}/decisions/apply")
    def decision_apply(body: DecisionBody, request: Request) -> Response:
        """Apply exactly the previewed request: the digest the owner saw must still match."""
        _require_owner(request)  # CP10.5 round 2: every owner-only route checks it itself
        raw = json.dumps(body.request, ensure_ascii=False).encode("utf-8")
        preview = service.review_request(project_id, raw, _current())
        if not preview["would_apply"]:
            # A retry of an already committed decision replays its original result (the
            # store looks request IDs up first); anything else must be reviewed again.
            try:
                replay = service.apply(project_id, raw, _current())
            except PrometheosError:
                replay = None
            if replay is not None and replay.get("replayed"):
                return _json({"preview": preview, "result": replay})
            raise PrometheosError(
                "revision_conflict",
                "the request no longer applies; review it again",
                {"preview": preview},
            )
        if body.digest is None or body.digest != preview["digest"]:
            raise PrometheosError(
                "revision_conflict",
                "the request changed since it was previewed; review it again",
                {"digest": preview["digest"]},
            )
        secret = None
        if preview["protected_effects"]:
            grant = service.grant(
                project_id,
                operation="exact_request",
                digest=preview["digest"],
                ttl_seconds=60,
                max_uses=1,
                label="hub owner decision",
            )
            secret = grant.secret
        result = service.apply(project_id, raw, _current(), secret)
        return _json({"preview": preview, "result": result})

    @app.post(f"{API}/papers/{{paper_id}}/check/save")
    def paper_check_save(paper_id: str, body: SaveBody) -> Response:
        """Explicitly record the (agent-scope) findings as one generated notebook entry."""
        return _json(
            service.paper_check_save(project_id, paper_id, body.request_id or new_request_id())
        )

    @app.post(f"{API}/jobs/tick")
    def jobs_tick() -> Response:
        """Run due job slots now (one bounded pass) while this hub process is running."""
        return _json(service.jobs_tick(project_id))

    @app.post(f"{API}/inbox/{{notification_id}}/dismiss")
    def inbox_dismiss(notification_id: int) -> Response:
        return _json(service.inbox_dismiss(project_id, _current(), notification_id))

    @app.post(f"{API}/meetings")
    def meeting_capture(body: MeetingBody) -> Response:
        return _json(
            service.capture_meeting(
                project_id,
                _current(),
                title=body.title,
                text=body.text,
                held_at=body.held_at,
                sensitivity=body.sensitivity,
                request_id=body.request_id or new_request_id(),
            )
        )

    @app.post(f"{API}/tasks/prepare")
    def task_prepare(body: TaskPrepareBody) -> Response:
        return _json(
            service.task_prepare(
                project_id,
                purpose=body.purpose,
                budget=body.budget,
                meeting_id=body.meeting_id,
                request_id=body.request_id or new_request_id(),
            )
        )

    @app.post(f"{API}/tasks/{{task_id}}/submit")
    def task_submit(task_id: str, body: TaskSubmitBody) -> Response:
        raw = json.dumps(body.result, ensure_ascii=False).encode("utf-8")
        return _json(service.task_submit(project_id, task_id, raw, body.request_id))

    # Long operations answer an operation run at once (decision 0017); wait_seconds (at most
    # 20) lets a quick one answer with its result in the same call.

    @app.post(f"{API}/sources/plans")
    def source_plan(body: SourcePlanBody) -> Response:
        return _json(
            service.import_plan(
                project_id,
                _current(),
                mode="background",
                wait_seconds=body.wait_seconds,
                root_alias=body.root_alias,
                paths=body.paths,
                retention=body.retention,
                sensitivity=body.sensitivity,
                description=body.description,
            )
        )

    @app.post(f"{API}/sources/plans/{{plan_id}}/commit")
    def source_commit(plan_id: str, body: WaitBody | None = None) -> Response:
        wait = body.wait_seconds if body is not None else 0
        return _json(
            service.import_commit(
                project_id, plan_id, _current(), mode="background", wait_seconds=wait
            )
        )

    # ----- A05, CP17: adoption scan (decision 0039); the reads are agentops routes ------------

    @app.post(f"{API}/adoption/scans")
    def adoption_scan(body: AdoptionScanBody) -> Response:
        return _json(
            service.adoption_scan(
                project_id,
                _current(),
                root_alias=body.root_alias,
                exclusions=body.exclusions,
                limits=body.limits,
                profile=body.profile,
                mode="background",
                wait_seconds=body.wait_seconds,
            )
        )

    # ----- A02, CP11 S11.1: library import (decision 0060); the reads are agentops routes -------

    @app.post(f"{API}/library/plans")
    def library_plan(body: LibraryPlanBody) -> Response:
        return _json(
            service.library_import_plan(
                project_id,
                _current(),
                root_alias=body.root_alias,
                path=body.path,
                sensitivity=body.sensitivity,
                csv_columns=body.csv_columns,
                mode="background",
                wait_seconds=body.wait_seconds,
            )
        )

    # ----- A02, CP11 S11.5: the read-only Zotero adapter (decision 0070); the status read is an
    # agentops route -------------------------------------------------------------------------

    @app.post(f"{API}/zotero/plans")
    def zotero_plan(body: ZoteroPlanBody) -> Response:
        return _json(
            service.zotero_import_plan(
                project_id,
                _current(),
                collection=body.collection,
                item_keys=body.item_keys,
                sensitivity=body.sensitivity,
                mode="background",
                wait_seconds=body.wait_seconds,
            )
        )

    @app.post(f"{API}/zotero/plans/{{plan_id}}/commit")
    def zotero_commit(
        plan_id: str, request: Request, body: ZoteroCommitBody | None = None
    ) -> Response:
        body = body or ZoteroCommitBody()
        if not _is_owner(request):
            # Reading the owner's library into the project is the owner's decision (0070 s7).
            params: dict[str, Any] = {"plan_id": plan_id}
            if body.item_keys:
                params["item_keys"] = body.item_keys
            return _json(
                service.approval_request(project_id, AGENT, "zotero_import_commit", params,
                                         body.request_id)
            )  # fmt: skip
        return _json(
            service.zotero_import_commit(
                project_id,
                plan_id,
                OWNER,
                authority=HUB_OWNER_ACT,
                item_keys=body.item_keys,
                mode="background",
                wait_seconds=body.wait_seconds,
            )
        )

    # CP11 S11.4b (decision 0066): rule extraction from the hub answers the operation run at
    # once (the page polls it, as for imports); MCP's matrix_manage#extract waits instead.
    @app.post(f"{API}/matrix/{{matrix_id}}/extract/run")
    def matrix_extract_run(matrix_id: str, body: MatrixExtractBody) -> Response:
        return _json(
            service.matrix_extract(
                project_id,
                _current(),
                matrix_id=matrix_id,
                rules=dict(body.rules),
                mode="background",
                wait_seconds=body.wait_seconds,
            )
        )

    @app.post(f"{API}/library/plans/{{plan_id}}/commit")
    def library_commit(plan_id: str, body: WaitBody | None = None) -> Response:
        wait = body.wait_seconds if body is not None else 0
        return _json(
            service.library_import_commit(
                project_id, plan_id, _current(), mode="background", wait_seconds=wait
            )
        )

    @app.post(f"{API}/adoption/baselines/import")
    def adoption_baseline_import(body: AdoptionImportBody, request: Request) -> Response:
        # Owner channel only: the package carries owner decisions (T0), which no agent may
        # supply (decision 0045).
        _require_owner(request)
        return _json(
            service.adoption_baseline_import(project_id, body.package, authority=HUB_OWNER_ACT)
        )

    # ----- A02, CP10.3: operation runs (decision 0017) -----------------------------------------

    @app.get(f"{API}/runs")
    async def run_list(
        state: str | None = None,
        operation: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        watch_after: Annotated[int | None, Query(ge=0, le=MAX_INT)] = None,
        wait: Annotated[float, Query(ge=0, le=LONG_POLL_MAX)] = LONG_POLL_MAX,
    ) -> Response:
        """The run list. For the paired owner session it also carries the hub's run write count
        (``X-Prometheos-Run-Generation``), and ``watch_after`` makes it wait, up to ``wait``
        seconds, until a run of this hub was created or wrote (state, stage, progress) since
        that count (pushed run progress, decision 0076). The wait reads only that in-process
        count, never the store, so waiting calls never take the project's admission gate
        (decision 0074). An agent token gets the list at once and no count: the count would
        tell it that runs it may not see had changed."""
        caller = _current()
        owner = caller.scope == "owner"
        if owner and watch_after is not None:
            deadline = anyio.current_time() + wait
            while runs_mod.write_generation == watch_after and anyio.current_time() < deadline:
                await anyio.sleep(RUN_WATCH_TICK)
            if runs_mod.write_generation != watch_after:
                # A status write is counted before its transaction commits: let it land first.
                await anyio.sleep(RUN_WATCH_SETTLE)
        generation = runs_mod.write_generation
        view = await anyio.to_thread.run_sync(
            lambda: service.run_list(
                project_id, caller, state=state, operation=operation, limit=limit
            )
        )
        response = _json(view)
        if owner:
            response.headers["X-Prometheos-Run-Generation"] = str(generation)
        return response

    @app.get(f"{API}/runs/{{run_id}}")
    def run_get(run_id: str) -> Response:
        return _json(service.run_get(project_id, _current(), run_id))

    @app.post(f"{API}/runs/{{run_id}}/cancel")
    def run_cancel(run_id: str) -> Response:
        return _json(service.run_cancel(project_id, _current(), run_id))

    @app.post(f"{API}/runs/{{run_id}}/resume")
    def run_resume(run_id: str, body: RunResumeBody | None = None) -> Response:
        body = body or RunResumeBody()
        return _json(
            service.run_resume(
                project_id, _current(), run_id, budget_amount=body.budget_amount,
                mode="background", wait_seconds=body.wait_seconds,
            )
        )  # fmt: skip

    @app.post(f"{API}/runs/{{run_id}}/acknowledge")
    def run_acknowledge(run_id: str, body: RunAcknowledgeBody | None = None) -> Response:
        note = body.note if body is not None else None
        return _json(service.run_acknowledge(project_id, _current(), run_id, note))

    # ----- CP9: LaTeX workspace, engine and Overleaf Git ------------------------------------
    # The paired session is the owner at this computer (module docstring), so it may install
    # the engine after showing the plan and push an exact plan it previewed.

    @app.get(f"{API}/tex/engine")
    def tex_engine() -> Response:
        return _json(
            {
                "status": service.tex_engine_status(redact=True),
                "plan": service.tex_engine_plan(redact=True),
            }
        )

    @app.post(f"{API}/tex/engine/install")
    def tex_engine_install(body: EngineInstallBody, request: Request) -> Response:
        if not _is_owner(request):  # an agent requests it (approval_required)
            return _json(
                service.approval_request(
                    project_id, AGENT, "tex_engine_install", {"digest": body.digest},
                    body.request_id,
                )
            )  # fmt: skip
        return _json(
            service.tex_engine_install(
                body.digest, redact=True, authority=HUB_OWNER_ACT, project_id=project_id,
                caller=OWNER, mode="background", wait_seconds=body.wait_seconds,
            )
        )  # fmt: skip

    @app.post(f"{API}/papers/{{paper_id}}/workspace/bind")
    def workspace_bind(paper_id: str, body: WorkspaceBindBody) -> Response:
        return _json(
            service.workspace_bind(project_id, _current(), paper_id, body.root_alias, body.main)
        )

    @app.get(f"{API}/papers/{{paper_id}}/workspace/files")
    def workspace_files(paper_id: str) -> Response:
        return _json(service.workspace_files(project_id, _current(), paper_id))

    @app.get(f"{API}/papers/{{paper_id}}/workspace/file")
    def workspace_file(
        paper_id: str, path: Annotated[str, Query(min_length=1, max_length=1024)]
    ) -> Response:
        return _json(service.workspace_read(project_id, _current(), paper_id, path))

    @app.put(f"{API}/papers/{{paper_id}}/workspace/file")
    def workspace_write(paper_id: str, body: WorkspaceWriteBody) -> Response:
        return _json(
            service.workspace_write(
                project_id, _current(), paper_id, body.path, body.text, body.base_sha256
            )
        )

    @app.post(f"{API}/papers/{{paper_id}}/workspace/snapshot")
    def workspace_snapshot(paper_id: str, body: WorkspaceSnapshotBody) -> Response:
        return _json(service.workspace_snapshot(project_id, _current(), paper_id, body.paths))

    @app.get(f"{API}/papers/{{paper_id}}/workspace/structure")
    def workspace_structure(paper_id: str) -> Response:
        return _json(service.workspace_structure(project_id, _current(), paper_id))

    @app.post(f"{API}/papers/{{paper_id}}/workspace/insert")
    def workspace_insert(paper_id: str, body: WorkspaceInsertBody) -> Response:
        return _json(
            service.workspace_insert(
                project_id, _current(), paper_id, body.kind, body.version_id, body.bib_file
            )
        )

    @app.post(f"{API}/papers/{{paper_id}}/compile")
    def paper_compile(paper_id: str, body: CompileBody) -> Response:
        return _json(
            service.paper_compile(
                project_id, _current(), paper_id, fetch=body.fetch, mode="background",
                wait_seconds=body.wait_seconds,
            )
        )  # fmt: skip

    @app.get(f"{API}/papers/{{paper_id}}/compile")
    def paper_compile_status(paper_id: str) -> Response:
        return _json(service.paper_compile_status(project_id, _current(), paper_id))

    @app.get(f"{API}/papers/{{paper_id}}/synctex/forward")
    def synctex_forward(
        paper_id: str,
        file: Annotated[str, Query(min_length=1, max_length=1024)],
        line: Annotated[int, Query(ge=1, le=10_000_000)],
        pdf_version: str | None = None,
    ) -> Response:
        return _json(
            service.synctex_forward(project_id, _current(), paper_id, file, line, pdf_version)
        )

    @app.get(f"{API}/papers/{{paper_id}}/synctex/inverse")
    def synctex_inverse(
        paper_id: str,
        page: Annotated[int, Query(ge=1, le=100_000)],
        x: Annotated[float, Query(ge=-100_000, le=100_000)],
        y: Annotated[float, Query(ge=-100_000, le=100_000)],
        pdf_version: str | None = None,
    ) -> Response:
        return _json(
            service.synctex_inverse(project_id, _current(), paper_id, page, x, y, pdf_version)
        )

    @app.get(f"{API}/papers/{{paper_id}}/overleaf")
    def overleaf_status(paper_id: str) -> Response:
        return _json(service.overleaf_status(project_id, _current(), paper_id))

    @app.post(f"{API}/papers/{{paper_id}}/overleaf/link")
    def overleaf_link(paper_id: str, body: OverleafLinkBody) -> Response:
        return _json(
            service.overleaf_link(
                project_id, _current(), paper_id, body.url, body.root_alias, body.folder, body.main
            )
        )

    @app.post(f"{API}/papers/{{paper_id}}/overleaf/pull/preview")
    def overleaf_pull_preview(paper_id: str, body: PreviewBody) -> Response:
        return _json(service.overleaf_pull_preview(project_id, _current(), paper_id, body.fetch))

    @app.post(f"{API}/papers/{{paper_id}}/overleaf/pull/apply")
    def overleaf_pull_apply(paper_id: str, body: DigestBody) -> Response:
        return _json(service.overleaf_pull_apply(project_id, _current(), paper_id, body.digest))

    @app.post(f"{API}/papers/{{paper_id}}/overleaf/commit")
    def overleaf_commit(paper_id: str, body: CommitBody) -> Response:
        return _json(service.overleaf_commit(project_id, _current(), paper_id, body.message))

    @app.post(f"{API}/papers/{{paper_id}}/overleaf/push/preview")
    def overleaf_push_preview(paper_id: str) -> Response:
        return _json(service.overleaf_push_preview(project_id, _current(), paper_id))

    @app.post(f"{API}/papers/{{paper_id}}/overleaf/push/apply")
    def overleaf_push_apply(paper_id: str, body: DigestBody, request: Request) -> Response:
        """Push exactly the plan the owner previewed: a single-use capability bound to its
        digest is issued and consumed in one step; a changed plan is refused. An agent's call
        becomes an approval request (approval_required)."""
        if not _is_owner(request):
            params = {"paper_id": paper_id, "digest": body.digest}
            return _json(
                service.approval_request(
                    project_id, AGENT, "overleaf_push", params, body.request_id
                )
            )
        grant = service.overleaf_push_grant(project_id, body.digest, "hub owner push")
        try:
            return _json(
                service.overleaf_push(project_id, _current(), paper_id, body.digest, grant.secret)
            )
        finally:
            service.revoke(project_id, grant.capability_id)

    # ----- CP6: audio, recording, transcription ----------------------------------------------

    def _background(target: Any, *args: Any) -> None:
        def body() -> None:
            try:
                target(*args)
            except PrometheosError:
                pass  # the run/session row carries the state and reason; the UI polls it

        threading.Thread(target=body, daemon=True).start()

    @app.get(f"{API}/audio/devices")
    def audio_devices() -> Response:
        """Capture devices of the selected backend; records nothing."""
        return _json(service.capture_devices())

    @app.get(f"{API}/recordings")
    def recordings(include_done: bool = False) -> Response:
        return _json(service.recording_list(project_id, _current(), include_done))

    @app.get(f"{API}/recordings/{{session_id}}")
    def recording(session_id: str) -> Response:
        return _json(service.recording_status(project_id, _current(), session_id))

    @app.post(f"{API}/recordings")
    def recording_start(body: RecordingStartBody, request: Request) -> Response:
        """Start a visible recording session (the reminder must be acknowledged). Capture runs
        in this process; the page polls the session. An agent's call becomes an approval
        request: consent stays the owner's act at this computer (decision 0015 R15)."""
        if not _is_owner(request):
            params = {
                "title": body.title, "device_ids": list(body.device_ids),
                "sensitivity": body.sensitivity, "segment_seconds": body.segment_seconds,
            }  # fmt: skip
            return _json(
                service.approval_request(
                    project_id, AGENT, "recording_start", params, body.request_id
                )
            )
        backend = capture.get_backend()
        session = service.recording_start(
            project_id,
            _current(),
            recorder.SessionRequest(
                body.title,
                tuple(body.device_ids),
                body.sensitivity,
                body.segment_seconds,
                body.consent_acknowledged,
            ),
            backend,
            authority=HUB_OWNER_ACT,
        )
        _background(service.recording_run, project_id, session["session_id"], backend)
        return _json(session)

    @app.post(f"{API}/recordings/{{session_id}}/{{action}}")
    def recording_control(session_id: str, action: str, confirm: bool = False) -> Response:
        confirmed = confirm
        if action == "recover":
            return _json(service.recording_recover(project_id, _current(), session_id))
        if action == "discard":
            return _json(
                service.recording_discard(project_id, _current(), session_id, confirm=confirmed)
            )
        return _json(service.recording_control(project_id, _current(), session_id, action))

    @app.post(f"{API}/audio/import")
    def audio_import(body: AudioImportBody) -> Response:
        return _json(
            service.audio_import(
                project_id,
                _current(),
                root_alias=body.root_alias,
                path=body.path,
                sensitivity=body.sensitivity,
                title=body.title,
                mode="background",
                wait_seconds=body.wait_seconds,
            )
        )

    @app.get(f"{API}/audio/models")
    def audio_models() -> Response:
        """Installed speech models and the pure plan for the default one (size, licence,
        revision, location). Installing stays an explicit command: no download from here."""
        return _json(
            {
                "status": service.audio_models_status(),
                "plan": service.audio_models_plan(),
                "synthetic_enabled": os.environ.get(engines.SYNTHETIC_ENV) == "1",
            }
        )

    @app.get(f"{API}/audio/transcriptions")
    def transcriptions(version_id: str | None = None) -> Response:
        return _json(service.transcription_list(project_id, _current(), version_id))

    @app.get(f"{API}/audio/transcriptions/{{run_id}}")
    def transcription(run_id: str) -> Response:
        return _json(service.transcription_status(project_id, _current(), run_id))

    @app.post(f"{API}/audio/transcriptions")
    def transcription_start(body: TranscribeBody) -> Response:
        config = transcribe.RunConfig(body.engine, body.model, body.language)
        run = service.transcribe_start(project_id, _current(), body.version_id, config)
        return _json(service.transcribe_background(project_id, run))

    @app.post(f"{API}/audio/transcriptions/{{run_id}}/cancel")
    def transcription_cancel(run_id: str) -> Response:
        return _json(service.transcription_cancel(project_id, _current(), run_id))

    @app.get(f"{API}/transcripts/{{version_id}}")
    def transcript(version_id: str) -> Response:
        return _json(service.transcript_get(project_id, _current(), version_id))

    @app.post(f"{API}/transcripts/{{version_id}}/corrections")
    def transcript_correct(version_id: str, body: CorrectionBody) -> Response:
        edits = [
            transcribe.Edit(e.segment_id, e.text, e.speaker_label, e.uncertain) for e in body.edits
        ]
        return _json(
            service.transcript_correct(
                project_id, _current(), version_id, edits, body.request_id or new_request_id()
            )
        )

    @app.post(f"{API}/meetings/from-audio")
    def meeting_from_audio(body: MeetingAudioBody) -> Response:
        return _json(
            service.capture_meeting_from_transcript(
                project_id,
                _current(),
                transcript_version_id=body.transcript_version_id,
                audio_version_id=body.audio_version_id,
                title=body.title,
                held_at=body.held_at,
                request_id=body.request_id or new_request_id(),
            )
        )

    # ----- CP6.3: provider status, egress preview, owner-approved run ---------------------------

    @app.get(f"{API}/provider")
    def provider_status() -> Response:
        return _json(service.provider_status(project_id, _current()))

    @app.get(f"{API}/provider/calls")
    def provider_calls() -> Response:
        return _json(service.provider_calls(project_id, _current()))

    @app.post(f"{API}/provider/preview")
    def provider_preview(body: ProviderPreviewBody) -> Response:
        return _json(
            service.provider_preview(
                project_id,
                _current(),
                purpose=body.purpose,
                meeting_id=body.meeting_id,
                context_budget=body.context_budget,
                include_restricted=body.include_restricted,
            )
        )

    @app.post(f"{API}/provider/run")
    def provider_run(body: ProviderRunBody, request: Request) -> Response:
        """The owner sends exactly the previewed request: the digest they saw must still match.
        The session issues the single-use capabilities itself, like decisions/apply. An
        agent's call becomes an approval request (egress needs the owner)."""
        if not _is_owner(request):
            if body.include_restricted:
                raise PrometheosError(
                    "policy_denied", "previewing restricted inputs needs owner scope", {}
                )
            params = {
                "purpose": body.purpose, "meeting_id": body.meeting_id,
                "context_budget": body.context_budget, "digest": body.digest,
            }  # fmt: skip
            return _json(
                service.approval_request(project_id, AGENT, "provider_run", params, body.request_id)
            )
        shown = service.provider_preview(
            project_id,
            _current(),
            purpose=body.purpose,
            meeting_id=body.meeting_id,
            context_budget=body.context_budget,
            include_restricted=body.include_restricted,
        )
        if body.digest != shown["digest"]:
            raise PrometheosError(
                "revision_conflict",
                "what would be sent changed since it was previewed; preview it again",
                {"digest": shown["digest"]},
            )
        run_grant = service.grant(
            project_id, operation="provider_run", digest=shown["digest"], ttl_seconds=60,
            max_uses=1, label="hub owner provider run",
        )  # fmt: skip
        egress = None
        if shown["needs_egress_grant"]:
            if not body.confirm_restricted:
                raise PrometheosError(
                    "policy_denied",
                    "confirm that restricted records may be sent to this provider",
                    {"needs_egress_grant": True},
                )
            egress = service.grant(
                project_id, operation="provider_egress",
                digest=shown["transmits"]["payload_sha256"], ttl_seconds=60, max_uses=1,
                label="hub owner restricted egress",
            ).secret  # fmt: skip
        return _json(
            service.provider_run(
                project_id,
                purpose=body.purpose,
                meeting_id=body.meeting_id,
                context_budget=body.context_budget,
                include_restricted=body.include_restricted,
                approved_digest=body.digest,
                capability=run_grant.secret,
                egress_capability=egress,
                acknowledge_unknown=body.acknowledge_unknown,
                caller=OWNER,
                mode="background",
                wait_seconds=body.wait_seconds,
            )
        )

    @app.post(f"{API}/provider/configure/preview")
    def provider_configure_preview(body: ProviderConfigBody) -> Response:
        revision = service.provider_status(project_id, _current())["revision"]
        return _json(service.provider_configure_preview(project_id, body.config, revision))

    @app.post(f"{API}/provider/configure")
    def provider_configure(body: ProviderConfigBody, request: Request) -> Response:
        if not _is_owner(request):  # an agent requests it (approval_required)
            revision = body.revision
            if revision is None:
                revision = service.provider_status(project_id, AGENT)["revision"]
            params = {"config": body.config, "revision": revision}
            return _json(
                service.approval_request(
                    project_id, AGENT, "provider_configure", params, body.request_id
                )
            )
        revision = service.provider_status(project_id, _current())["revision"]
        shown = service.provider_configure_preview(project_id, body.config, revision)
        if body.digest != shown["digest"]:
            raise PrometheosError(
                "revision_conflict",
                "the configuration changed since it was previewed; review it again",
                {"digest": shown["digest"]},
            )
        grant = service.grant(
            project_id, operation="provider_configure", digest=shown["digest"], ttl_seconds=60,
            max_uses=1, label="hub owner provider configuration",
        )  # fmt: skip
        return _json(service.provider_configure(project_id, body.config, revision, grant.secret))

    # ----- CP10.5: the maintenance service (no project database; decision 0021) -----------------
    # Served in both modes. An agent token reaches the agent routes (inspect, verify, restore,
    # its own requests); the paired owner session also switches, migrates and backs up directly
    # and grants or denies requests (owner-channel routes, never in AGENT_ROUTES).

    @app.get(f"{API}/maintenance/inspect")
    async def maintenance_inspect(
        request: Request, quarantine_id: str | None = None, refresh: bool = False
    ) -> Response:
        caller = _who(request)
        return _json(
            await anyio.to_thread.run_sync(
                lambda: service.maintenance_inspect(
                    project_id, caller, quarantine_id, refresh=refresh
                )
            )
        )

    @app.post(f"{API}/maintenance/verify")
    async def maintenance_verify(body: MaintenanceVerifyBody) -> Response:
        return _json(
            await anyio.to_thread.run_sync(
                lambda: service.maintenance_verify(project_id, body.backup)
            )
        )

    @app.post(f"{API}/maintenance/restore")
    async def maintenance_restore(body: MaintenanceRestoreBody, request: Request) -> Response:
        """Restore into a new store. The paired owner may name the location (an absolute path
        or a home label); an agent's restore always lands in a new folder PrometheOS creates
        under the home, and naming one is refused (round 2, F1)."""
        if _is_owner(request):
            return _json(
                await anyio.to_thread.run_sync(
                    lambda: service.maintenance_restore(
                        project_id, body.backup, body.into, owner=HUB_OWNER_ACT
                    )
                )
            )
        into = body.into  # refused by the service unless it is None
        return _json(
            await anyio.to_thread.run_sync(
                lambda: service.maintenance_restore(project_id, body.backup, into)
            )
        )

    @app.post(f"{API}/maintenance/journal-recovery")
    async def maintenance_journal_recover() -> Response:
        """Recover a hot rollback journal into a new store (via a verified backup); never in
        place (round 2, F3)."""
        return _json(
            await anyio.to_thread.run_sync(lambda: service.maintenance_journal_recover(project_id))
        )

    @app.post(f"{API}/maintenance/switch")
    async def maintenance_switch(body: MaintenanceSwitchBody, request: Request) -> Response:
        """The paired owner switches at once; an agent's call is a maintenance request
        (approval_required) held in the per-user registry."""
        if _is_owner(request):
            return _json(
                await anyio.to_thread.run_sync(
                    lambda: service.maintenance_switch(
                        project_id, OWNER, body.backup, body.target, owner=HUB_OWNER_ACT
                    )
                )
            )
        caller = _who(request)
        return _json(
            await anyio.to_thread.run_sync(
                lambda: service.maintenance_switch(
                    project_id, caller, body.backup, body.target, request_id=body.request_id
                )
            )
        )

    @app.post(f"{API}/maintenance/migrate")
    async def maintenance_migrate(body: RequestIdBody, request: Request) -> Response:
        if _is_owner(request):
            return _json(
                await anyio.to_thread.run_sync(
                    lambda: service.migrate(project_id, authority=HUB_OWNER_ACT)
                )
            )
        caller = _who(request)
        return _json(
            await anyio.to_thread.run_sync(
                lambda: service.maintenance_migrate_request(project_id, caller, body.request_id)
            )
        )

    @app.post(f"{API}/maintenance/backups")
    async def backup_create(body: RequestIdBody, request: Request) -> Response:
        """A verified whole-store backup: the paired owner (full-data scope) makes it; an
        agent's call is an approval request (approval_required)."""
        if _is_owner(request):
            full = Caller(actor="hub-owner", scope="owner", full_data=True)
            made = await anyio.to_thread.run_sync(lambda: service.backup(project_id, None, full))
            return _json(maintenance_mod.Maintenance(service).created_view(made))
        caller = _who(request)
        return _json(
            await anyio.to_thread.run_sync(
                lambda: service.approval_request(
                    project_id, caller, "backup_create", {}, body.request_id
                )
            )
        )

    @app.get(f"{API}/maintenance/approvals")
    def maintenance_queue(request: Request) -> Response:
        """Owner queue polling reads only the registry, even while inspect is still running."""
        _require_owner(request)
        return _json(service.maintenance_queue(project_id))

    @app.get(f"{API}/maintenance/approvals/{{approval_id}}")
    def maintenance_approval_get(approval_id: str, request: Request) -> Response:
        return _json(service.maintenance_approval_get(project_id, _who(request), approval_id))

    @app.post(f"{API}/maintenance/approvals/{{approval_id}}/resume")
    async def maintenance_resume(approval_id: str, request: Request) -> Response:
        caller = _who(request)
        return _json(
            await anyio.to_thread.run_sync(
                lambda: service.maintenance_resume(project_id, caller, approval_id)
            )
        )

    @app.post(f"{API}/maintenance/approvals/{{approval_id}}/withdraw")
    def maintenance_withdraw(approval_id: str, request: Request) -> Response:
        return _json(service.maintenance_withdraw(project_id, _who(request), approval_id))

    @app.post(f"{API}/maintenance/approvals/{{approval_id}}/grant")
    def maintenance_grant(
        approval_id: str, body: ApprovalDecisionBody, request: Request
    ) -> Response:
        """The paired owner grants exactly the maintenance plan the recovery screen showed;
        the grant is held in the registry and used once by the requester's resume."""
        _require_owner(request)
        return _json(
            service.maintenance_decide(
                project_id, approval_id, grant=True, plan_sha256=body.plan_sha256,
                owner=HUB_OWNER_ACT,
            )
        )  # fmt: skip

    @app.post(f"{API}/maintenance/approvals/{{approval_id}}/deny")
    def maintenance_deny(
        approval_id: str, body: ApprovalDecisionBody, request: Request
    ) -> Response:
        _require_owner(request)
        return _json(
            service.maintenance_decide(
                project_id, approval_id, grant=False, plan_sha256=body.plan_sha256,
                owner=HUB_OWNER_ACT,
            )
        )  # fmt: skip

    # ----- A02, CP10.6: parity completion (decision 0022) -----------------------------------
    # Owner-or-agent routes with a protected effect: the paired owner acts at once with its own
    # act (as for the engine install above); an agent token gets an approval request.

    @app.post(f"{API}/skills/{{ref}}/trust")
    def skill_trust(ref: str, body: SkillTrustBody, request: Request) -> Response:
        if not _is_owner(request):
            return _json(
                service.approval_request(
                    project_id, AGENT, "skill_trust", {"ref": ref}, body.request_id
                )
            )
        current = service.catalog.get(ref)["package_digest"]
        if body.package_digest != current:
            raise PrometheosError(
                "revision_conflict",
                "the package changed since you reviewed it; review it again",
                {"package_digest": current},
            )
        return _json(service.skill_trust(ref, authority=HUB_OWNER_ACT))

    @app.post(f"{API}/audio/models/install")
    def audio_models_install(body: ModelInstallBody, request: Request) -> Response:
        if not _is_owner(request):
            params = {"plan_sha256": body.plan_sha256, "model": body.model}
            return _json(
                service.approval_request(
                    project_id, AGENT, "audio_models_install", params, body.request_id
                )
            )
        return _json(
            service.audio_models_install(
                body.model, body.plan_sha256, authority=HUB_OWNER_ACT, project_id=project_id,
                caller=OWNER, mode="background", wait_seconds=body.wait_seconds,
            )
        )  # fmt: skip

    @app.post(f"{API}/audio/models/remove")
    def audio_models_remove(body: ModelRemoveBody, request: Request) -> Response:
        if not _is_owner(request):
            return _json(
                service.approval_request(
                    project_id, AGENT, "audio_models_remove", {"model": body.model},
                    body.request_id,
                )
            )  # fmt: skip
        return _json(service.audio_models_remove(body.model, authority=HUB_OWNER_ACT))

    @app.post(f"{API}/retention/purge/{{plan_id}}/commit")
    def retention_purge_commit(plan_id: str, body: PurgeCommitBody, request: Request) -> Response:
        if not _is_owner(request):
            return _json(
                service.approval_request(
                    project_id, AGENT, "retention_purge_commit", {"plan_id": plan_id},
                    body.request_id,
                )
            )  # fmt: skip
        plan = service.purge_plan_get(project_id, plan_id, OWNER)
        if body.plan_sha256 != plan["plan_sha256"]:
            raise PrometheosError(
                "revision_conflict",
                "the purge plan changed; review it again",
                {"plan_sha256": plan["plan_sha256"]},
            )
        grant = service.grant(
            project_id, operation="purge", digest=plan["plan_sha256"], ttl_seconds=60,
            max_uses=1, label="hub owner purge",
        )  # fmt: skip
        return _json(service.purge_commit(project_id, plan_id, grant.secret))

    @app.delete(f"{API}/provider/secrets")
    def provider_secret_delete(
        reference: Annotated[str, Query(min_length=1, max_length=64)], request: Request
    ) -> Response:
        _require_owner(request)
        grant = service.grant(
            project_id, operation="provider_configure",
            digest=service.provider_secret_digest("delete", reference), ttl_seconds=60,
            max_uses=1, label="hub owner credential",
        )  # fmt: skip
        return _json(service.provider_secret_delete(project_id, reference, grant.secret))

    # Everything else the registry declares for CP10.6 comes from the agent operation table:
    # the same parameter models as the MCP family tools, validated the same way.

    def _agent_context(request: Request) -> agentops.Ctx:
        paired = _is_owner(request)
        return agentops.Ctx(
            service,
            project_id,
            _who(request),
            owner=paired,
            transport="http",
            authority=HUB_OWNER_ACT if paired else None,
        )

    def _add_agent_route(operation_: agentops.AgentOp) -> None:
        assert operation_.http is not None
        method, _, path = operation_.http.partition(" ")
        in_path = set(re.findall(r"\{(\w+)\}", path))
        strict = method not in ("GET", "DELETE")
        if strict:
            raw_body_routes.add(f"{method} {path}")

        async def endpoint(request: Request) -> Response:
            data: dict[str, Any] = {}
            if strict:
                raw = await request.body()
                if raw.strip():
                    try:
                        loaded = loads_strict(raw)
                    except ValueError as exc:
                        raise PrometheosError(
                            "validation_error", f"not valid JSON: {exc}"[:300], {}
                        ) from exc
                    if not isinstance(loaded, dict):
                        raise PrometheosError(
                            "validation_error", "the request body must be an object", {}
                        )
                    data.update(loaded)
            else:
                data.update(request.query_params.items())
            for key, value in request.path_params.items():
                if key in data and data[key] != value:
                    raise PrometheosError(
                        "validation_error",
                        "the body and the path name different values",
                        {"name": key},
                    )
                data[key] = value
            ctx = _agent_context(request)
            result = await anyio.to_thread.run_sync(
                lambda: agentops.execute(operation_, ctx, data, strict=strict)
            )
            return _json(result)

        schema = _inline(operation_.params.model_json_schema())
        extra: dict[str, Any]
        if strict:
            extra = {
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": schema}},
                }
            }
        else:
            required = set(schema.get("required", []))
            extra = {
                "parameters": [
                    {
                        "name": name,
                        "in": "path" if name in in_path else "query",
                        "required": name in in_path or name in required,
                        "schema": prop,
                    }
                    for name, prop in schema.get("properties", {}).items()
                ]
            }
        app.add_api_route(
            path,
            endpoint,
            methods=[method],
            name=operation_.id,
            summary=operation_.summary,
            openapi_extra=extra,
            response_class=Response,
        )

    agentops.load()
    for operation_ in agentops.OPS.values():
        if operation_.http is not None:
            _add_agent_route(operation_)

    # ----- web assets ---------------------------------------------------------------------

    static = static_dir or _static_dir()  # static_dir: development/test builds only

    @app.get("/sw.js", include_in_schema=False)
    def service_worker_script() -> Response:
        script = service_worker(static)
        if script is None:
            raise PrometheosError("not_found", "no hub build is shipped", {})
        return Response(
            script,
            media_type="text/javascript; charset=utf-8",
            headers={"cache-control": "no-cache"},
        )

    @app.get("/{path:path}", include_in_schema=False)
    def assets(path: str) -> Response:
        if path.startswith("api/"):
            raise PrometheosError("not_found", "unknown API route", {})
        candidate = (static / path).resolve() if path else None
        if (
            candidate is not None
            and candidate.is_file()
            and candidate.is_relative_to(static.resolve())
        ):
            return FileResponse(candidate, media_type=asset_media_type(candidate.name))
        index = static / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html; charset=utf-8")
        return HTMLResponse(
            "<!doctype html><title>PrometheOS hub</title><p>Hub assets are not built.</p>",
            status_code=503,
        )

    return app
