"""CP4 package lifecycle: create, inspect, import, install, update, remove, export, adapt,
verify, revoke/trust, and the second-host projection (C04-PACKAGE)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from cp4_support import import_package, install, skill_md, write_package, zip_folder

from prometheos import packages
from prometheos.domain.models import PrometheosError
from prometheos.skills import host_apply, host_plan


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "code-project"
    folder.mkdir()
    return folder


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_create_inspect_import_is_digest_bound(env: Env, tmp_path: Path) -> None:
    created = env.svc.skill_create("lab-notes", "Keep lab notes.", "CC-BY-4.0", tmp_path / "pk")
    folder = Path(created["created"])
    assert (folder / "skills" / "lab-notes" / "SKILL.md").is_file()
    report = env.svc.skill_inspect(folder)
    assert report["blocking"] == [] and report["rights_resolved"] is True
    assert "cannot certify" in report["note"]
    plan = env.svc.skill_import_plan(folder)
    assert plan["action"] == "import" and plan["status_after"] == "available"
    with pytest.raises(PrometheosError) as exc:
        env.svc.skill_import(folder, "0" * 64)
    assert exc.value.code == "revision_conflict"
    # Editing after review invalidates the reviewed plan.
    (folder / "skills" / "lab-notes" / "examples" / "example.md").write_text("changed", "utf-8")
    with pytest.raises(PrometheosError) as exc:
        env.svc.skill_import(folder, plan["plan_sha256"])
    assert exc.value.code == "revision_conflict"
    env.svc.skill_refresh(folder)
    sidecar = json.loads((folder / "prometheos-package.json").read_text("utf-8"))
    changed = next(c for c in sidecar["components"] if c["path"].endswith("example.md"))
    assert changed["modified"] is True  # refresh records the modification
    result = import_package(env.svc, folder)
    assert result["imported"] is True
    assert env.svc.skill_catalog()["packages"][0]["name"] == "lab-notes"
    assert import_package(env.svc, folder)["imported"] is False  # identical re-import


def test_unresolved_license_is_explicit_and_blocks_export_and_adaptation(
    env: Env, tmp_path: Path
) -> None:
    created = env.svc.skill_create("draft-notes", "Draft notes.", None, tmp_path / "pk")
    folder = Path(created["created"])
    report = env.svc.skill_inspect(folder)
    assert report["rights_resolved"] is False and report["unknown_rights"]
    import_package(env.svc, folder)
    with pytest.raises(PrometheosError) as exc:
        env.svc.skill_export("draft-notes@0.1.0", tmp_path / "out.zip")
    assert exc.value.code == "policy_denied"
    with pytest.raises(PrometheosError):
        env.svc.skill_adapt("draft-notes@0.1.0", "my-notes", tmp_path / "adapted")
    draft = env.svc.skill_export("draft-notes@0.1.0", tmp_path / "draft.zip", draft=True)
    assert draft["distributable"] is False
    with zipfile.ZipFile(tmp_path / "draft.zip") as zf:
        assert zf.read("NOTICE.txt").decode().startswith("LOCAL DRAFT - NOT FOR DISTRIBUTION")


def test_install_projects_skills_with_lock_and_project_record(
    env: Env, tmp_path: Path, project_dir: Path
) -> None:
    import_package(env.svc, write_package(tmp_path / "src"))
    unrelated = project_dir / "README.md"
    unrelated.write_text("my project", "utf-8")
    result = install(env.svc, "synthetic-evidence", "claude-code", project_dir, env.project_id)
    skill = project_dir / ".claude" / "skills" / "synthetic-evidence"
    assert (skill / "SKILL.md").read_text("utf-8") == skill_md("synthetic-evidence")
    assert (skill / "references" / "guide.md").is_file()
    lock = json.loads((project_dir / packages.LOCK_NAME).read_text("utf-8"))
    (installed,) = lock["installs"]
    assert installed["host"] == "claude-code" and installed["version"] == "1.0.0"
    assert installed["files"][".claude/skills/synthetic-evidence/SKILL.md"]["sha256"] == _sha(
        skill / "SKILL.md"
    )
    assert installed["origin"]["kind"] == "folder" and installed["licenses"] == ["MIT"]
    record = result["record"]["results"][0]["id"]
    body = env.svc.get(env.project_id, OWNER, record, None)["body"]
    assert body["status"] == "installed" and body["digest"] == installed["package_digest"]
    assert unrelated.read_text("utf-8") == "my project"
    replan = env.svc.skill_install(
        "synthetic-evidence", "claude-code", project_dir, None, authority=OWNER_ACT
    )["plan"]
    assert {op["action"] for op in replan["operations"]} == {"unchanged"}
    (summary,) = replan["skills"]
    assert summary["name"] == "synthetic-evidence" and summary["skill_md_bytes"] > 0
    assert summary["description"] == "Synthetic test skill synthetic-evidence."


def test_second_host_projection_and_project_local_setup(
    env: Env, tmp_path: Path, project_dir: Path
) -> None:
    (project_dir / "opencode.json").write_text(json.dumps({"theme": "dark"}), "utf-8")
    plan = host_plan("opencode", project_dir, env.project_id)
    assert {Path(f["path"]).name for f in plan["files"]} >= {"opencode.json", "SKILL.md"}
    host_apply(plan)
    config = json.loads((project_dir / "opencode.json").read_text("utf-8"))
    assert config["theme"] == "dark"  # merged, not overwritten
    assert config["mcp"]["prometheos"]["command"][:3] == ["prometheos", "mcp", "serve"]
    assert config["mcp"]["prometheos"]["type"] == "local"
    assert (project_dir / ".opencode" / "skills" / "reconcile-meeting" / "SKILL.md").is_file()
    assert "E02" in plan["notes"][1]
    import_package(env.svc, write_package(tmp_path / "src"))
    install(env.svc, "synthetic-evidence", "opencode", project_dir)
    install(env.svc, "synthetic-evidence", "claude-code", project_dir)
    lock = json.loads((project_dir / packages.LOCK_NAME).read_text("utf-8"))
    assert sorted(i["host"] for i in lock["installs"]) == ["claude-code", "opencode"]
    assert (project_dir / ".opencode" / "skills" / "synthetic-evidence" / "SKILL.md").is_file()


def _v2(tmp_path: Path, **kwargs: Any) -> Path:
    return write_package(
        tmp_path / "src2",
        version="1.1.0",
        files={
            "skills/synthetic-evidence/SKILL.md": skill_md("synthetic-evidence", "Version two."),
            "skills/synthetic-evidence/examples/new.md": "New example.\n",
        },
        licenses={
            "skills/synthetic-evidence/SKILL.md": "Apache-2.0",
            "skills/synthetic-evidence/examples/new.md": "MIT",
        },
        capabilities=["project_read", "project_write"],
        **kwargs,
    )


def test_update_preserves_local_edits_or_needs_explicit_resolution(
    env: Env, tmp_path: Path, project_dir: Path
) -> None:
    import_package(env.svc, write_package(tmp_path / "src"))
    install(env.svc, "synthetic-evidence", "claude-code", project_dir, env.project_id)
    skill = project_dir / ".claude" / "skills" / "synthetic-evidence"
    (skill / "SKILL.md").write_text(skill_md("synthetic-evidence", "My local edit."), "utf-8")
    import_package(env.svc, _v2(tmp_path))
    plan = env.svc.skill_install(
        "synthetic-evidence@1.1.0", "claude-code", project_dir, None, authority=OWNER_ACT
    )["plan"]
    assert plan["kind"] == "package_update"
    assert plan["conflicts"] == [
        {
            "path": ".claude/skills/synthetic-evidence/SKILL.md",
            "reason": "locally modified and changed by the package update",
        }
    ]
    assert plan["changes"]["capabilities_added"] == ["project_write"]
    assert any("Apache-2.0" in change for change in plan["changes"]["licenses"])
    with pytest.raises(PrometheosError) as exc:
        env.svc.skill_install(
            "synthetic-evidence@1.1.0",
            "claude-code",
            project_dir,
            plan["plan_sha256"],
            authority=OWNER_ACT,
        )
    assert exc.value.code == "revision_conflict"
    install(
        env.svc, "synthetic-evidence@1.1.0", "claude-code", project_dir, env.project_id,
        resolve="keep-local",
    )  # fmt: skip
    assert "My local edit." in (skill / "SKILL.md").read_text("utf-8")
    assert not (skill / "references" / "guide.md").exists()  # unmodified, dropped by v2
    assert (skill / "examples" / "new.md").is_file()
    lock = json.loads((project_dir / packages.LOCK_NAME).read_text("utf-8"))
    kept = lock["installs"][0]["files"][".claude/skills/synthetic-evidence/SKILL.md"]
    assert kept["local_edit_kept"] is True and lock["installs"][0]["version"] == "1.1.0"


def test_update_take_package_overwrites_only_with_explicit_choice(
    env: Env, tmp_path: Path, project_dir: Path
) -> None:
    import_package(env.svc, write_package(tmp_path / "src"))
    install(env.svc, "synthetic-evidence", "claude-code", project_dir)
    skill = project_dir / ".claude" / "skills" / "synthetic-evidence"
    (skill / "SKILL.md").write_text("edited", "utf-8")
    import_package(env.svc, _v2(tmp_path))
    install(env.svc, "synthetic-evidence@1.1.0", "claude-code", project_dir,
            resolve="take-package")  # fmt: skip
    assert "Version two." in (skill / "SKILL.md").read_text("utf-8")


def test_remove_touches_only_lock_owned_unmodified_files(
    env: Env, tmp_path: Path, project_dir: Path
) -> None:
    import_package(env.svc, write_package(tmp_path / "src"))
    install(env.svc, "synthetic-evidence", "claude-code", project_dir, env.project_id)
    skill = project_dir / ".claude" / "skills" / "synthetic-evidence"
    (skill / "references" / "guide.md").write_text("my edit", "utf-8")
    (skill / "notes-by-me.md").write_text("unrelated", "utf-8")
    plan = env.svc.skill_remove(
        "synthetic-evidence", "claude-code", project_dir, None, authority=OWNER_ACT
    )["plan"]
    actions = {op["path"].rsplit("/", 1)[-1]: op["action"] for op in plan["operations"]}
    assert actions == {"SKILL.md": "delete", "guide.md": "keep_modified"}
    result = env.svc.skill_remove(
        "synthetic-evidence",
        "claude-code",
        project_dir,
        plan["plan_sha256"],
        env.project_id,
        authority=OWNER_ACT,
    )
    assert not (skill / "SKILL.md").exists()
    assert (skill / "references" / "guide.md").read_text("utf-8") == "my edit"
    assert (skill / "notes-by-me.md").read_text("utf-8") == "unrelated"
    assert json.loads((project_dir / packages.LOCK_NAME).read_text("utf-8"))["installs"] == []
    record = result["record"]["results"][0]["id"]
    assert env.svc.get(env.project_id, OWNER, record, None)["body"]["status"] == "removed"


def test_failed_install_rolls_back_files_and_lock(
    env: Env, tmp_path: Path, project_dir: Path
) -> None:
    import_package(env.svc, write_package(tmp_path / "src"))
    catalog = env.svc.catalog
    plan = packages.install_plan(catalog, "synthetic-evidence", "claude-code", project_dir)

    def crash(index: int) -> None:
        if index == 1:
            raise OSError("simulated disk failure")

    with pytest.raises(OSError):
        packages.install_commit(
            catalog, "synthetic-evidence", "claude-code", project_dir, plan["plan_sha256"],
            fail_after=crash,
        )  # fmt: skip
    assert not (project_dir / ".claude").exists()
    assert not (project_dir / packages.LOCK_NAME).exists()


def test_export_bundle_reimports_with_same_digest(env: Env, tmp_path: Path) -> None:
    import_package(env.svc, write_package(tmp_path / "src"))
    out = tmp_path / "share" / "synthetic.zip"
    result = env.svc.skill_export("synthetic-evidence@1.0.0", out)
    assert result["distributable"] is True
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        lock = json.loads(zf.read("prometheos-package.lock.json"))
        notice = zf.read("NOTICE.txt").decode()
    assert {"NOTICE.txt", "MODIFICATIONS.json", "prometheos-package.json"} <= names
    assert "path" not in lock["origin"]  # no local paths leak into a shared bundle
    assert "MIT from https://example.invalid/synthetic" in notice
    report = env.svc.skill_inspect(out)
    original = env.svc.skill_catalog()["packages"][0]["package_digest"]
    assert report["package_digest"] == original and report["blocking"] == []
    with pytest.raises(PrometheosError):
        env.svc.skill_export("synthetic-evidence@1.0.0", out)  # never overwrites


def test_adaptation_records_parent_and_modifications(env: Env, tmp_path: Path) -> None:
    import_package(env.svc, write_package(tmp_path / "src"))
    adapted = Path(
        env.svc.skill_adapt("synthetic-evidence", "my-evidence", tmp_path / "a")["adapted"]
    )
    sidecar_path = adapted / "prometheos-package.json"
    sidecar = json.loads(sidecar_path.read_text("utf-8"))
    assert sidecar["parent"]["name"] == "synthetic-evidence"
    guide = adapted / "skills" / "synthetic-evidence" / "references" / "guide.md"
    guide.write_text("Adapted guide.\n", "utf-8")
    # Not refreshed: the sidecar digest no longer matches, so rights become unknown.
    assert env.svc.skill_inspect(adapted)["unknown_rights"]
    env.svc.skill_refresh(adapted)
    sidecar = json.loads(sidecar_path.read_text("utf-8"))
    for component in sidecar["components"]:
        if component["path"].endswith("guide.md"):
            component["modified"] = False  # hide the modification
    sidecar_path.write_text(json.dumps(sidecar), "utf-8")
    report = env.svc.skill_inspect(adapted)
    assert any("not marked modified" in p for p in report["problems"])
    env.svc.skill_refresh(adapted)
    result = import_package(env.svc, adapted)
    assert result["package"]["parent"]["name"] == "synthetic-evidence"
    assert result["package"]["trust"]["automated_checks_passed"] is True


def test_verify_detects_catalog_tampering(env: Env, tmp_path: Path) -> None:
    imported = import_package(env.svc, write_package(tmp_path / "src"))["package"]
    assert env.svc.skill_verify("synthetic-evidence")["digests_ok"] is True
    stored = env.svc.catalog.files_dir(imported["package_digest"])
    (stored / "skills" / "synthetic-evidence" / "SKILL.md").write_text("tampered", "utf-8")
    report = env.svc.skill_verify("synthetic-evidence")
    assert report["digests_ok"] is False and report["problems"]


def test_catalog_save_waits_out_a_brief_hold_on_the_index(tmp_path: Path) -> None:
    """A scanner or indexer holding catalog.json open made the atomic replace fail with
    WinError 5 (seen under ``pytest -n auto``). A brief hold is waited out; a lasting one
    still fails, and the previous index stays whole. (Elsewhere the replace succeeds while the
    file is open, so this only bites on Windows.)"""
    import threading

    catalog = packages.Catalog(tmp_path / "home")
    catalog.save({"schema_version": 1, "packages": {}, "denylist": []})
    held = catalog.index_path.open("rb")  # Python opens without FILE_SHARE_DELETE on Windows
    threading.Timer(0.3, held.close).start()
    catalog.save({"schema_version": 1, "packages": {"a@1": {}}, "denylist": []})
    assert catalog.load()["packages"] == {"a@1": {}}


def test_catalog_save_gives_up_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = packages.Catalog(tmp_path / "home")
    catalog.save({"schema_version": 1, "packages": {}, "denylist": []})
    attempts: list[object] = []
    waited: list[float] = []

    def refuse(src: object, _dst: object) -> None:
        attempts.append(src)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(packages.os, "replace", refuse)
    monkeypatch.setattr(packages.time, "sleep", waited.append)
    with pytest.raises(PermissionError):
        catalog.save({"schema_version": 1, "packages": {"a@1": {}}, "denylist": []})
    assert len(attempts) == 20 and sum(waited) < 10
    monkeypatch.undo()
    assert catalog.load()["packages"] == {}


def test_revoke_refuses_use_and_trust_needs_the_owner(
    env: Env, tmp_path: Path, project_dir: Path
) -> None:
    source = write_package(tmp_path / "src")
    import_package(env.svc, source)
    revoked = env.svc.skill_revoke("synthetic-evidence@1.0.0")
    assert revoked["status"] == "revoked" and "host refreshes" in revoked["warning"]
    with pytest.raises(PrometheosError) as exc:
        env.svc.skill_install(
            "synthetic-evidence", "claude-code", project_dir, None, authority=OWNER_ACT
        )
    assert exc.value.code == "policy_denied"
    assert env.svc.skill_import_plan(source)["blocking"]  # denylisted digest
    with pytest.raises(PrometheosError):
        env.svc.skill_trust("synthetic-evidence@1.0.0", authority=AGENT)
    env.svc.skill_trust("synthetic-evidence@1.0.0", authority=OWNER_ACT)
    assert install(env.svc, "synthetic-evidence", "claude-code", project_dir)["installed"]


def test_archive_import_records_archive_digest(env: Env, tmp_path: Path) -> None:
    archive = zip_folder(write_package(tmp_path / "src"), tmp_path / "pkg.zip")
    imported = import_package(env.svc, archive)["package"]
    assert imported["source"]["archive_sha256"] == _sha(archive)
