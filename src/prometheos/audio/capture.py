"""Capture backends: the narrow interface between the recorder and a sound device.

``pyaudiowpatch`` is the Windows backend (WASAPI microphones and WASAPI loopback of the
system output, from the optional ``audio`` extra). ``synthetic`` produces tones and is
selectable only with ``PROMETHEOS_ENABLE_SYNTHETIC_ENGINE=1``; tests also pass their own
backend objects. A source delivers interleaved signed 16-bit PCM at its native rate; the
recorder converts and mixes. A failing device raises :class:`DeviceLost`.

The real backend is exercised by live hardware evidence (E04), not by default tests: its
default-test coverage uses a stand-in module for the vendor library.
"""

from __future__ import annotations

import math
import os
import sys
import time
from array import array
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from prometheos.audio import engines, pcm
from prometheos.domain.models import PrometheosError

BACKEND_ENV = "PROMETHEOS_CAPTURE_BACKEND"
SPEED_ENV = "PROMETHEOS_SYNTHETIC_SPEED"
BLOCK_MS = 50


class DeviceLost(Exception):
    """The device stopped delivering audio (unplugged, disabled, driver error)."""


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    name: str
    kind: Literal["microphone", "loopback"]
    sample_rate: int
    channels: int
    default: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "default": self.default,
        }


class CaptureSource(Protocol):
    info: DeviceInfo

    def read(self, frames: int) -> bytes | None:
        """Up to ``frames`` interleaved frames (blocking); ``None`` at end of stream."""
        ...

    def close(self) -> None: ...


class Backend(Protocol):
    name: str

    def devices(self) -> list[DeviceInfo]: ...

    def open(self, device_id: str) -> CaptureSource: ...


# ----- synthetic ---------------------------------------------------------------------------


class SyntheticSource:
    """A sine tone at the device's native rate; paced like a real device unless speed is 0."""

    def __init__(self, info: DeviceInfo, freq_hz: float, speed: float) -> None:
        self.info = info
        self._freq = freq_hz
        self._speed = speed
        self._index = 0

    def read(self, frames: int) -> bytes | None:
        rate = self.info.sample_rate
        peak = 0.3 * pcm.INT16_MAX
        samples = array(
            "h",
            (
                int(peak * math.sin(2 * math.pi * self._freq * (self._index + i) / rate))
                for i in range(frames)
            ),
        )
        self._index += frames
        if self._speed > 0:
            time.sleep(frames / rate / self._speed)
        if self.info.channels > 1:
            interleaved: array[int] = array("h")
            for value in samples:
                interleaved.extend([value] * self.info.channels)
            samples = interleaved
        return pcm.samples_to_bytes(samples)

    def close(self) -> None:
        return None


class SyntheticBackend:
    name = "synthetic"

    def __init__(self, speed: float | None = None) -> None:
        self._speed = float(os.environ.get(SPEED_ENV, "1")) if speed is None else speed
        self._devices = {
            "synthetic-mic": DeviceInfo(
                "synthetic-mic", "Synthetic tone (440 Hz)", "microphone", 44100, 1, True
            ),
            "synthetic-loopback": DeviceInfo(
                "synthetic-loopback", "Synthetic system audio (880 Hz)", "loopback", 48000, 2, True
            ),
        }

    def devices(self) -> list[DeviceInfo]:
        return list(self._devices.values())

    def open(self, device_id: str) -> CaptureSource:
        info = self._devices.get(device_id)
        if info is None:
            raise PrometheosError("not_found", "unknown capture device", {"device": device_id})
        return SyntheticSource(info, 440.0 if info.kind == "microphone" else 880.0, self._speed)


# ----- Windows (PyAudioWPatch) ---------------------------------------------------------------


class _PyAudioSource:
    def __init__(self, pyaudio: Any, instance: Any, info: DeviceInfo, index: int) -> None:
        self.info = info
        self._pyaudio = pyaudio
        self._instance = instance
        try:
            self._stream = instance.open(
                format=pyaudio.paInt16,
                channels=info.channels,
                rate=info.sample_rate,
                input=True,
                input_device_index=index,
                frames_per_buffer=info.sample_rate * BLOCK_MS // 1000,
            )
        except OSError as exc:
            instance.terminate()
            raise DeviceLost(f"could not open {info.name}: {exc}") from exc

    def read(self, frames: int) -> bytes | None:
        try:
            data = self._stream.read(frames, exception_on_overflow=False)
        except OSError as exc:
            raise DeviceLost(f"{self.info.name}: {exc}") from exc
        return bytes(data)

    def close(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._instance.terminate()


class PyAudioWPatchBackend:
    name = "pyaudiowpatch"

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise PrometheosError(
                "unsupported", "the WASAPI capture backend is for Windows only", {}
            )
        try:
            import pyaudiowpatch
        except ImportError as exc:
            raise PrometheosError(
                "unsupported",
                "live capture needs the optional audio extra (uv sync --extra audio)",
                {"extra": "audio"},
            ) from exc
        self._lib: Any = pyaudiowpatch

    def devices(self) -> list[DeviceInfo]:
        lib = self._lib
        instance = lib.PyAudio()
        try:
            api = instance.get_host_api_info_by_type(lib.paWASAPI)
            default_mic = api.get("defaultInputDevice", -1)
            try:
                default_loop = instance.get_default_wasapi_loopback()["index"]
            except (OSError, LookupError):
                default_loop = -1
            found: list[DeviceInfo] = []
            for number in range(instance.get_device_count()):
                item = instance.get_device_info_by_index(number)
                if item.get("hostApi") != api["index"]:
                    continue
                number = int(item["index"])
                rate = int(item["defaultSampleRate"])
                channels = int(item["maxInputChannels"])
                if item.get("isLoopbackDevice"):
                    found.append(
                        DeviceInfo(
                            f"wasapi:{number}", str(item["name"]), "loopback", rate, channels,
                            number == default_loop,
                        )
                    )  # fmt: skip
                elif channels > 0:
                    found.append(
                        DeviceInfo(
                            f"wasapi:{number}", str(item["name"]), "microphone", rate, channels,
                            number == default_mic,
                        )
                    )  # fmt: skip
            return found
        finally:
            instance.terminate()

    def open(self, device_id: str) -> CaptureSource:
        for info in self.devices():
            if info.id == device_id:
                instance = self._lib.PyAudio()
                return _PyAudioSource(self._lib, instance, info, int(device_id.split(":", 1)[1]))
        raise PrometheosError("not_found", "unknown capture device", {"device": device_id})


def get_backend(name: str | None = None) -> Backend:
    """``auto`` is the real backend where one exists; ``synthetic`` needs the explicit switch."""
    chosen = name or os.environ.get(BACKEND_ENV, "auto")
    if chosen == "synthetic":
        engines.require_synthetic_enabled()
        return SyntheticBackend()
    if chosen in ("auto", "pyaudiowpatch"):
        return PyAudioWPatchBackend()
    raise PrometheosError(
        "validation_error",
        "unknown capture backend",
        {"backend": chosen, "known": ["auto", "pyaudiowpatch", "synthetic"]},
    )
