"""Small PCM helpers on the standard ``array`` module.

Python 3.13 removed ``audioop``, so mono mixing, 16-bit conversion and resampling are
implemented here. The target format for recording and transcription is 16 kHz mono signed
16-bit little-endian PCM (about 115 MB per hour): enough for speech, not archival quality.
"""

from __future__ import annotations

import math
import struct
import sys
from array import array

TARGET_RATE = 16000
BYTES_PER_SAMPLE = 2
BYTES_PER_MS = TARGET_RATE * BYTES_PER_SAMPLE // 1000  # 32
INT16_MAX = 32767


def _le(samples: array[int]) -> array[int]:
    """``array('h')`` uses native order; PCM files are little-endian."""
    if sys.byteorder == "big":  # pragma: no cover - Windows and Linux CI are little-endian
        samples.byteswap()
    return samples


def samples_from_bytes(data: bytes) -> array[int]:
    usable = len(data) - (len(data) % BYTES_PER_SAMPLE)
    out = array("h")
    out.frombytes(data[:usable])
    return _le(out)


def samples_to_bytes(samples: array[int]) -> bytes:
    copy = array("h", samples)
    return _le(copy).tobytes()


def to_mono_int16(raw: bytes, sample_width: int, channels: int) -> array[int]:
    """Interleaved integer PCM of 1/2/3/4 bytes per sample to mono signed 16-bit."""
    if sample_width not in (1, 2, 3, 4) or channels < 1:
        raise ValueError("unsupported PCM layout")
    frame = sample_width * channels
    usable = len(raw) - (len(raw) % frame)
    raw = raw[:usable]
    if sample_width == 2:
        values = samples_from_bytes(raw)
    elif sample_width == 1:  # unsigned 8-bit
        values = array("h", ((b - 128) << 8 for b in raw))
    elif sample_width == 3:  # packed 24-bit: keep the top 16 bits
        values = array(
            "h",
            (struct.unpack("<h", raw[i + 1 : i + 3])[0] for i in range(0, len(raw) - 2, 3)),
        )
    else:  # 32-bit integer: keep the top 16 bits
        values = array("h", (v >> 16 for (v,) in struct.iter_unpack("<i", raw)))
    if channels == 1:
        return values
    count = len(values) // channels
    return array(
        "h", (sum(values[i * channels : (i + 1) * channels]) // channels for i in range(count))
    )


class Resampler:
    """Streaming sample-rate conversion to ``dst_rate`` (mono int16).

    Integer ratios average each group (a crude but adequate anti-alias for speech); other
    ratios interpolate linearly. State carries across ``process`` calls, so block
    boundaries introduce no discontinuity.
    """

    def __init__(self, src_rate: int, dst_rate: int = TARGET_RATE) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate, self.dst_rate = src_rate, dst_rate
        self._factor = src_rate // dst_rate if src_rate % dst_rate == 0 else 0
        self._carry: array[int] = array("h")
        self._pos = 0.0

    def process(self, block: array[int]) -> array[int]:
        if self.src_rate == self.dst_rate:
            return array("h", block)
        buf = array("h", self._carry)
        buf.extend(block)
        if self._factor:
            usable = len(buf) - (len(buf) % self._factor)
            f = self._factor
            out = array("h", (sum(buf[i : i + f]) // f for i in range(0, usable, f)))
            self._carry = buf[usable:]
            return out
        step = self.src_rate / self.dst_rate
        out = array("h")
        n, pos = len(buf), self._pos
        while int(pos) + 1 < n:
            i = int(pos)
            frac = pos - i
            out.append(int(buf[i] + (buf[i + 1] - buf[i]) * frac))
            pos += step
        keep = min(int(pos), n)
        self._carry = buf[keep:]
        self._pos = pos - keep
        return out


def mix(a: array[int], b: array[int]) -> array[int]:
    """Average two mono tracks (the shorter is padded with silence); never clips."""
    n = max(len(a), len(b))
    out = array("h", bytes(2 * n))
    for i in range(n):
        out[i] = ((a[i] if i < len(a) else 0) + (b[i] if i < len(b) else 0)) // 2
    return out


def peak_level(samples: array[int]) -> float:
    """Peak amplitude as a fraction of full scale (0.0 silence .. 1.0 clipping)."""
    if not samples:
        return 0.0
    return max(max(samples), -min(samples)) / 32768.0


def dbfs(level: float) -> float:
    return -180.0 if level <= 0 else max(-180.0, 20.0 * math.log10(level))


def tone(freq_hz: float, ms: int, rate: int = TARGET_RATE, amplitude: float = 0.3) -> array[int]:
    count = rate * ms // 1000
    peak = amplitude * INT16_MAX
    return array(
        "h", (int(peak * math.sin(2 * math.pi * freq_hz * i / rate)) for i in range(count))
    )


def zero_crossing_frequency(samples: array[int], rate: int = TARGET_RATE) -> float:
    """Rough frequency of a clean tone from its upward zero crossings."""
    crossings = sum(1 for i in range(1, len(samples)) if samples[i - 1] < 0 <= samples[i])
    return crossings * rate / len(samples) if samples else 0.0
