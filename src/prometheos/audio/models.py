"""Local speech-model manager: plan, explicit install, status and removal (decision 0013).

Nothing is downloaded implicitly. ``plan`` is a pure preview (repository, pinned revision,
files and sizes, licence, target directory, hosts contacted, total size) with a digest;
``install`` downloads only when handed that digest, verifies every file against pinned
digests, and renames a staged directory into place atomically. Models live under the
PrometheOS home, never in a project store, and are not part of backups.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError

ALLOWED_HOST_SUFFIXES = ("huggingface.co", ".hf.co", ".huggingface.co")
MAX_REDIRECTS = 5
CHUNK = 1024 * 1024
MANIFEST = "manifest.json"


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str | None  # LFS objects: the pinned content digest
    git_oid: str | None  # other files: git blob id (sha1 of "blob <size>\0" + content)


@dataclass(frozen=True)
class ModelSpec:
    key: str
    title: str
    engine: str
    repo: str
    revision: str
    license: str
    files: tuple[ModelFile, ...]
    base_url: str = "https://huggingface.co"

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def url(self, name: str) -> str:
        return f"{self.base_url}/{self.repo}/resolve/{self.revision}/{urllib.parse.quote(name)}"


FASTER_WHISPER_BASE = ModelSpec(
    key="faster-whisper-base",
    title="faster-whisper base (multilingual, CTranslate2 int8/float16)",
    engine="faster-whisper",
    repo="Systran/faster-whisper-base",
    revision="ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
    license="MIT",
    files=(
        ModelFile("config.json", 2309, None, "867cf1a0fece1394e01d55e287ba2f09a577c046"),
        ModelFile(
            "model.bin",
            145217532,
            "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9",
            None,
        ),
        ModelFile("tokenizer.json", 2203239, None, "7818adb6de9fa3064d3ff81226fdd675be1f6344"),
        ModelFile("vocabulary.txt", 459861, None, "c9074644d9d1205686f16d411564729461324b75"),
    ),
)
KNOWN: dict[str, ModelSpec] = {FASTER_WHISPER_BASE.key: FASTER_WHISPER_BASE}
DEFAULT_MODEL = FASTER_WHISPER_BASE.key


class Response(Protocol):
    status: int
    headers: dict[str, str]

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


Fetcher = Callable[[str], Response]


MODELS_DIR_ENV = "PROMETHEOS_MODELS_DIR"


def models_dir(home: Path) -> Path:
    """Where models live: ``<home>/models``, or the folder named by ``PROMETHEOS_MODELS_DIR``
    (one shared copy for several homes; the folder is only ever read and written by this module)."""
    override = os.environ.get(MODELS_DIR_ENV)
    return Path(override) if override else home / "models"


def redact_location(home: Path, document: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``document`` whose ``location`` is relative to a ``<PrometheOS home>`` alias."""
    location = document.get("location")
    if not isinstance(location, str):
        return document
    base = models_dir(home)
    try:
        relative = Path(location).relative_to(base).as_posix()
        shown = f"<PrometheOS models>/{relative}"
    except ValueError:
        shown = "<PrometheOS models>"
    return {**document, "location": shown}


def spec_for(key: str) -> ModelSpec:
    spec = KNOWN.get(key)
    if spec is None:
        raise PrometheosError(
            "validation_error", "unknown model", {"model": key, "known": sorted(KNOWN)}
        )
    return spec


def target_dir(home: Path, spec: ModelSpec) -> Path:
    return models_dir(home) / spec.key / spec.revision


