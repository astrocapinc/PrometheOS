"""Transcription engines behind one interface.

``faster-whisper`` is the real local engine (optional ``audio`` extra, CPU, no network at
run time). ``synthetic`` is a deterministic stand-in enabled only by
``PROMETHEOS_ENABLE_SYNTHETIC_ENGINE=1``: its text is explicitly synthetic (or a supplied
script), and its provenance says so, so it can never pass as a real transcript.
"""

from __future__ import annotations

import math
import os
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from prometheos.audio import models, pcm
from prometheos.audio.transcript import RawSegment
from prometheos.domain.models import PrometheosError

SYNTHETIC_ENV = "PROMETHEOS_ENABLE_SYNTHETIC_ENGINE"
# Tests only (kill probes, decision 0017): the synthetic engine sleeps this long per chunk, so a
# test can stop its worker process while a later chunk is in progress.
SYNTHETIC_DELAY_ENV = "PROMETHEOS_SYNTHETIC_ENGINE_DELAY_MS"
# Tests only (decision 0048): a file the synthetic engine appends one line to (the chunk's start
# in ms) as it enters that delay, so a kill probe stops the worker between transactions instead
# of on a timer that can land inside a commit.
SYNTHETIC_MARKER_ENV = "PROMETHEOS_SYNTHETIC_ENGINE_MARKER"
SYNTHETIC_WINDOW_MS = 2000
SILENCE_PEAK = 0.005
QUIET_PEAK = 0.05
ENGINES = ("faster-whisper", "synthetic")


@dataclass(frozen=True)
class ChunkResult:
    segments: list[RawSegment]
    language: str | None = None


class Engine(Protocol):
    name: str
    version: str
    model: str | None

    def transcribe(self, data: bytes, start_ms: int, language: str | None) -> ChunkResult: ...

    def close(self) -> None: ...


@dataclass
class SyntheticEngine:
    """Windowed, content-driven and deterministic: silence yields nothing, a quiet window is
    marked uncertain, anything louder gets the next script line (or a labelled placeholder)."""

    script: list[str] = field(default_factory=list)
    name: str = "synthetic-engine"
    version: str = "1"
    model: str | None = None

    def transcribe(self, data: bytes, start_ms: int, language: str | None) -> ChunkResult:
        delay = os.environ.get(SYNTHETIC_DELAY_ENV)
        if delay:
            import time

            marker = os.environ.get(SYNTHETIC_MARKER_ENV)
            if marker:
                with open(marker, "a", encoding="utf-8") as stream:
                    stream.write(f"{start_ms}\n")
            time.sleep(min(int(delay), 60_000) / 1000)
        samples = pcm.samples_from_bytes(data)
        per_window = pcm.TARGET_RATE * SYNTHETIC_WINDOW_MS // 1000
        out: list[RawSegment] = []
        for offset in range(0, len(samples), per_window):
            window: array[int] = samples[offset : offset + per_window]
            if len(window) < pcm.TARGET_RATE // 4:
                continue  # under a quarter second: not a segment
            level = pcm.peak_level(window)
            if level < SILENCE_PEAK:
                continue
            begin = start_ms + offset * 1000 // pcm.TARGET_RATE
            end = begin + len(window) * 1000 // pcm.TARGET_RATE
            number = begin // SYNTHETIC_WINDOW_MS
            if self.script:
                text = self.script[number % len(self.script)]
            else:
                text = f"[synthetic segment {number + 1}]"
            quiet = level < QUIET_PEAK
            out.append(
                RawSegment(
                    begin,
                    end,
                    "[inaudible]" if quiet else text,
                    None if quiet else 1.0,
                    quiet,
                )
            )
        return ChunkResult(out, language or "und")

    def close(self) -> None:
        return None


class FasterWhisperEngine:
    name = "faster-whisper"
    model: str | None

    def __init__(self, model_dir: Path, model_name: str) -> None:
        try:
            import faster_whisper
        except ImportError as exc:
            raise PrometheosError(
                "unsupported",
                "local transcription needs the optional audio extra (uv sync --extra audio)",
                {"extra": "audio"},
            ) from exc
        os.environ["HF_HUB_OFFLINE"] = "1"  # the model is a local directory; never fetch
        self.version = str(faster_whisper.__version__)
        self.model = model_name
        self._model: Any = faster_whisper.WhisperModel(
            str(model_dir), device="cpu", compute_type="int8"
        )

    def transcribe(self, data: bytes, start_ms: int, language: str | None) -> ChunkResult:
        import numpy

        samples = numpy.frombuffer(data, dtype="<i2").astype("float32") / 32768.0
        segments, info = self._model.transcribe(
            samples, language=language, vad_filter=True, beam_size=5
        )
        out: list[RawSegment] = []
        for seg in segments:
            text = str(seg.text).strip()
            if not text:
                continue
            confidence = max(0.0, min(1.0, math.exp(float(seg.avg_logprob))))
            uncertain = float(seg.avg_logprob) < -1.0 or float(seg.no_speech_prob) > 0.6
            out.append(
                RawSegment(
                    start_ms + int(float(seg.start) * 1000),
                    start_ms + int(float(seg.end) * 1000),
                    text,
                    round(confidence, 3),
                    uncertain,
                )
            )
        return ChunkResult(out, str(info.language) if info.language else language)

    def close(self) -> None:
        self._model = None


def require_synthetic_enabled() -> None:
    if os.environ.get(SYNTHETIC_ENV) != "1":
        raise PrometheosError(
            "unsupported",
            f"the synthetic engine is for tests and demos; set {SYNTHETIC_ENV}=1 to use it",
            {},
        )


def create(name: str, *, home: Path, model: str | None, script: list[str] | None = None) -> Engine:
    if name == "synthetic":
        require_synthetic_enabled()
        return SyntheticEngine(script=list(script or []))
    if name == "faster-whisper":
        key = model or models.DEFAULT_MODEL
        return FasterWhisperEngine(models.installed_path(home, key), key)
    raise PrometheosError(
        "validation_error", "unknown transcription engine", {"engine": name, "known": list(ENGINES)}
    )


def engine_version_without_loading(name: str) -> str:
    """Version string for plans and provenance when the engine is not yet constructed."""
    if name == "synthetic":
        return "1"
    try:
        import faster_whisper

        return str(faster_whisper.__version__)
    except ImportError:
        return "not installed"
