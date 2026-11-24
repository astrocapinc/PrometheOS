"""Helpers for CP6 tests: synthetic WAV files, scripted capture devices, a project with audio."""

from __future__ import annotations

import wave
from array import array
from pathlib import Path
from typing import Any

from cp1_support import OWNER, Env

from prometheos.audio import capture, pcm
from prometheos.audio.transcribe import RunConfig

SPEECH = 0.3
QUIET = 0.02


def burst_samples(plan: list[tuple[int, float]], rate: int = pcm.TARGET_RATE) -> array[int]:
    """Concatenated 1 kHz bursts: ``(milliseconds, amplitude)``; amplitude 0 is digital silence."""
    out: array[int] = array("h")
    for ms, amplitude in plan:
        if amplitude <= 0:
            out.extend(array("h", bytes(2 * (rate * ms // 1000))))
        else:
            out.extend(pcm.tone(1000.0, ms, rate, amplitude))
    return out


def write_wav(
    path: Path,
    plan: list[tuple[int, float]],
    *,
    rate: int = pcm.TARGET_RATE,
    channels: int = 1,
) -> Path:
    samples = burst_samples(plan, rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        if channels == 1:
            handle.writeframes(pcm.samples_to_bytes(samples))
        else:
            inter: array[int] = array("h")
            for value in samples:
                inter.extend([value] * channels)
            handle.writeframes(pcm.samples_to_bytes(inter))
    return path


# 8 s: 2 s speech, 2 s silence, 2 s speech, 2 s quiet (marked uncertain by the synthetic engine)
MEETING_PLAN = [(2000, SPEECH), (2000, 0.0), (2000, SPEECH), (2000, QUIET)]
# The synthetic engine speaks script line n in the 2-second window n (windows 1 and 3 are
# silent and quiet in MEETING_PLAN), so line 0 is heard at 0 s and line 2 at 4 s.
SCRIPT = (
    "Compare the controllers under actuator limits.",
    "(silence)",
    "Send the constrained result.",
    "(quiet)",
)


class AudioEnv(Env):
    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """Read raw operational tables (always closes the connection)."""
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.store_dir / "record.sqlite")) as conn:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.store_dir / "record.sqlite")) as conn:
            conn.execute(sql, params)
            conn.commit()

    def audio_file(
        self,
        name: str = "meeting.wav",
        plan: list[tuple[int, float]] | None = None,
        **kwargs: Any,
    ) -> str:
        write_wav(self.research / name, plan or MEETING_PLAN, **kwargs)
        return name

    def import_audio(
        self, name: str = "meeting.wav", sensitivity: str = "normal", **kwargs: Any
    ) -> dict[str, Any]:
        self.audio_file(name, **kwargs)
        return self.svc.audio_import(
            self.project_id, OWNER, root_alias="research", path=name, sensitivity=sensitivity
        )

    def synthetic_config(self, **overrides: Any) -> RunConfig:
        values: dict[str, Any] = {
            "engine": "synthetic",
            "chunk_ms": 4000,
            "overlap_ms": 2000,
            "script": SCRIPT,
        }
        values.update(overrides)
        return RunConfig(**values)


class ScriptedSource:
    """A device that yields ``blocks`` blocks of a tone, then ends, loses the device, or blocks."""

    def __init__(
        self,
        info: capture.DeviceInfo,
        blocks: int,
        *,
        ending: str = "eof",
        freq: float = 1000.0,
    ) -> None:
        self.info = info
        self.blocks = blocks
        self.ending = ending
        self.freq = freq
        self.index = 0
        self.closed = False

    def read(self, frames: int) -> bytes | None:
        if self.index >= self.blocks:
            if self.ending == "lost":
                raise capture.DeviceLost("device unplugged")
            return None
        rate = self.info.sample_rate
        start = self.index * frames
        import math

        samples = array(
            "h",
            (
                int(0.3 * 32767 * math.sin(2 * math.pi * self.freq * (start + i) / rate))
                for i in range(frames)
            ),
        )
        self.index += 1
        if self.info.channels > 1:
            inter: array[int] = array("h")
            for value in samples:
                inter.extend([value] * self.info.channels)
            samples = inter
        return pcm.samples_to_bytes(samples)

    def close(self) -> None:
        self.closed = True


class ScriptedBackend:
    name = "scripted"

    def __init__(self, sources: dict[str, ScriptedSource]) -> None:
        self.sources = sources

    def devices(self) -> list[capture.DeviceInfo]:
        return [s.info for s in self.sources.values()]

    def open(self, device_id: str) -> capture.CaptureSource:
        return self.sources[device_id]


MIC = capture.DeviceInfo("mic-1", "Test microphone", "microphone", 48000, 1, True)
LOOP = capture.DeviceInfo("loop-1", "Test system audio", "loopback", 44100, 2, True)
