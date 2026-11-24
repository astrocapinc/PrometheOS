"""Capability discovery (A02, CP10.1; spec/04 "Parity and capability discovery").

A pure report, per feature (the registry's capability groups): installed version,
availability, the authorities its operations require, local or remote execution, cost
class and the current blocker. It reads only local facts (installed files, importable
optional extras, the project's provider settings) and never reveals a secret value, a
local path, an endpoint or restricted content. Nothing is fetched and nothing is written.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prometheos import __version__, registry
from prometheos.domain.models import PrometheosError

Probe = dict[str, Any]

CORE_FEATURES = frozenset(
    {"core", "meetings", "skills", "workflows", "papers", "jobs", "administration"}
)
EXECUTION: dict[str, str] = {"overleaf": "remote", "provider": "local_or_remote"}
COST: dict[str, str] = {
    "latex": "download",
    "audio_transcription": "download",
    "provider": "metered",
}


def _available(version: str | None) -> Probe:
    return {"version": version, "available": True, "blocker": None}


def _blocked(version: str | None, code: str, message: str) -> Probe:
    return {"version": version, "available": False, "blocker": {"code": code, "message": message}}


def _package_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _latex(home: Path) -> Probe:
    from prometheos.app import texengine

    report = texengine.status(home)
    if report["state"] == "installed":
        installed = report.get("installed") or {}
        return _available(str(installed.get("version") or report["pinned_version"]))
    # Decision 0015 round 2 (R11): a version only for what is installed.
    if report["state"] == "invalid":
        return _blocked(None, "not_installed", "the installed engine does not verify; reinstall")
    return _blocked(
        None, "not_installed", "Tectonic is not installed (prometheos tex engine install)"
    )


def _overleaf(_home: Path) -> Probe:
    # Only what is known locally: the system git is present. Whether a paper is linked, or
    # its remote reachable, is per paper (overleaf_status) and never claimed here.
    if shutil.which("git") is None:
        return _blocked(None, "not_installed", "the system git is not installed")
    return _available(None)


def _audio_capture(_home: Path) -> Probe:
    if sys.platform != "win32":
        return _blocked(None, "unsupported_platform", "live capture uses Windows WASAPI")
    if importlib.util.find_spec("pyaudiowpatch") is None:
        return _blocked(None, "not_installed", "the optional audio extra is not installed")
    return _available(_package_version("PyAudioWPatch"))


def _audio_transcription(home: Path) -> Probe:
    from prometheos.audio import models

    if importlib.util.find_spec("faster_whisper") is None:
        return _blocked(None, "not_installed", "the optional audio extra is not installed")
    version = _package_version("faster-whisper")
    spec = models.spec_for(models.DEFAULT_MODEL)
    if not models.installed(home, spec):
        return _blocked(
            version, "not_installed", "the speech model is not installed (audio model plan)"
        )
    return _available(version)


PROBES: dict[str, Callable[[Path], Probe]] = {
    "latex": _latex,
    "overleaf": _overleaf,
    "audio_capture": _audio_capture,
    "audio_transcription": _audio_transcription,
}


def provider_probe(status: dict[str, Any] | PrometheosError | None) -> tuple[Probe, str]:
    """Availability and execution of the configured provider, from ``provider_status``
    (agent view: no counts, no secret). ``None`` means no project was selected; an error
    reading the status blocks this feature only (decision 0015 round 2, R11)."""
    if status is None:
        return _blocked(None, "not_configured", "no project selected"), "local_or_remote"
    if isinstance(status, PrometheosError):
        return (
            _blocked(None, "not_configured", f"provider status unavailable ({status.code})"),
            "local_or_remote",
        )
    route = str(status.get("route") or "none")
    execution = route if route in ("local", "remote") else "local_or_remote"
    if status.get("policy") == "no-ai":
        return _blocked(None, "disabled_by_policy", "the project's policy is no-ai"), execution
    if not status.get("endpoint_host"):
        return _blocked(None, "not_configured", "no provider endpoint is configured"), execution
    credential = status.get("credential") or {}
    if route == "remote" and not credential.get("present"):
        return _blocked(None, "not_configured", "the credential reference has no value"), execution
    remaining = (status.get("usage") or {}).get("remaining_usd")
    if remaining is not None and float(remaining) <= 0:
        return _blocked(None, "quota_exhausted", "the spend cap is reached"), execution
    return _available(None), execution


def report(
    home: Path,
    project_id: str | None,
    provider_status: dict[str, Any] | PrometheosError | None,
) -> dict[str, Any]:
    by_group: dict[str, list[registry.Operation]] = {}
    for operation in registry.operations():
        by_group.setdefault(operation.group, []).append(operation)
    features = []
    for group, description in registry.CAPABILITY_GROUPS.items():
        ops = by_group.get(group, [])
        execution = EXECUTION.get(group, "local")
        if group == "provider":
            probe, execution = provider_probe(provider_status)
        elif group in CORE_FEATURES:
            probe = _available(__version__)
        else:
            try:
                probe = PROBES[group](home)
            except (OSError, PrometheosError) as exc:
                message = exc.message if isinstance(exc, PrometheosError) else type(exc).__name__
                probe = _blocked(None, "not_installed", f"could not be checked: {message}")
        features.append(
            {
                "feature": group,
                "description": description,
                **probe,
                # The authority the code enforces today. A declared target that CP10.2
                # still has to enforce is listed apart, never claimed (round 2, R4).
                "required_authority": sorted({o.current_authority for o in ops}),
                "authority_pending": [
                    {"operation": o.id, "declared": o.authority, "enforced": o.current_authority}
                    for o in sorted(ops, key=lambda x: x.id)
                    if o.enforced is not None
                ],
                "execution": execution,
                "cost_class": COST.get(group, "free"),
                "operations": sorted(o.id for o in ops),
            }
        )
    return {
        "registry_version": registry.REGISTRY_VERSION,
        "software_version": __version__,
        "project_id": project_id,
        "authorities": dict(registry.AUTHORITIES),
        "features": features,
    }
