"""WAV inspection, slicing and decoding to the 16 kHz mono PCM used for transcription.

Plain PCM WAV needs only the standard ``wave`` module. Other containers (mp3, m4a, ogg,
flac ...) are decoded by PyAV, which exists only in the optional ``audio`` extra; without
it they fail with an ``unsupported`` error that says what to install or how to convert.
"""

from __future__ import annotations

import wave
from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from prometheos.audio import pcm
from prometheos.domain.models import PrometheosError
from prometheos.faults import fault_point

BLOCK_FRAMES = 48000
MAX_WAV_BYTES = 8 * 1024**3
MIN_RATE, MAX_RATE = 4_000, 192_000
MAX_CHANNELS = 8
MAX_DECODED_SECONDS = 12 * 3600  # decoded audio is capped at twelve hours of 16 kHz mono
MAX_DECODED_SAMPLES = MAX_DECODED_SECONDS * pcm.TARGET_RATE


@dataclass(frozen=True)
class WavInfo:
    frames: int
    rate: int
    channels: int
    width: int

    @property
    def duration_ms(self) -> int:
        return self.frames * 1000 // self.rate if self.rate else 0


def is_wav_name(name: str) -> bool:
    return name.lower().endswith((".wav", ".wave"))


def _data_offset(stream: BinaryIO) -> int | None:
    """Where the sample bytes start, found by walking the RIFF chunks (the header's own size
    fields are never trusted)."""
    stream.seek(0)
    head = stream.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    for _ in range(64):
        header = stream.read(8)
        if len(header) < 8:
            return None
        size = int.from_bytes(header[4:8], "little")
        if header[:4] == b"data":
            return stream.tell()
        stream.seek(size + (size & 1), 1)
    return None