def plan(home: Path, key: str = DEFAULT_MODEL, base_url: str | None = None) -> dict[str, Any]:
    spec = spec_for(key)
    if base_url is not None:  # tests only: never exposed by a transport
        spec = ModelSpec(**{**spec.__dict__, "base_url": base_url})
    target = target_dir(home, spec)
    summary: dict[str, Any] = {
        "model": spec.key,
        "title": spec.title,
        "engine": spec.engine,
        "repository": f"{spec.base_url}/{spec.repo}",
        "revision": spec.revision,
        "license": spec.license,
        "files": [
            {
                "name": f.name,
                "bytes": f.size,
                "expected_sha256": f.sha256,
                "expected_git_blob": f.git_oid,
            }
            for f in spec.files
        ],
        "total_bytes": spec.total_bytes,
        "total_mib": round(spec.total_bytes / 1024 / 1024, 1),
        "location": str(target),
        "hosts_contacted": sorted(
            {urllib.parse.urlparse(spec.base_url).hostname or "", "redirect targets under *.hf.co"}
            | proxy_hosts()
        ),
        "transmits": "only the file requests (repository, revision, file name); no project data",
        "installed": installed(home, spec),
    }
    summary["plan_sha256"] = canonical_sha256(
        {k: v for k, v in summary.items() if k != "installed"}
    )
    return summary


def proxy_hosts() -> set[str]:
    """Proxies the downloader will use (environment or system settings). The plan names them so
    "hosts contacted" stays literally true (review cp6-a R2-I2); never any credentials."""
    found: set[str] = set()
    for scheme, address in urllib.request.getproxies().items():
        if scheme not in ("http", "https", "all"):
            continue
        parsed = urllib.parse.urlparse(address if "//" in address else f"//{address}")
        if parsed.hostname:
            found.add(f"via proxy {parsed.hostname}" + (f":{parsed.port}" if parsed.port else ""))
    return found


def installed(home: Path, spec: ModelSpec) -> bool:
    target = target_dir(home, spec)
    manifest = target / MANIFEST
    if not manifest.is_file():
        return False
    return all(
        (target / f.name).is_file() and (target / f.name).stat().st_size == f.size
        for f in spec.files
    )


def installed_path(home: Path, key: str = DEFAULT_MODEL) -> Path:
    spec = spec_for(key)
    if not installed(home, spec):
        raise PrometheosError(
            "unsupported",
            "the speech model is not installed; review and install it explicitly "
            f"(prometheos audio model plan, then install) - {spec.total_bytes // 1024 // 1024} MiB",
            {"model": key},
        )
    return target_dir(home, spec)


def _host_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https":
        return False
    return host == "huggingface.co" or any(host.endswith(s) for s in ALLOWED_HOST_SUFFIXES[1:])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def default_fetch(url: str) -> Response:
    opener = urllib.request.build_opener(_NoRedirect)  # public model hosts: proxies are fine
    request = urllib.request.Request(url, headers={"User-Agent": "prometheos-model-install"})  # noqa: S310
    try:
        response: Response = opener.open(request, timeout=60)
        return response
    except urllib.error.HTTPError as err:
        return err  # type: ignore[return-value]


def _open_following(fetch: Fetcher, url: str, *, allow_http: bool) -> Response:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not (_host_allowed(current) or (allow_http and current.startswith("http://127.0.0.1"))):
            raise PrometheosError(
                "policy_denied",
                "download host is not allowed",
                {"host": urllib.parse.urlparse(current).hostname},
            )
        response = fetch(current)
        if response.status in (301, 302, 303, 307, 308):
            location = response.headers.get("Location") or response.headers.get("location")
            response.close()
            if not location:
                raise PrometheosError("io_error", "redirect without a location", {})
            current = urllib.parse.urljoin(current, location)
            continue
        if response.status != 200:
            response.close()
            raise PrometheosError(
                "source_unavailable", f"download failed with HTTP {response.status}", {"url": url}
            )
        return response
    raise PrometheosError("io_error", "too many redirects", {})


