"""Checks that need the optional audio extra and, for one of them, the downloaded speech model.

* ``audio_extra``: PyAV decodes compressed audio (mp3, flac) into the same 16 kHz mono PCM the
  engines read; skipped when the extra is not installed.
* ``real_model``: the real faster-whisper engine transcribes a spoken sample. The sample is made
  at run time with the Windows speech synthesiser (a synthetic voice, not a recording of a
  person); skipped unless the model was installed explicitly (``prometheos audio model
  install``) and Windows speech is available. This is the engineering half of E05: the owner's
  real, permissioned recording and ear-check of the spans remain the gate itself."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import pytest
from audio_support import AudioEnv, write_wav
from cp1_support import OWNER, create_entry

from prometheos.audio import engines, models, pcm, transcribe, wav
from prometheos.audio import transcript as tr
from prometheos.domain.models import PrometheosError
from prometheos.store.home import app_home

try:
    import av
except ImportError:  # the optional extra is not installed
    av = None

SPEECH = (
    "Please attach the constrained plant result before the next meeting. "
    "The supervisor asked whether the venue allows color figures."
)
TTS_SCRIPT = """param([string]$Out, [string]$Text)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToWaveFile($Out)
$s.Speak($Text)
$s.Dispose()
"""


@pytest.fixture
def env(tmp_path: Path) -> AudioEnv:
    return AudioEnv(tmp_path)


def _required(config: pytest.Config, marker: str, variable: str) -> bool:
    expression = (config.getoption("markexpr") or "").replace(" ", "")
    return os.environ.get(variable) == "1" or (
        marker in expression and f"not{marker}" not in expression
    )


@pytest.fixture(autouse=True)
def _prerequisites(request: pytest.FixtureRequest) -> None:
    """Skip when the extra, the model or Windows speech is missing, but FAIL (never skip) when
    the run asks for these tests: ``-m real_model`` / ``-m audio_extra`` or
    ``PROMETHEOS_REQUIRE_REAL_MODEL=1`` / ``PROMETHEOS_REQUIRE_AUDIO_EXTRA=1`` (the CP6
    checkpoint suite sets them), as the Tectonic tests do."""
    config = request.config
    problems: list[tuple[str, str, str]] = []
    if request.node.get_closest_marker("audio_extra") and av is None:
        problems.append(
            ("audio_extra", "PROMETHEOS_REQUIRE_AUDIO_EXTRA", "the audio extra is missing")
        )
    if request.node.get_closest_marker("real_model"):
        if av is None:
            problems.append(
                ("real_model", "PROMETHEOS_REQUIRE_REAL_MODEL", "the audio extra is missing")
            )
        if not model_installed():
            problems.append(
                ("real_model", "PROMETHEOS_REQUIRE_REAL_MODEL", "the speech model is not installed")
            )
        if sys.platform != "win32":
            problems.append(
                (
                    "real_model",
                    "PROMETHEOS_REQUIRE_REAL_MODEL",
                    "the sample voice needs Windows speech",
                )
            )
    for marker, variable, reason in problems:
        if _required(config, marker, variable):
            pytest.fail(f"required test cannot run: {reason}")
        pytest.skip(reason)


# ----- compressed audio through PyAV ----------------------------------------------------------


def encode(path: Path, codec: str, source: Path) -> None:
    """Encode a WAV with PyAV (libmp3lame for mp3, native flac)."""
    samples = wav.read_all_samples(source)
    with av.open(str(path), "w") as container:
        stream = container.add_stream(codec, rate=pcm.TARGET_RATE, layout="mono")
        frame_size = 1152
        for offset in range(0, len(samples), frame_size):
            block = samples[offset : offset + frame_size]
            frame = av.AudioFrame.from_ndarray(
                __import__("numpy").array([list(block)], dtype="int16"), format="s16", layout="mono"
            )
            frame.sample_rate = pcm.TARGET_RATE
            frame.pts = offset
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


@pytest.mark.audio_extra
@pytest.mark.parametrize("codec, suffix", [("libmp3lame", ".mp3"), ("flac", ".flac")])
def test_compressed_audio_is_decoded_to_the_same_pcm_and_transcribed(
    env: AudioEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codec: str, suffix: str
) -> None:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    source = write_wav(tmp_path / "source.wav", [(2000, 0.3), (2000, 0.0), (2000, 0.3)])
    packed = env.research / f"talk{suffix}"
    encode(packed, codec, source)
    imported = env.svc.audio_import(
        env.project_id, OWNER, root_alias="research", path=packed.name, sensitivity="normal"
    )
    assert imported["audio"] is None  # not a PCM WAV: the header cannot be read without decoding
    done = env.svc.transcribe_audio(
        env.project_id, OWNER, imported["version_id"], env.synthetic_config(chunk_ms=4000)
    )
    segments = env.svc.transcript_get(env.project_id, OWNER, done["result_version_id"])["payload"][
        "segments"
    ]
    assert segments and abs(segments[-1]["end_ms"] - 6000) < 300  # mp3 pads by a frame or two
    spoken = [s for s in segments if s["start_ms"] < 1900]
    assert spoken and not [s for s in segments if 2300 < s["start_ms"] < 3700]  # the silence stays
    # The decoder output equals the original within codec error: same tone, same loudness.
    raw = tmp_path / "decoded.raw"
    count = wav.normalize_to_raw16k(packed, raw)
    assert abs(count - 6 * pcm.TARGET_RATE) < 0.05 * pcm.TARGET_RATE
    decoded = pcm.samples_from_bytes(raw.read_bytes())
    assert abs(pcm.zero_crossing_frequency(decoded[: pcm.TARGET_RATE]) - 1000.0) < 60
    # Citations on a compressed recording are refused, not guessed (cite the transcript).
    with pytest.raises(PrometheosError, match="without decoding"):
        env.apply(
            [
                create_entry(
                    evidence=[
                        {
                            "source_id": imported["source_id"],
                            "version_id": imported["version_id"],
                            "locator": {"type": "audio", "start_ms": 0, "end_ms": 1000},
                            "quote_sha256": None,
                        }
                    ]
                )
            ]
        )


@pytest.mark.audio_extra
def test_a_file_that_is_not_audio_fails_clearly(
    env: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    (env.research / "noise.mp3").write_bytes(b"this is certainly not an mp3 file" * 50)
    imported = env.svc.audio_import(
        env.project_id, OWNER, root_alias="research", path="noise.mp3", sensitivity="normal"
    )
    run = env.svc.transcribe_start(
        env.project_id, OWNER, imported["version_id"], env.synthetic_config()
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.transcribe_run(env.project_id, OWNER, run["run_id"])
    assert err.value.code in ("unsupported", "io_error")


# ----- the real engine --------------------------------------------------------------------------


def real_models_dir() -> Path:
    return Path(os.environ.get(models.MODELS_DIR_ENV) or app_home() / "models")


def model_installed() -> bool:
    spec = models.spec_for(models.DEFAULT_MODEL)
    return (real_models_dir() / spec.key / spec.revision / models.MANIFEST).is_file()


POWERSHELL_RELATIVE = Path("System32") / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def windows_powershell() -> Path | None:
    """The standard Windows PowerShell 5.1 (``%SystemRoot%\\System32\\WindowsPowerShell\\v1.0``),
    resolved explicitly: a bare ``powershell`` depends on PATH, and a cleared environment (the
    CP10 launches) need not carry the PowerShell folder. PATH is not consulted. ``None`` when
    neither SystemRoot nor WINDIR names a folder that contains it."""
    for variable in ("SystemRoot", "WINDIR"):
        root = os.environ.get(variable)
        if root and (candidate := Path(root) / POWERSHELL_RELATIVE).is_file():
            return candidate
    return None


def make_speech(tmp_path: Path, config: pytest.Config) -> Path:
    def unavailable(detail: str) -> NoReturn:
        reason = f"Windows speech synthesis is not available: {detail}"
        if _required(config, "real_model", "PROMETHEOS_REQUIRE_REAL_MODEL"):
            pytest.fail(f"required test cannot run: {reason}")  # never a silent skip when required
        pytest.skip(reason)

    powershell = windows_powershell()
    if powershell is None:
        unavailable(
            f"Windows PowerShell was not found at %SystemRoot%\\{POWERSHELL_RELATIVE} "
            f"(SystemRoot={os.environ.get('SystemRoot')!r}, WINDIR={os.environ.get('WINDIR')!r}; "
            "PATH is not searched)"
        )
    script = tmp_path / "speak.ps1"
    script.write_text(TTS_SCRIPT, encoding="utf-8")
    out = tmp_path / "spoken.wav"
    try:
        done = subprocess.run(
            [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-Out", str(out), "-Text", SPEECH],
            capture_output=True, timeout=120, check=False,
        )  # fmt: skip
    except OSError as error:
        unavailable(f"{powershell} could not be started ({error})")
    if done.returncode != 0 or not out.is_file():
        detail = done.stderr.decode("utf-8", "replace").strip()[-400:]
        unavailable(f"{powershell} exited {done.returncode} without a spoken sample ({detail})")
    return out


@pytest.mark.real_model
def test_real_engine_transcribes_a_spoken_sample_with_checkable_spans(
    env: AudioEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv(models.MODELS_DIR_ENV, str(real_models_dir()))
    spoken = make_speech(tmp_path, request.config)
    (env.research / "spoken.wav").write_bytes(spoken.read_bytes())
    imported = env.svc.audio_import(
        env.project_id, OWNER, root_alias="research", path="spoken.wav", sensitivity="restricted"
    )
    duration = imported["audio"]["duration_ms"]
    assert 6000 < duration < 12000
    config = transcribe.RunConfig("faster-whisper", language="en")
    done = env.svc.transcribe_audio(env.project_id, OWNER, imported["version_id"], config)
    assert done["state"] == "completed"
    version = env.svc.get(env.project_id, OWNER, done["result_version_id"], None)
    transformation = version["body"]["transformation"]
    assert (
        transformation["engine"] == "faster-whisper"
        and transformation["model"] == models.DEFAULT_MODEL
    )
    assert transformation["language"] == "en" and transformation["engine_version"] == "1.2.1"
    assert version["sensitivity"] == "restricted"
    view = env.svc.transcript_get(env.project_id, OWNER, done["result_version_id"])
    payload = tr.TranscriptV1.model_validate(view["payload"])
    text = " ".join(s.text for s in payload.segments).lower()
    for word in ("attach", "constrained", "plant", "result", "meeting", "venue"):
        assert word in text, (word, text)
    # Spans are real time ranges inside the recording, in order, and every one is checkable:
    # the locator validates, the quote digest matches, the clip is the span's audio.
    last_end = 0
    for segment in payload.segments:
        assert 0 <= segment.start_ms < segment.end_ms <= duration + 500
        assert segment.start_ms >= last_end - 1  # no overlap after the deterministic merge
        last_end = segment.end_ms
        assert segment.confidence is None or 0.0 <= segment.confidence <= 1.0
    first = payload.segments[0]
    clip = tmp_path / "clip.wav"
    report = env.svc.audio_span(
        env.project_id, OWNER, done["result_version_id"], first.start_ms,
        min(first.end_ms, payload.duration_ms), out=clip,
    )  # fmt: skip
    assert report["quote"] == first.text and wav.inspect_wav(clip).duration_ms > 500
    assert pcm.peak_level(wav.read_all_samples(clip)) > 0.02  # the span is speech, not silence

    # E05-style correction: a changed word is a new version; the old one and its quote stay.
    fixed = env.svc.transcript_correct(
        env.project_id, OWNER, done["result_version_id"],
        [transcribe.Edit(first.id, text="Please attach the constrained-plant result.")],
        "00000000-0000-4000-8000-000000000001",
    )  # fmt: skip
    newer = env.svc.transcript_get(env.project_id, OWNER, fixed["version_id"])
    assert newer["payload"]["segments"][0]["text"] == "Please attach the constrained-plant result."
    assert view["payload"]["segments"][0]["text"] == first.text


# ----- R3-L1: a missing speech synthesiser fails a required run, never skips it ---------------


def _speech_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(*_a: object, **_k: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 1, b"", b"no voices")

    monkeypatch.setattr(subprocess, "run", failed)


def test_a_required_real_model_run_fails_when_windows_speech_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    _speech_unavailable(monkeypatch)
    monkeypatch.setenv("PROMETHEOS_REQUIRE_REAL_MODEL", "1")
    with pytest.raises(pytest.fail.Exception, match="required test cannot run"):
        make_speech(tmp_path, request.config)


def test_an_optional_real_model_run_skips_when_windows_speech_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    _speech_unavailable(monkeypatch)
    monkeypatch.delenv("PROMETHEOS_REQUIRE_REAL_MODEL", raising=False)
    monkeypatch.setattr(request.config.option, "markexpr", "")
    with pytest.raises(pytest.skip.Exception, match="speech synthesis"):
        make_speech(tmp_path, request.config)


def test_the_real_model_marker_selected_with_dash_m_makes_the_speech_check_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    _speech_unavailable(monkeypatch)
    monkeypatch.delenv("PROMETHEOS_REQUIRE_REAL_MODEL", raising=False)
    monkeypatch.setattr(request.config.option, "markexpr", "real_model")
    with pytest.raises(pytest.fail.Exception, match="required test cannot run"):
        make_speech(tmp_path, request.config)


# ----- the speech synthesiser is resolved explicitly, and its absence is a stated failure ------


def _fake_windows(root: Path) -> Path:
    shell = root / POWERSHELL_RELATIVE
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b"")
    return shell


def test_the_standard_powershell_is_resolved_from_the_windows_folder_not_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _fake_windows(tmp_path / "Windows")
    monkeypatch.setenv("PATH", "")  # a cleared environment: nothing to find by name
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.delenv("WINDIR", raising=False)
    assert windows_powershell() == shell
    monkeypatch.delenv("SystemRoot")
    monkeypatch.setenv("WINDIR", str(tmp_path / "Windows"))
    assert windows_powershell() == shell
    monkeypatch.setenv("WINDIR", str(tmp_path / "Elsewhere"))
    assert windows_powershell() is None


def _without_powershell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path / "NoWindows"))
    monkeypatch.delenv("WINDIR", raising=False)

    def not_called(*_a: object, **_k: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("no process may be started without a resolved PowerShell")

    monkeypatch.setattr(subprocess, "run", not_called)


def test_a_required_real_model_run_fails_with_a_diagnostic_when_powershell_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    _without_powershell(monkeypatch, tmp_path)
    monkeypatch.setenv("PROMETHEOS_REQUIRE_REAL_MODEL", "1")
    with pytest.raises(pytest.fail.Exception) as err:
        make_speech(tmp_path, request.config)
    message = str(err.value)
    assert message.startswith("required test cannot run: Windows speech synthesis is not available")
    assert "Windows PowerShell was not found" in message and "NoWindows" in message
    assert "PATH is not searched" in message


def test_an_optional_real_model_run_skips_when_powershell_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    _without_powershell(monkeypatch, tmp_path)
    monkeypatch.delenv("PROMETHEOS_REQUIRE_REAL_MODEL", raising=False)
    monkeypatch.setattr(request.config.option, "markexpr", "")
    with pytest.raises(pytest.skip.Exception, match="Windows PowerShell was not found"):
        make_speech(tmp_path, request.config)


def test_a_required_run_names_a_powershell_that_cannot_be_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """The failure of the first CP10 launches (WinError 2 starting a bare ``powershell``)."""
    shell = _fake_windows(tmp_path / "Windows")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))

    def cannot_start(*_a: object, **_k: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(subprocess, "run", cannot_start)
    monkeypatch.setenv("PROMETHEOS_REQUIRE_REAL_MODEL", "1")
    with pytest.raises(pytest.fail.Exception) as err:
        make_speech(tmp_path, request.config)
    assert "required test cannot run" in str(err.value)
    assert str(shell) in str(err.value) and "could not be started" in str(err.value)