def _validated(
    stream: BinaryIO, size: int, frames: int, rate: int, channels: int, width: int
) -> WavInfo:
    """Bound the header and derive the length from the bytes actually present: a header can
    claim hours of audio in a few bytes, or a rate that blows a resampler up."""
    if not (MIN_RATE <= rate <= MAX_RATE):
        raise PrometheosError(
            "validation_error",
            f"WAV sample rate {rate} Hz is outside the accepted {MIN_RATE}-{MAX_RATE} Hz; "
            "convert the file to a standard rate (for example 16000 Hz, 16-bit PCM) and import "
            "it again",
            {"sample_rate": rate, "accepted_min": MIN_RATE, "accepted_max": MAX_RATE},
        )
    if not (1 <= channels <= MAX_CHANNELS):
        raise PrometheosError(
            "validation_error",
            f"WAV must have 1-{MAX_CHANNELS} channels; convert the file and import it again",
            {"channels": channels},
        )
    if not (1 <= width <= 4):
        raise PrometheosError(
            "validation_error",
            "WAV sample width must be 1-4 bytes; convert the file to 16-bit PCM and import "
            "it again",
            {},
        )
    if size > MAX_WAV_BYTES:
        raise PrometheosError("validation_error", "WAV file is too large", {"bytes": size})
    offset = _data_offset(stream)
    if offset is None:
        raise PrometheosError("unsupported", "WAV has no readable data chunk", {})
    present = max(0, size - offset) // (width * channels)
    real = min(frames, present)
    if real > MAX_DECODED_SECONDS * rate:
        raise PrometheosError(
            "validation_error",
            "WAV is longer than the 12 hour limit",
            {"seconds": real // rate},
        )
    return WavInfo(real, rate, channels, width)


def inspect_wav(path: Path) -> WavInfo:
    """Header facts of an integer-PCM WAV, bounded and measured against the real data bytes.
    ``unsupported`` for anything that is not integer PCM; ``validation_error`` for a header
    outside the accepted bounds."""
    try:
        with wave.open(str(path), "rb") as handle:
            frames, rate = handle.getnframes(), handle.getframerate()
            channels, width = handle.getnchannels(), handle.getsampwidth()
        with open(path, "rb") as raw:
            return _validated(raw, path.stat().st_size, frames, rate, channels, width)
    except (wave.Error, EOFError) as exc:
        raise PrometheosError(
            "unsupported",
            "not an integer PCM WAV file (compressed or float audio needs the audio extra)",
            {"detail": str(exc)[:200]},
        ) from exc


def wav_info_bytes(data: bytes) -> WavInfo | None:
    """Bounded header facts of in-memory WAV bytes, or ``None`` when they are not a usable
    integer WAV."""
    import io

    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            frames, rate = handle.getnframes(), handle.getframerate()
            channels, width = handle.getnchannels(), handle.getsampwidth()
        return _validated(io.BytesIO(data), len(data), frames, rate, channels, width)
    except (wave.Error, EOFError, PrometheosError):
        return None


def write_wav(out: Path, blocks: Iterable[bytes], rate: int = pcm.TARGET_RATE) -> int:
    """Write mono 16-bit PCM blocks as a WAV file; returns the frame count."""
    frames = 0
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        for block in blocks:
            handle.writeframes(block)
            frames += len(block) // 2
    return frames


def extract_clip(src: Path, start_ms: int, end_ms: int, out: Path) -> WavInfo:
    """Copy the frames between two instants into a new WAV with the same format."""
    info = inspect_wav(src)
    first = min(info.frames, start_ms * info.rate // 1000)
    last = min(info.frames, -(-end_ms * info.rate // 1000))
    with wave.open(str(src), "rb") as reader, wave.open(str(out), "wb") as writer:
        writer.setnchannels(info.channels)
        writer.setsampwidth(info.width)
        writer.setframerate(info.rate)
        reader.setpos(first)
        left = max(0, last - first)
        while left:
            chunk = reader.readframes(min(left, BLOCK_FRAMES))
            if not chunk:
                break
            writer.writeframes(chunk)
            left -= len(chunk) // (info.width * info.channels)
    return WavInfo(max(0, last - first), info.rate, info.channels, info.width)


def _normalize_wav(src: Path, out_raw: Path) -> int:
    info = inspect_wav(src)
    resampler = pcm.Resampler(info.rate, pcm.TARGET_RATE)
    total = 0
    left = info.frames  # only what the data bytes really hold, whatever the header claims
    with wave.open(str(src), "rb") as reader, open(out_raw, "wb") as sink:
        while left > 0:
            raw = reader.readframes(min(BLOCK_FRAMES, left))
            if not raw:
                break
            left -= len(raw) // (info.width * info.channels)
            mono = pcm.to_mono_int16(raw, info.width, info.channels)
            converted = resampler.process(mono)
            sink.write(pcm.samples_to_bytes(converted))
            sink.flush()
            total += len(converted)
            fault_point("transcribe_during_decode")  # the output is partly written, still open
    return total


def _normalize_with_av(src: Path, out_raw: Path) -> int:
    if src.stat().st_size > MAX_WAV_BYTES:
        raise PrometheosError("validation_error", "the audio file is too large", {})
    try:
        import av
    except ImportError as exc:
        raise PrometheosError(
            "unsupported",
            "decoding this audio format needs the optional audio extra "
            "(uv sync --extra audio), or convert it to a PCM WAV file first",
            {"extra": "audio"},
        ) from exc
    total = 0
    try:
        with av.open(str(src)) as container, open(out_raw, "wb") as sink:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise PrometheosError("unsupported", "the file contains no audio stream", {})
            resampler = av.AudioResampler(format="s16", layout="mono", rate=pcm.TARGET_RATE)
            for frame in container.decode(stream):
                if not isinstance(frame, av.AudioFrame):
                    continue
                for converted in resampler.resample(frame):
                    data = bytes(converted.to_ndarray().tobytes())
                    sink.write(data)
                    sink.flush()
                    total += len(data) // 2
                    fault_point("transcribe_during_decode")  # partly written, still open
                if total > MAX_DECODED_SAMPLES:
                    raise PrometheosError(
                        "validation_error",
                        "the decoded audio is longer than the 12 hour limit",
                        {"limit_seconds": MAX_DECODED_SECONDS},
                    )
            for converted in resampler.resample(None):
                data = bytes(converted.to_ndarray().tobytes())
                sink.write(data)
                total += len(data) // 2
    except PrometheosError:
        raise
    except Exception as exc:  # PyAV raises its own error family for corrupt input
        raise PrometheosError(
            "unsupported", "the audio could not be decoded", {"detail": str(exc)[:200]}
        ) from exc
    return total


def normalize_to_raw16k(src: Path, out_raw: Path) -> int:
    """Convert any supported audio file to raw 16 kHz mono s16le; returns the sample count."""
    if is_wav_name(src.name) or _looks_like_wav(src):
        try:
            return _normalize_wav(src, out_raw)
        except PrometheosError as exc:
            if exc.code == "unsupported" and is_wav_name(src.name):
                # A float or compressed WAV: PyAV can decode what the stdlib cannot.
                return _normalize_with_av(src, out_raw)
            raise
    return _normalize_with_av(src, out_raw)


def _looks_like_wav(path: Path) -> bool:
    with open(path, "rb") as handle:
        head = handle.read(12)
    return head[:4] == b"RIFF" and head[8:12] == b"WAVE"


def read_all_samples(path: Path) -> array[int]:
    """Test and inspection helper: a whole 16 kHz mono WAV as samples."""
    info = inspect_wav(path)
    with wave.open(str(path), "rb") as reader:
        return pcm.to_mono_int16(reader.readframes(info.frames), info.width, info.channels)
