"""Regressions for the CP1 r1 independent review (reviewer-subagent-cp1-a).

Each test reproduces a reported defect against the fixed code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cp1_support import AGENT, FULL, OWNER, Env, create_entry

from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    from prometheos.app import sources

    monkeypatch.setattr(sources, "INLINE_LIMIT", 1024)  # artifact.bin goes to media
    return Env(tmp_path)


def _media_item(env: Env) -> dict:
    item = env.import_files(["artifact.bin"], retention="copy")["plan"]["items"][0]
    assert item["storage"] == "media"
    return item


# Finding 1 (blocking): availability truthful on every read path, corrupt bytes included.
def test_availability_is_computed_on_list_and_as_of_reads(env: Env) -> None:
    item = _media_item(env)
    payload = env.import_files(["baseline.md"], retention="copy")["plan"]["items"][0]
    seq = env.seq()
    blob = env.store_dir / "media" / item["sha256"][:2] / item["sha256"]
    blob.write_bytes(b"x" * blob.stat().st_size)  # same size, different bytes
    os.utime(blob, ns=(blob.stat().st_atime_ns, blob.stat().st_mtime_ns + 10_000_000))

    def availability(record_id: str, at_seq: int | None = None) -> str:
        return env.svc.get(env.project_id, OWNER, record_id, at_seq)["body"]["availability"]

    assert availability(item["version_id"]) == "missing"
    assert availability(item["version_id"], seq) == "missing"
    listed = {
        r["id"]: r["body"]["availability"]
        for r in env.svc.list_records(env.project_id, OWNER, kind="version")["items"]
    }
    assert listed[item["version_id"]] == "missing"
    assert listed[payload["version_id"]] == "retained"
    blob.unlink()
    assert availability(item["version_id"]) == "missing"


def test_verification_cache_does_not_hide_replaced_bytes(env: Env) -> None:
    item = _media_item(env)
    assert (
        env.svc.get(env.project_id, OWNER, item["version_id"], None)["body"]["availability"]
        == "retained"
    )
    blob = env.store_dir / "media" / item["sha256"][:2] / item["sha256"]
    original = blob.read_bytes()
    blob.write_bytes(bytes(reversed(original)))  # same size; mtime changes
    assert (
        env.svc.get(env.project_id, OWNER, item["version_id"], None)["body"]["availability"]
        == "missing"
    )


# Finding 2 (major): orphan collection cannot race an in-flight media import.
def test_orphan_collection_waits_for_media_import(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app import sources

    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["artifact.bin"],
        retention="copy",
        sensitivity="normal",
    )
    original = sources._stage_media
    attempts: list[str] = []

    def stage_then_collect(store, source, sha):  # type: ignore[no-untyped-def]
        key = original(store, source, sha)
        try:
            env.svc.orphans(env.project_id, collect=True, grace_seconds=0)
            attempts.append("collected")
        except PrometheosError as exc:
            attempts.append(exc.code)
        return key

    monkeypatch.setattr(sources, "_stage_media", stage_then_collect)
    env.svc.import_commit(env.project_id, plan["plan_id"])
    assert attempts == ["busy"]
    version = env.svc.get(env.project_id, OWNER, plan["items"][0]["version_id"], None)
    assert version["body"]["availability"] == "retained"


# Finding 3 (major): restore validates rebinding before anything becomes visible.
def test_invalid_rebind_switches_nothing(env: Env, tmp_path: Path) -> None:
    backup = Path(env.svc.backup(env.project_id, tmp_path / "b.zip", FULL)["backup"])
    for rebind in ({"unknown": str(tmp_path)}, {"research": str(tmp_path / "does-not-exist")}):
        target = tmp_path / f"r-{len(rebind)}-{abs(hash(str(rebind)))}"
        with pytest.raises(PrometheosError) as err:
            env.svc.restore(backup, target, rebind=rebind, caller=FULL)
        assert err.value.code == "validation_error"
        assert not target.exists()
        assert env.svc.registry.lookup(env.project_id) == env.store_dir
    assert not list(tmp_path.glob(".r-*.restore-*"))


# Finding 4 (major): one source per file, whatever the plan history or spelling.
def test_two_stale_plans_cannot_create_two_sources(env: Env) -> None:
    first = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["baseline.md"],
        retention="copy",
        sensitivity="normal",
    )
    second = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["baseline.md"],
        retention="copy",
        sensitivity="normal",
    )
    env.svc.import_commit(env.project_id, first["plan_id"])
    with pytest.raises(PrometheosError) as err:
        env.svc.import_commit(env.project_id, second["plan_id"])
    assert err.value.code == "revision_conflict"
    sources = env.svc.list_records(env.project_id, OWNER, kind="source")["items"]
    assert len(sources) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive NTFS names")
def test_case_variant_names_resolve_to_one_identity(env: Env) -> None:
    env.import_files(["baseline.md"], retention="copy")
    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["BASELINE.MD"],
        retention="copy",
        sensitivity="normal",
    )
    item = plan["items"][0]
    assert item["relative_path"] == "baseline.md"
    assert item["action"] == "unchanged"


# Finding 5 (major): no scope leaks through source check, project info or apply.
def test_source_check_and_project_info_respect_scope(env: Env) -> None:
    hidden = env.import_files(["meeting.txt"], sensitivity="restricted")["plan"]["items"][0]
    env.import_files(["baseline.md"])
    for record_id in (hidden["version_id"], hidden["source_id"]):
        with pytest.raises(PrometheosError) as err:
            env.svc.source_check(env.project_id, [record_id], save=False, caller=AGENT)
        assert err.value.code == "not_found"
    owner = env.svc.source_check(env.project_id, [hidden["version_id"]], save=False, caller=OWNER)
    assert owner["observations"][0]["retained_digest_ok"] is True
    agent_counts = env.svc.project_info(env.project_id, AGENT)["record_counts"]
    owner_counts = env.svc.project_info(env.project_id, OWNER)["record_counts"]
    assert agent_counts["source"] == 1 and owner_counts["source"] == 2


def test_save_retry_with_request_id_replays(env: Env) -> None:
    item = env.import_files(["baseline.md"])["plan"]["items"][0]
    rid = "4f8a7c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
    first = env.svc.source_check(env.project_id, [item["version_id"]], save=True, request_id=rid)
    again = env.svc.source_check(env.project_id, [item["version_id"]], save=True, request_id=rid)
    assert first["commit"]["replayed"] is False and again["commit"]["replayed"] is True
    assert again["commit"]["project_seq"] == first["commit"]["project_seq"]


# Finding 6 (major): hostile manifests produce one JSON error document, never a traceback.
@pytest.mark.parametrize(
    "manifest",
    [
        b"[]",
        b'{"format": "prometheos-backup-v1"}',
        b'{"format": 7}',
        b"null",
        b'{"format":"prometheos-backup-v1","database":{"path":"../x","sha256":"a","bytes":1}}',
    ],
)
def test_malformed_manifest_is_a_clean_cli_error(env: Env, tmp_path: Path, manifest: bytes) -> None:
    good = Path(env.svc.backup(env.project_id, tmp_path / "good.zip", FULL)["backup"])
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad, "w") as zout:
        for info in zin.infolist():
            data = manifest if info.filename == "manifest.json" else zin.read(info)
            zout.writestr(info.filename, data)
    for args in (
        ["backup", "verify", str(bad)],
        ["backup", "restore", str(bad), "--into", str(tmp_path / "x")],
    ):
        proc = subprocess.run(
            [sys.executable, "-m", "prometheos", *args, "--json"],
            env={**os.environ, "PROMETHEOS_HOME": str(env.home), "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 2, proc.stderr
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        assert len(lines) == 1 and json.loads(lines[0])["error"]["code"] == "validation_error"
        assert "Traceback" not in proc.stderr
    assert not (tmp_path / "x").exists()


# Minor: line numbering follows LF only (form feed, U+2028 are text, not breaks).
def test_quote_lines_split_on_newline_only(env: Env) -> None:
    import hashlib

    tricky = env.research / "tricky.txt"
    first_line = "alpha\x0cbeta" + chr(0x2028) + "gamma"
    tricky.write_bytes((first_line + "\r\nsecond line\n").encode())
    item = env.import_files(["tricky.txt"])["plan"]["items"][0]
    evidence = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": 1, "end_line": 1},
        "quote_sha256": hashlib.sha256(first_line.encode()).hexdigest(),
    }
    env.apply([create_entry(evidence=[evidence])])
    second = dict(
        evidence,
        locator={"type": "text", "start_line": 2, "end_line": 2},
        quote_sha256=hashlib.sha256(b"second line").hexdigest(),
    )
    env.apply([create_entry(evidence=[second])])


# Round 2 (review of r3).
def test_agent_create_with_hidden_id_is_a_plain_conflict(env: Env) -> None:
    from prometheos.domain.ids import new_id

    hidden = new_id("entry")
    env.apply([create_entry(hidden, sensitivity="restricted")], caller=OWNER)
    visible = new_id("entry")
    env.apply([create_entry(visible)])
    errors = []
    for record_id in (hidden, visible):
        with pytest.raises(PrometheosError) as err:
            env.apply([create_entry(record_id)])
        errors.append((err.value.code, err.value.message))
    assert errors[0] == errors[1] == ("revision_conflict", "record already exists")


def test_cli_owner_scope_reaches_apply_and_source_check(env: Env, tmp_path: Path) -> None:
    from prometheos.domain.ids import new_id, new_request_id

    hidden = env.import_files(["meeting.txt"], sensitivity="restricted")["plan"]["items"][0]
    entry = new_id("entry")
    env.apply([create_entry(entry, sensitivity="restricted")], caller=OWNER)
    revise = {
        "schema_version": 1,
        "project_id": env.project_id,
        "request_id": new_request_id(),
        "expected_seq": env.seq(),
        "operations": [
            {
                "op": "revise",
                "id": entry,
                "base_rev": 1,
                "sensitivity": "restricted",
                "occurred_at": None,
                "body": create_entry(title="Revised")["body"],
            }
        ],
    }
    base = [sys.executable, "-m", "prometheos"]
    cli_env = {**os.environ, "PROMETHEOS_HOME": str(env.home), "PYTHONIOENCODING": "utf-8"}

    def run(*args: str, stdin: bytes | None = None) -> tuple[int, dict]:
        proc = subprocess.run(
            [*base, *args, "--json"], env=cli_env, input=stdin, capture_output=True, timeout=120
        )
        return proc.returncode, json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])

    raw = json.dumps(revise).encode()
    code, out = run("apply", "--file", "-", stdin=raw)
    assert code == 2 and out["error"]["code"] == "not_found"  # agent scope by default
    from test_cp1_cli import synthetic_capability

    cap = tmp_path / "read.cap"
    cap.write_text(synthetic_capability(tmp_path, env.project_id, "read_sensitive"), "utf-8")
    owner = ["--scope", "owner", "--capability-file", str(cap)]
    code, out = run("apply", "--file", "-", "--scope", "owner", stdin=raw)
    assert code == 4  # owner scope without a capability is refused
    code, out = run("apply", "--file", "-", *owner, stdin=raw)
    assert code == 0 and out["results"][0]["rev"] == 2
    rid = "6b1f0d2e-3c4a-4b5d-9e6f-7a8b9c0d1e2f"
    code, first = run(
        "source", "check", hidden["version_id"], *owner, "--save", "--request-id", rid
    )
    assert code == 0 and first["saved"] is True
    code, again = run(
        "source", "check", hidden["version_id"], *owner, "--save", "--request-id", rid
    )
    assert code == 0 and again["commit"]["replayed"] is True


def test_saved_failed_digest_check_makes_bytes_missing(env: Env) -> None:
    item = _media_item(env)
    blob = env.store_dir / "media" / item["sha256"][:2] / item["sha256"]
    stat = blob.stat()
    blob.write_bytes(bytes(reversed(blob.read_bytes())))
    os.utime(blob, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same size and mtime
    saved = env.svc.source_check(env.project_id, [item["version_id"]], save=True, caller=OWNER)
    assert saved["observations"][0]["retained_digest_ok"] is False
    version = env.svc.get(env.project_id, OWNER, item["version_id"], None)
    assert version["body"]["availability"] == "missing"


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive NTFS names")
def test_renaming_case_on_disk_keeps_one_source(env: Env) -> None:
    env.import_files(["baseline.md"])
    os.rename(env.research / "baseline.md", env.research / "Baseline.md")
    (env.research / "Baseline.md").write_text("changed after rename\n", encoding="utf-8")
    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["Baseline.md"],
        retention="copy",
        sensitivity="normal",
    )
    assert plan["items"][0]["action"] == "new_version"
    env.svc.import_commit(env.project_id, plan["plan_id"])
    assert len(env.svc.list_records(env.project_id, OWNER, kind="source")["items"]) == 1
