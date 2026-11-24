"""Audio devices, transcription list, recording status, listing and control (decision 0022).

MCP family tools ``audio_read`` and ``recording_manage``. Starting a recording stays the
owner's act at the computer: the agent's ``recording_start`` is an approval request, granted in
the hub queue (decision 0015 R15, decision 0016). Pausing, resuming, stopping, cancelling,
recovering and discarding follow decision 0013's creator-rank rules (the service enforces
them: a session started at a higher rank reads as absent to an agent).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from prometheos.agentops import Ctx, Params, RecordId, family, operation

family(
    "audio_read",
    "read",
    "Audio: capture devices of the selected backend, transcription runs, recording sessions "
    "that are running or recoverable, and one session's state. Nothing is recorded.",
)
family(
    "recording_manage",
    "destructive",
    "Control a recording that is already running or interrupted: pause, resume, stop (save), "
    "cancel (segments stay recoverable), recover (register what was recorded) or discard "
    "(delete the segments for good; needs confirm). Starting a recording needs the owner: use "
    "recording_start.",
)

SessionId = Annotated[str, Field(min_length=1, max_length=80)]


class NoParams(Params):
    pass


@operation(
    "audio_devices",
    NoParams,
    "Capture devices of the selected backend; records nothing",
    tool="audio_read",
    sub="devices",
)
def audio_devices(ctx: Ctx, p: NoParams) -> Any:
    return ctx.service.capture_devices()


class TranscriptionListParams(Params):
    version_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None


@operation(
    "transcription_list",
    TranscriptionListParams,
    "Transcription runs, optionally of one audio version",
    tool="audio_read",
    sub="transcriptions",
)
def transcription_list(ctx: Ctx, p: TranscriptionListParams) -> Any:
    return ctx.service.transcription_list(ctx.project_id, ctx.caller, p.version_id)


class RecordingListParams(Params):
    include_done: bool = False


@operation(
    "recording_list",
    RecordingListParams,
    "Recording sessions that are running or recoverable (restricted ones need owner scope)",
    tool="audio_read",
    sub="recordings",
)
def recording_list(ctx: Ctx, p: RecordingListParams) -> Any:
    return ctx.service.recording_list(ctx.project_id, ctx.caller, p.include_done)


class SessionParams(Params):
    session_id: SessionId


@operation(
    "recording_status",
    SessionParams,
    "One session: state, elapsed time, level, devices and reason",
    tool="audio_read",
    sub="recording_status",
)
def recording_status(ctx: Ctx, p: SessionParams) -> Any:
    return ctx.service.recording_status(ctx.project_id, ctx.caller, p.session_id)


class ControlParams(SessionParams):
    action: Literal["pause", "resume", "stop", "cancel"]


@operation(
    "recording_control",
    ControlParams,
    "Pause, resume, stop (save) or cancel a recording; cancelled segments stay recoverable",
    tool="recording_manage",
    sub="control",
)
def recording_control(ctx: Ctx, p: ControlParams) -> Any:
    return ctx.service.recording_control(ctx.project_id, ctx.caller, p.session_id, p.action)


@operation(
    "recording_recover",
    SessionParams,
    "Register what an interrupted, cancelled or abandoned session recorded",
    tool="recording_manage",
    sub="recover",
)
def recording_recover(ctx: Ctx, p: SessionParams) -> Any:
    return ctx.service.recording_recover(ctx.project_id, ctx.caller, p.session_id)


class DiscardParams(SessionParams):
    confirm: bool = False


@operation(
    "recording_discard",
    DiscardParams,
    "Delete an unregistered session's segments (cannot be undone; needs confirm)",
    tool="recording_manage",
    sub="discard",
)
def recording_discard(ctx: Ctx, p: DiscardParams) -> Any:
    return ctx.service.recording_discard(ctx.project_id, ctx.caller, p.session_id, p.confirm)


class SpanParams(Params):
    version_id: RecordId
    start_ms: Annotated[int, Field(ge=0, le=2**40)]
    end_ms: Annotated[int, Field(ge=0, le=2**40)]


@operation(
    "audio_span",
    SpanParams,
    "The transcript text a locator would cite for a time span",
    http="GET /api/v1/transcripts/{version_id}/span",
)
def audio_span(ctx: Ctx, p: SpanParams) -> Any:
    return ctx.service.audio_span(ctx.project_id, ctx.caller, p.version_id, p.start_ms, p.end_ms)
