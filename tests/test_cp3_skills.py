"""C03-SKILL: three original Agent Skills validate; first-host setup is project-local and
bounded."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from prometheos.domain.models import PrometheosError
from prometheos.skills import (
    BUNDLED,
    bundled_skill_dir,
    bundled_skills,
    host_apply,
    host_plan,
    validate_skill,
)

PID = "project_00000000-0000-4000-8000-000000000001"


CP3_ORIGINALS = ["capture-research-evidence", "reconcile-meeting", "inspect-claim-evidence"]


def test_three_original_skills_validate() -> None:
    skills = bundled_skills()
    # The three CP3 originals stay first; later checkpoints add their own skills (CP18.1:
    # work-items, the session-start summary), and every bundled skill must validate too.
    # Pinned literally: adding a bundled skill needs a conscious edit here (review s1-a F5).
    # CP17.5: 0043; decision 0112 adds frame-paper and rerun-paper.
    assert list(BUNDLED) == [
        *CP3_ORIGINALS,
        "work-items",
        "adopt-folder",
        "frame-paper",
        "rerun-paper",
        "research-directions",  # decision 0113
    ]
    assert [s["name"] for s in skills] == list(BUNDLED)
    for skill in skills:
        assert skill["errors"] == []
        assert 1 <= len(skill["description"]) <= 1024
        text = (bundled_skill_dir() / skill["name"] / "SKILL.md").read_text(encoding="utf-8")
        assert len(text.splitlines()) < 500  # progressive disclosure guidance
        assert "researcher" in text  # every skill leaves decisions to the researcher


def make(tmp: Path, name: str, frontmatter: str, body: str = "Do the thing.\n") -> Path:
    d = tmp / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return d


@pytest.mark.parametrize(
    ("dirname", "frontmatter", "fragment"),
    [
        ("Bad-Name", "name: Bad-Name\ndescription: x\n", "invalid name"),
        ("a--b", "name: a--b\ndescription: x\n", "invalid name"),
        ("-lead", "name: -lead\ndescription: x\n", "invalid name"),
        ("other", "name: mismatch\ndescription: x\n", "must equal the directory"),
        ("long-desc", "name: long-desc\ndescription: " + "x" * 1025 + "\n", "1-1024"),
        ("no-desc", "name: no-desc\n", "1-1024"),
        ("extra", "name: extra\ndescription: x\nversion: 2\n", "unknown frontmatter field"),
        ("dupe", "name: dupe\nname: dupe\ndescription: x\n", "duplicate"),
        ("compat", "name: compat\ndescription: x\ncompatibility: " + "y" * 501 + "\n", "1-500"),
    ],
)
def test_validator_rejects_spec_violations(
    tmp_path: Path, dirname: str, frontmatter: str, fragment: str
) -> None:
    errors = validate_skill(make(tmp_path, dirname, frontmatter))
    assert any(fragment in e for e in errors), errors


def test_validator_checks_body_and_references(tmp_path: Path) -> None:
    empty = make(tmp_path, "empty-body", "name: empty-body\ndescription: x\n", body="\n")
    assert any("body is empty" in e for e in validate_skill(empty))
    broken = make(
        tmp_path,
        "broken-ref",
        "name: broken-ref\ndescription: x\n",
        body="See [ref](references/missing.md).\n",
    )
    assert any("missing" in e for e in validate_skill(broken))
    no_front = tmp_path / "plain"
    no_front.mkdir()
    (no_front / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
    assert validate_skill(no_front)


def test_host_setup_is_project_local_planned_and_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "coding-project"
    target.mkdir()
    before = sorted(p.name for p in tmp_path.iterdir())
    plan = host_plan("claude-code", target, PID)
    assert all(Path(f["path"]).resolve().is_relative_to(target.resolve()) for f in plan["files"])
    assert not (target / ".mcp.json").exists()  # a plan writes nothing
    written = host_apply(plan)
    assert len(written) == 1 + len(BUNDLED)  # .mcp.json and one SKILL.md per bundled skill
    config = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
    assert config["mcpServers"]["prometheos"]["args"] == [
        "mcp",
        "serve",
        "--stdio",
        "--project",
        PID,
    ]
    for name in BUNDLED:
        assert (target / ".claude" / "skills" / name / "SKILL.md").is_file()
    assert host_apply(host_plan("claude-code", target, PID)) == []  # nothing to change
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # nothing outside the target


def test_host_setup_merges_but_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    (target / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
    )
    host_apply(host_plan("claude-code", target, PID))
    servers = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"other", "prometheos"}
    with pytest.raises(PrometheosError) as err:
        host_plan("claude-code", target, "project_00000000-0000-4000-8000-000000000002")
    assert err.value.code == "revision_conflict"
    skill = target / ".claude" / "skills" / BUNDLED[0] / "SKILL.md"
    skill.write_text("locally edited\n", encoding="utf-8")
    shutil.copyfile(target / ".mcp.json", tmp_path / "keep.json")
    with pytest.raises(PrometheosError):
        host_plan("claude-code", target, PID)
    assert skill.read_text(encoding="utf-8") == "locally edited\n"


def test_host_setup_refuses_global_locations(tmp_path: Path) -> None:
    with pytest.raises(PrometheosError) as err:
        host_plan("claude-code", Path.home(), PID)
    assert err.value.code == "policy_denied"
    claude_dir = Path.home() / ".claude"
    if claude_dir.is_dir():
        with pytest.raises(PrometheosError):
            host_plan("claude-code", claude_dir, PID)
    with pytest.raises(PrometheosError) as err:
        host_plan("cursor", tmp_path, PID)
    assert err.value.code == "unsupported"
