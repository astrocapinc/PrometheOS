"""The speech-model manager: nothing downloads without a confirmed plan, every file is checked
against pinned digests, redirects stay on allowed hosts. Fake transport only (no network)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from prometheos.audio import models
from prometheos.domain.models import PrometheosError

BIG = b"weights" * 1000
SMALL = b'{"config": true}'


def git_oid(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()


SPEC = models.ModelSpec(
    key="test-model",
    title="Test model",
    engine="faster-whisper",
    repo="Example/test-model",
    revision="a" * 40,
    license="MIT",
    files=(
        models.ModelFile("config.json", len(SMALL), None, git_oid(SMALL)),
        models.ModelFile("model.bin", len(BIG), hashlib.sha256(BIG).hexdigest(), None),
    ),
)


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self._at = 0

    def read(self, size: int) -> bytes:
        chunk = self._body[self._at : self._at + size]
        self._at += len(chunk)
        return chunk

    def close(self) -> None:
        pass


class FakeNetwork:
    def __init__(self, routes: dict[str, FakeResponse | bytes]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def __call__(self, url: str) -> FakeResponse:
        self.requested.append(url)
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(404)
        return route if isinstance(route, FakeResponse) else FakeResponse(200, route)


def good_network() -> FakeNetwork:
    base = f"https://huggingface.co/{SPEC.repo}/resolve/{SPEC.revision}"
    return FakeNetwork(
        {
            f"{base}/config.json": FakeResponse(
                307, headers={"Location": f"/api/resolve-cache/{SPEC.repo}/config.json"}
            ),
            "https://huggingface.co/api/resolve-cache/Example/test-model/config.json": SMALL,
            f"{base}/model.bin": FakeResponse(
                302, headers={"Location": "https://cas-bridge.xethub.hf.co/obj/abc"}
            ),
            "https://cas-bridge.xethub.hf.co/obj/abc": BIG,
        }
    )


@pytest.fixture(autouse=True)
def test_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Synthetic install/remove tests own only their temporary home's cache. A staged real
    # model override belongs to tests marked real_model and must never be mutated here.
    monkeypatch.delenv(models.MODELS_DIR_ENV, raising=False)
    monkeypatch.setitem(models.KNOWN, SPEC.key, SPEC)


def test_pinned_real_model_facts_are_the_ones_the_owner_approved() -> None:
    real = models.spec_for("faster-whisper-base")
    assert real.repo == "Systran/faster-whisper-base" and real.license == "MIT"
    assert real.revision == "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
    assert {f.name: f.size for f in real.files}["model.bin"] == 145217532
    assert real.total_bytes == 145217532 + 2203239 + 459861 + 2309


def test_plan_is_a_pure_preview_with_size_licence_revision_and_location(tmp_path: Path) -> None:
    plan = models.plan(tmp_path, SPEC.key)
    assert plan["license"] == "MIT" and plan["revision"] == "a" * 40
    assert plan["total_bytes"] == len(BIG) + len(SMALL)
    assert plan["location"] == str(tmp_path / "models" / "test-model" / ("a" * 40))
    assert plan["installed"] is False and "no project data" in plan["transmits"]
    assert {f["name"] for f in plan["files"]} == {"config.json", "model.bin"}
    assert not (tmp_path / "models").exists()  # nothing created by looking
    assert models.plan(tmp_path, SPEC.key)["plan_sha256"] == plan["plan_sha256"]


def test_install_needs_the_confirmed_plan_digest_and_downloads_nothing_without_it(
    tmp_path: Path,
) -> None:
    network = good_network()
    with pytest.raises(PrometheosError) as err:
        models.install(tmp_path, SPEC.key, "0" * 64, fetch=network)
    assert err.value.code == "revision_conflict" and network.requested == []
    assert not (tmp_path / "models").exists()


def test_install_verifies_places_atomically_and_is_idempotent(tmp_path: Path) -> None:
    network = good_network()
    plan = models.plan(tmp_path, SPEC.key)
    seen: list[tuple[str, int, int]] = []
    result = models.install(
        tmp_path, SPEC.key, plan["plan_sha256"], fetch=network,
        progress=lambda n, got, total: seen.append((n, got, total)),
    )  # fmt: skip
    assert result["status"] == "installed" and result["bytes"] == len(BIG) + len(SMALL)
    target = models.installed_path(tmp_path, SPEC.key)
    assert (target / "model.bin").read_bytes() == BIG
    assert seen and seen[-1][1] == seen[-1][2]
    assert not [p for p in (tmp_path / "models").iterdir() if p.name.startswith(".staging")]
    entry = next(
        m for m in models.status(tmp_path, verify=True)["models"] if m["model"] == SPEC.key
    )
    assert entry["installed"] is True and entry["verified"] is True
    again = models.install(tmp_path, SPEC.key, plan["plan_sha256"], fetch=network)
    assert again["status"] == "already_installed"
    assert models.plan(tmp_path, SPEC.key)["installed"] is True
    assert models.remove(tmp_path, SPEC.key)["removed"] is True
    with pytest.raises(PrometheosError) as err:
        models.installed_path(tmp_path, SPEC.key)
    assert err.value.code == "unsupported" and "install" in err.value.message


def test_tampered_download_is_refused_and_leaves_nothing(tmp_path: Path) -> None:
    network = good_network()
    network.routes["https://cas-bridge.xethub.hf.co/obj/abc"] = b"X" * len(BIG)  # same size
    plan = models.plan(tmp_path, SPEC.key)
    with pytest.raises(PrometheosError, match="SHA-256"):
        models.install(tmp_path, SPEC.key, plan["plan_sha256"], fetch=network)
    assert not (tmp_path / "models" / "test-model").exists()
    assert not [p for p in (tmp_path / "models").iterdir()]  # staging cleaned


def test_wrong_size_downloads_are_refused(tmp_path: Path) -> None:
    for payload, message in ((BIG + b"extra", "larger"), (BIG[:-1], "smaller")):
        network = good_network()
        network.routes["https://cas-bridge.xethub.hf.co/obj/abc"] = payload
        plan = models.plan(tmp_path, SPEC.key)
        with pytest.raises(PrometheosError, match=message):
            models.install(tmp_path, SPEC.key, plan["plan_sha256"], fetch=network)


def test_small_file_must_match_its_git_blob_id(tmp_path: Path) -> None:
    network = good_network()
    network.routes["https://huggingface.co/api/resolve-cache/Example/test-model/config.json"] = (
        b'{"config": fals}'  # same length, different content
    )
    plan = models.plan(tmp_path, SPEC.key)
    with pytest.raises(PrometheosError, match="git blob"):
        models.install(tmp_path, SPEC.key, plan["plan_sha256"], fetch=network)


def test_redirects_off_the_allowed_hosts_are_refused(tmp_path: Path) -> None:
    network = good_network()
    network.routes[f"https://huggingface.co/{SPEC.repo}/resolve/{SPEC.revision}/config.json"] = (
        FakeResponse(302, headers={"Location": "https://evil.example/steal"})
    )
    plan = models.plan(tmp_path, SPEC.key)
    with pytest.raises(PrometheosError) as err:
        models.install(tmp_path, SPEC.key, plan["plan_sha256"], fetch=network)
    assert err.value.code == "policy_denied"
    assert "https://evil.example/steal" not in network.requested  # never even contacted
    for spoof in ("https://huggingface.co.evil.example/x", "http://huggingface.co/x"):
        assert models._host_allowed(spoof) is False
    assert models._host_allowed("https://cas-bridge.xethub.hf.co/x") is True


def test_redirect_loops_and_http_errors_stop(tmp_path: Path) -> None:
    base = f"https://huggingface.co/{SPEC.repo}/resolve/{SPEC.revision}/config.json"
    loop = FakeNetwork({base: FakeResponse(302, headers={"Location": base})})
    plan = models.plan(tmp_path, SPEC.key)
    with pytest.raises(PrometheosError, match="redirects"):
        models.install(tmp_path, SPEC.key, plan["plan_sha256"], fetch=loop)
    missing = FakeNetwork({})
    with pytest.raises(PrometheosError, match="HTTP 404"):
        models.install(tmp_path, SPEC.key, plan["plan_sha256"], fetch=missing)


def test_unknown_model_is_a_validation_error(tmp_path: Path) -> None:
    with pytest.raises(PrometheosError) as err:
        models.plan(tmp_path, "gpt-nothing")
    assert err.value.code == "validation_error"
