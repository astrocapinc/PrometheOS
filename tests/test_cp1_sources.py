"""C01-SOURCE: import plans detect changed bytes; retained versions and reference-only
unavailability are reported truthfully (S02)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from cp1_support import OWNER, Env, counts, create_entry

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def version_ids(result: dict) -> dict[str, str]:
    """Map relative path -> version id from a plan + commit."""
    return {i["relative_path"]: i["version_id"] for i in result["plan"]["items"]}


def test_plan_previews_retention_and_persists_without_mutating_records(env: Env) -> None:
    before = counts(env.store_dir)
    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["baseline.md", "artifact.bin"],
        retention="copy",
        sensitivity="normal",
    )
    assert plan["retained"] == ["baseline.md", "artifact.bin"]
    assert plan["reference_only"] == [] and plan["transmitted"] == []
    storages = {i["relative_path"]: i["storage"] for i in plan["items"]}
    assert storages == {"baseline.md": "body", "artifact.bin": "body"}
    assert (
        plan["items"][0]["sha256"]
        == hashlib.sha256((env.research / "baseline.md").read_bytes()).hexdigest()
    )
    assert counts(env.store_dir) == before  # plans are operational, not research records
    assert env.seq() == 1


def test_s02_retained_history_and_reference_only_unavailability(env: Env) -> None:
    retained = version_ids(env.import_files(["baseline.md"], retention="copy"))
    reference = version_ids(env.import_files(["artifact.bin"], retention="reference"))
    base_v, art_v = retained["baseline.md"], reference["artifact.bin"]
    original = (env.research / "baseline.md").read_bytes()

    base = env.svc.get(env.project_id, OWNER, base_v, None)
    art = env.svc.get(env.project_id, OWNER, art_v, None)
    assert base["body"]["storage"] == "body" and base["body"]["availability"] == "retained"
    assert art["body"]["storage"] == "external" and art["body"]["availability"] == "external"
    assert art["body"]["storage_key"] is None

    # The researcher edits the original and deletes the reference-only artifact.
    (env.research / "baseline.md").write_bytes(original.replace(b"2.40 kJ", b"2.45 kJ"))
    (env.research / "artifact.bin").unlink()

    seq = env.seq()
    observed = env.svc.source_check(env.project_id, [base_v, art_v], save=False)
    assert env.seq() == seq  # default check is read-only
    by_id = {o["version_id"]: o for o in observed["observations"]}
    assert by_id[base_v]["retained_digest_ok"] is True  # historical bytes intact
    assert by_id[base_v]["origin_reachable"] is True
    assert by_id[base_v]["origin_matches_version"] is False  # current file changed
    assert by_id[art_v]["retained_digest_ok"] is None  # nothing retained, nothing claimed
    assert by_id[art_v]["origin_reachable"] is False
    assert by_id[art_v]["error"] == "source_unavailable"

    saved = env.svc.source_check(env.project_id, [base_v, art_v], save=True)
    assert saved["saved"] and saved["commit"]["project_seq"] == seq + 1
    assert env.svc.get(env.project_id, OWNER, art_v, None)["body"]["availability"] == "missing"
    base_after = env.svc.get(env.project_id, OWNER, base_v, None)
    assert base_after["body"]["availability"] == "retained"
    assert base_after["body"]["sha256"] == hashlib.sha256(original).hexdigest()

    # Re-importing the edited file creates a second version; the first stays verifiable.
    again = env.import_files(["baseline.md"], retention="copy")
    item = again["plan"]["items"][0]
    assert item["action"] == "new_version"
    assert item["source_id"] == base["body"]["source_id"]
    recheck = env.svc.source_check(env.project_id, [base_v, item["version_id"]], save=False)
    states = {o["version_id"]: o for o in recheck["observations"]}
    assert states[base_v]["retained_digest_ok"] is True
    assert states[base_v]["origin_matches_version"] is False
    assert states[item["version_id"]]["origin_matches_version"] is True


def test_commit_rejects_bytes_changed_after_plan(env: Env) -> None:
    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["plant-result.md"],
        retention="copy",
        sensitivity="normal",
    )
    path = env.research / "plant-result.md"
    path.write_bytes(path.read_bytes() + b"\nedited after planning\n")
    before = counts(env.store_dir)
    with pytest.raises(PrometheosError) as err:
        env.svc.import_commit(env.project_id, plan["plan_id"])
    assert err.value.code == "revision_conflict"
    assert counts(env.store_dir) == before
    fresh = env.import_files(["plant-result.md"])
    assert fresh["commit"]["project_seq"] == 2


def test_unchanged_reimport_creates_nothing(env: Env) -> None:
    env.import_files(["meeting.txt"])
    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["meeting.txt"],
        retention="copy",
        sensitivity="normal",
    )
    assert plan["items"][0]["action"] == "unchanged"
    with pytest.raises(PrometheosError) as err:
        env.svc.import_commit(env.project_id, plan["plan_id"])
    assert err.value.code == "validation_error"


def test_commit_retry_is_idempotent(env: Env) -> None:
    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["baseline.md"],
        retention="copy",
        sensitivity="normal",
    )
    first = env.svc.import_commit(env.project_id, plan["plan_id"])
    before = counts(env.store_dir)
    second = env.svc.import_commit(env.project_id, plan["plan_id"])
    assert second["replayed"] is True and first["project_seq"] == second["project_seq"]
    assert counts(env.store_dir) == before


def test_large_binary_uses_staged_media(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from prometheos.app import sources

    monkeypatch.setattr(sources, "INLINE_LIMIT", 1024)
    result = env.import_files(["artifact.bin"], retention="copy")
    item = result["plan"]["items"][0]
    assert item["storage"] == "media"
    version = env.svc.get(env.project_id, OWNER, item["version_id"], None)
    key = version["body"]["storage_key"]
    blob = env.store_dir / "media" / key
    assert blob.read_bytes() == (env.research / "artifact.bin").read_bytes()
    assert key == f"{item['sha256'][:2]}/{item['sha256']}"
    assert list((env.store_dir / "staging").iterdir()) == []


def test_media_limit_enforced(env: Env) -> None:
    info = env.svc.project_info(env.project_id, OWNER)
    settings = info["settings"]
    settings["retention"]["max_media_bytes"] = 100
    env.apply_protected(
        [{"op": "project_update", "base_rev": info["settings_rev"], "settings": settings}]
    )
    with pytest.raises(PrometheosError) as err:
        env.svc.import_plan(
            env.project_id,
            root_alias="research",
            paths=["artifact.bin"],
            retention="copy",
            sensitivity="normal",
        )
    assert err.value.code == "budget_exceeded"


def test_evidence_quotes_must_match_retained_lines(env: Env) -> None:
    result = env.import_files(["baseline.md"], retention="copy")
    item = result["plan"]["items"][0]
    lines = (env.research / "baseline.md").read_text("utf-8").splitlines()
    line_no = next(i for i, line in enumerate(lines, 1) if "2.40 kJ" in line)
    good = hashlib.sha256(lines[line_no - 1].encode()).hexdigest()

    def ev(digest: str | None, start: int = line_no, end: int = line_no) -> dict:
        return {
            "source_id": item["source_id"],
            "version_id": item["version_id"],
            "locator": {"type": "text", "start_line": start, "end_line": end},
            "quote_sha256": digest,
        }

    env.apply([create_entry(title="Quoted", evidence=[ev(good)])])
    for bad, code in (
        (ev(hashlib.sha256(b"2.41 kJ").hexdigest()), "validation_error"),
        (ev(None, 1, 9999), "validation_error"),
    ):
        with pytest.raises(PrometheosError) as err:
            env.apply([create_entry(evidence=[bad])])
        assert err.value.code == code


def test_reference_only_versions_cannot_be_quoted(env: Env) -> None:
    result = env.import_files(["plant-result.md"], retention="reference")
    item = result["plan"]["items"][0]
    evidence = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": 1, "end_line": 1},
        "quote_sha256": hashlib.sha256(b"x").hexdigest(),
    }
    with pytest.raises(PrometheosError) as err:
        env.apply([create_entry(evidence=[evidence])])
    assert err.value.code == "validation_error"
    env.apply([create_entry(evidence=[dict(evidence, quote_sha256=None)])])


def test_derived_entry_inherits_source_sensitivity(env: Env) -> None:
    result = env.import_files(["meeting.txt"], sensitivity="restricted")
    item = result["plan"]["items"][0]
    evidence = {
        "source_id": item["source_id"],
        "version_id": item["version_id"],
        "locator": {"type": "text", "start_line": 1, "end_line": 1},
        "quote_sha256": None,
    }
    with pytest.raises(PrometheosError) as err:
        env.apply([create_entry(evidence=[evidence])], caller=OWNER)
    assert err.value.code == "policy_denied"
    env.apply([create_entry(evidence=[evidence], sensitivity="restricted")], caller=OWNER)
    # Agent scope cannot even tell that the restricted source exists.
    with pytest.raises(PrometheosError) as hidden:
        env.apply([create_entry(evidence=[evidence], sensitivity="restricted")])
    absent = dict(evidence, source_id=new_id("source"), version_id=new_id("version"))
    with pytest.raises(PrometheosError) as missing:
        env.apply([create_entry(evidence=[absent], sensitivity="restricted")])
    assert hidden.value.code == missing.value.code == "not_found"
    assert hidden.value.message == missing.value.message


def test_existing_source_keeps_retention_and_sensitivity(env: Env) -> None:
    env.import_files(["baseline.md"], retention="copy", sensitivity="restricted")
    (env.research / "baseline.md").write_text("changed\n", encoding="utf-8")
    for kwargs, code in (
        ({"retention": "reference", "sensitivity": "restricted"}, "validation_error"),
        ({"retention": "copy", "sensitivity": "normal"}, "policy_denied"),
    ):
        with pytest.raises(PrometheosError) as err:
            env.svc.import_plan(
                env.project_id, OWNER, root_alias="research", paths=["baseline.md"], **kwargs
            )
        assert err.value.code == code


def test_versions_are_immutable_through_apply(env: Env) -> None:
    result = env.import_files(["baseline.md"])
    vid = result["plan"]["items"][0]["version_id"]
    record = env.svc.get(env.project_id, OWNER, vid, None)
    with pytest.raises(PrometheosError) as err:
        env.apply(
            [
                {
                    "op": "revise",
                    "id": vid,
                    "base_rev": 1,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": record["body"],
                }
            ]
        )
    assert err.value.code == "policy_denied"
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        assert (
            conn.execute("SELECT current_rev FROM records WHERE id = ?", (vid,)).fetchone()[0] == 1
        )
    finally:
        conn.close()