def _download(
    fetch: Fetcher,
    spec: ModelSpec,
    item: ModelFile,
    dest: Path,
    progress: Callable[[str, int, int], None] | None,
    allow_http: bool,
) -> str:
    response = _open_following(fetch, spec.url(item.name), allow_http=allow_http)
    sha = hashlib.sha256()
    git = hashlib.sha1(f"blob {item.size}\0".encode(), usedforsecurity=False)
    received = 0
    try:
        with open(dest, "xb") as sink:
            while chunk := response.read(CHUNK):
                received += len(chunk)
                if received > item.size:
                    raise PrometheosError(
                        "validation_error",
                        "download is larger than the pinned size",
                        {"file": item.name},
                    )
                sha.update(chunk)
                git.update(chunk)
                sink.write(chunk)
                if progress:
                    progress(item.name, received, item.size)
            sink.flush()
            os.fsync(sink.fileno())
    finally:
        response.close()
    if received != item.size:
        raise PrometheosError(
            "validation_error", "download is smaller than the pinned size", {"file": item.name}
        )
    if item.sha256 is not None and sha.hexdigest() != item.sha256:
        raise PrometheosError(
            "validation_error",
            "downloaded file does not match its pinned SHA-256",
            {"file": item.name},
        )
    if item.git_oid is not None and git.hexdigest() != item.git_oid:
        raise PrometheosError(
            "validation_error",
            "downloaded file does not match its pinned git blob id",
            {"file": item.name},
        )
    return sha.hexdigest()


def install(
    home: Path,
    key: str,
    plan_sha256: str,
    *,
    fetch: Fetcher | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Download the model after the owner confirmed ``plan_sha256`` (from ``plan``)."""
    current = plan(home, key, base_url)
    if plan_sha256 != current["plan_sha256"]:
        raise PrometheosError(
            "revision_conflict",
            "the plan changed or the digest is wrong; review the plan again",
            {"plan_sha256": current["plan_sha256"]},
        )
    spec = spec_for(key)
    if base_url is not None:
        spec = ModelSpec(**{**spec.__dict__, "base_url": base_url})
    target = target_dir(home, spec)
    if installed(home, spec):
        return {"model": key, "status": "already_installed", "location": str(target)}
    root = models_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{uuid.uuid4()}"
    staging.mkdir()
    try:
        digests = {}
        for item in spec.files:
            digests[item.name] = _download(
                fetch or default_fetch,
                spec,
                item,
                staging / item.name,
                progress,
                allow_http=base_url is not None,
            )
        (staging / MANIFEST).write_text(
            json.dumps(
                {
                    "model": spec.key,
                    "repository": spec.repo,
                    "revision": spec.revision,
                    "license": spec.license,
                    "files": [
                        {"name": f.name, "bytes": f.size, "sha256": digests[f.name]}
                        for f in spec.files
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, target)
        except OSError as exc:
            if installed(home, spec):  # another process finished first
                return {"model": key, "status": "already_installed", "location": str(target)}
            raise PrometheosError("io_error", f"could not place the model: {exc}", {}) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "model": key,
        "status": "installed",
        "location": str(target),
        "revision": spec.revision,
        "bytes": spec.total_bytes,
        "files": digests,
    }


def status(home: Path, verify: bool = False) -> dict[str, Any]:
    out = []
    for spec in KNOWN.values():
        present = installed(home, spec)
        entry: dict[str, Any] = {
            "model": spec.key,
            "engine": spec.engine,
            "revision": spec.revision,
            "license": spec.license,
            "installed": present,
            "location": str(target_dir(home, spec)),
            "total_bytes": spec.total_bytes,
        }
        if present and verify:
            entry["verified"] = _verify(target_dir(home, spec))
        out.append(entry)
    return {"models": out}


def _verify(target: Path) -> bool:
    manifest = json.loads((target / MANIFEST).read_text(encoding="utf-8"))
    for item in manifest["files"]:
        digest = hashlib.sha256()
        with open(target / item["name"], "rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
        if digest.hexdigest() != item["sha256"]:
            return False
    return True


def remove(home: Path, key: str) -> dict[str, Any]:
    spec = spec_for(key)
    folder = models_dir(home) / spec.key
    existed = folder.exists()
    shutil.rmtree(folder, ignore_errors=True)
    return {"model": key, "removed": existed}
