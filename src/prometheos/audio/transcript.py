"""Transcript payload v1 (spec/03) and the timestamp-locator rules built on it.

A transcript is stored as the canonical JSON of ``{schema_version:1, segments:[...]}`` in a
version whose ``payload_schema`` is ``transcript_v1``. An evidence reference into it uses an
audio locator (milliseconds). The quoted text of a span is the text of every segment that
overlaps it, joined by a newline; its SHA-256 is the optional ``quote_sha256``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError, model_validator

from prometheos.domain.canonical import CanonicalJSONError, dumps_canonical, loads_internal
from prometheos.domain.models import MAX_INT, ExactlyOne, PrometheosError, StrictModel

MAX_SEGMENTS = 100_000
SegmentId = Annotated[str, StringConstraints(pattern=r"^seg-[0-9]{5,6}$")]


class Segment(StrictModel):
    id: SegmentId
    start_ms: int = Field(ge=0, le=MAX_INT)
    end_ms: int = Field(ge=0, le=MAX_INT)
    text: Annotated[str, StringConstraints(max_length=5000)]
    speaker_label: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None
    confidence: float | None = Field(ge=0.0, le=1.0)
    uncertain: bool

    @model_validator(mode="after")
    def _ordered(self) -> Segment:
        if self.end_ms < self.start_ms:
            raise ValueError("segment end_ms precedes start_ms")
        return self


class TranscriptV1(StrictModel):
    schema_version: ExactlyOne
    segments: list[Segment] = Field(max_length=MAX_SEGMENTS)

    @model_validator(mode="after")
    def _unique_ids(self) -> TranscriptV1:
        ids = [s.id for s in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("segment ids are unique")
        return self

    @property
    def duration_ms(self) -> int:
        return max((s.end_ms for s in self.segments), default=0)


@dataclass(frozen=True)
class RawSegment:
    """One engine result before IDs are assigned."""

    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
    uncertain: bool = False
    speaker_label: str | None = None


def parse_payload(data: bytes) -> TranscriptV1:
    try:
        document = loads_internal(data)
        return TranscriptV1.model_validate(document, strict=True)
    except (CanonicalJSONError, ValidationError, ValueError) as exc:
        raise PrometheosError(
            "validation_error", "not a transcript_v1 payload", {"detail": str(exc)[:300]}
        ) from exc


def dump_payload(transcript: TranscriptV1) -> bytes:
    return dumps_canonical(transcript.model_dump(mode="json"))


def build_payload(raw: list[RawSegment]) -> TranscriptV1:
    ordered = sorted(raw, key=lambda s: (s.start_ms, s.end_ms))
    segments = [
        Segment(
            id=f"seg-{index:05d}",
            start_ms=s.start_ms,
            end_ms=max(s.start_ms, s.end_ms),
            text=s.text,
            speaker_label=s.speaker_label,
            confidence=s.confidence,
            uncertain=s.uncertain,
        )
        for index, s in enumerate(ordered, 1)
    ]
    return TranscriptV1(schema_version=1, segments=segments)


def merge_chunks(chunks: list[tuple[int, int, int, list[RawSegment]]]) -> list[RawSegment]:
    """Deterministic merge of overlapping chunk results.

    ``chunks`` is ``(index, start_ms, end_ms, segments)``. The boundary between chunk ``i``
    and ``i+1`` is the middle of their overlap; a segment belongs to the chunk whose region
    contains its midpoint, so a segment seen by two chunks is kept exactly once and the same
    inputs always give the same output.
    """
    ordered = sorted(chunks, key=lambda c: c[0])
    out: list[RawSegment] = []
    for position, (_, _start, end, segments) in enumerate(ordered):
        low = 0 if position == 0 else (ordered[position][1] + ordered[position - 1][2]) // 2
        high = MAX_INT if position == len(ordered) - 1 else (ordered[position + 1][1] + end) // 2
        for seg in segments:
            mid = (seg.start_ms + seg.end_ms) // 2
            if low <= mid < high:
                out.append(seg)
    return sorted(out, key=lambda s: (s.start_ms, s.end_ms))


def span_segments(transcript: TranscriptV1, start_ms: int, end_ms: int) -> list[Segment]:
    """Segments that overlap ``[start_ms, end_ms)``; a zero-length span matches the segment
    containing that instant."""
    if end_ms == start_ms:
        return [s for s in transcript.segments if s.start_ms <= start_ms < s.end_ms]
    return [s for s in transcript.segments if s.start_ms < end_ms and s.end_ms > start_ms]


def quote_text(segments: list[Segment]) -> str:
    return "\n".join(s.text for s in segments)


def quote_sha256(segments: list[Segment]) -> str:
    return hashlib.sha256(quote_text(segments).encode("utf-8")).hexdigest()


def clock(ms: int) -> str:
    seconds, milli = divmod(ms, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{sec:02d}.{milli:03d}"


def render_lines(transcript: TranscriptV1) -> str:
    """Numbered text view for agent task contexts, one segment per line."""
    return "\n".join(
        f"{s.id} [{clock(s.start_ms)} - {clock(s.end_ms)}]"
        f"{' (uncertain)' if s.uncertain else ''} {s.text}"
        for s in transcript.segments
    )


def validate_locator(transcript: TranscriptV1, start_ms: int, end_ms: int) -> list[Segment]:
    """Segments for a locator, or ``validation_error`` when it does not fit the transcript."""
    if end_ms > transcript.duration_ms:
        raise PrometheosError(
            "validation_error",
            "audio locator extends beyond the transcript",
            {"transcript_end_ms": transcript.duration_ms},
        )
    found = span_segments(transcript, start_ms, end_ms)
    if not found:
        raise PrometheosError(
            "validation_error", "no transcript segment overlaps the audio locator", {}
        )
    return found


def as_json(transcript: TranscriptV1) -> dict[str, Any]:
    return transcript.model_dump(mode="json")
