"""C07-EXPORT: the local manuscript/submission bundle carries a source/version manifest
bound to exact bytes; the handoff into a local folder never deletes, never overwrites an
unnamed file, never runs Git; no cloud write, compile install or publication exists.
Synthetic fixtures only."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env, request
from cp7_support import OVERBROAD_LINE, SCOPE_OVERBROAD, PaperProject, table_counts, text_evidence
from test_cp1_cli import Cli

from prometheos.domain.models import PrometheosError

SRC = Path(__file__).resolve().parents[1] / "src" / "prometheos"


@pytest.fixture
def pp(tmp_path: Path) -> PaperProject:
    return PaperProject(Env(tmp_path))


def used_in(pp: PaperProject, version_id: str, paper_id: str) -> None:
    raw = request(
        pp.pid,
        [
            {
                "op": "relation_add",
                "source_ref": {"id": version_id, "rev": 1},
                "target_ref": {"id": paper_id, "rev": 1},
                "relation": "used_in",
            }
        ],
        pp.env.seq(),
    )
    pp.svc.apply(pp.pid, raw, OWNER)


def bundle_project(pp: PaperProject) -> dict[str, Any]:
    env = pp.env
    claim = pp.claim(
        OVERBROAD_LINE,
        SCOPE_OVERBROAD,
        text_evidence(pp.manuscript, pp.overbroad_line, OVERBROAD_LINE),
    )
    paper = pp.paper(claim_refs=[{"id": claim, "rev": 1}])
    anchor = pp.anchor(
        paper,
        pp.overbroad_line,
        claim_refs=[{"id": claim, "rev": 1}],
        evidence=[text_evidence(pp.plant, 1)],
    )
    used_in(pp, pp.baseline["version_id"], paper)
    (env.research / "large-run.csv").write_text("t,energy\n0,0\n", "utf-8")
    reference = env.import_files(["large-run.csv"], retention="reference")["plan"]["items"][0]
    used_in(pp, reference["version_id"], paper)
    return {"paper": paper, "anchor": anchor, "claim": claim, "reference": reference}


def test_bundle_is_bound_to_exact_versions(pp: PaperProject, tmp_path: Path) -> None:
    made = bundle_project(pp)
    seq, counts = pp.env.seq(), table_counts(pp.env.store_dir)
    out = tmp_path / "bundle.zip"
    result = pp.svc.paper_export(pp.pid, AGENT, made["paper"], out)
    assert pp.env.seq() == seq and table_counts(pp.env.store_dir) == counts  # nothing recorded

    with zipfile.ZipFile(out) as archive:
        names = sorted(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        members = {n: archive.read(n) for n in names if n != "manifest.json"}
    assert names == [
        "artifacts/baseline.md",
        "artifacts/plant-result.md",
        "manifest.json",
        "manuscript/manuscript.md",
    ]
    by_path = {f["path"]: f for f in manifest["files"]}
    for path, data in members.items():
        entry = by_path[path]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(data) == entry["byte_length"]
        version = pp.get(entry["version_id"])["body"]
        assert version["sha256"] == entry["sha256"] and version["source_id"] == entry["source_id"]
    assert members["manuscript/manuscript.md"] == (pp.env.research / "manuscript.md").read_bytes()
    roles = {f["path"]: f["role"] for f in manifest["files"]}
    assert roles == {
        "manuscript/manuscript.md": "manuscript",
        "artifacts/baseline.md": "used_in",
        "artifacts/plant-result.md": "anchor_evidence",
    }
    assert manifest["paper_id"] == made["paper"] and manifest["project_seq"] == seq
    assert manifest["not_included"] == [
        {
            "version_id": made["reference"]["version_id"],
            "source_id": made["reference"]["source_id"],
            "reason": "reference_only",
        }
    ]
    assert (
        manifest["check"]["findings_digest"]
        == pp.svc.paper_check(pp.pid, AGENT, made["paper"])["findings_digest"]
    )
    assert any("nothing was submitted" in s for s in manifest["statements"])
    assert any("Nothing was compiled" in s for s in manifest["statements"])
    assert result["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()

    # Never overwrites; an older manuscript version can be chosen explicitly.
    with pytest.raises(PrometheosError) as err:
        pp.svc.paper_export(pp.pid, AGENT, made["paper"], out)
    assert err.value.code == "validation_error"
    (pp.env.research / "manuscript.md").write_bytes(b"Rewritten draft.\n")
    pp.env.import_files(["manuscript.md"])
    older = pp.svc.paper_export(
        pp.pid, AGENT, made["paper"], tmp_path / "older.zip", pp.manuscript["version_id"]
    )
    assert older["files"][0]["version_id"] == pp.manuscript["version_id"]
    latest = pp.svc.paper_export(pp.pid, AGENT, made["paper"], tmp_path / "latest.zip")
    assert latest["files"][0]["version_id"] != pp.manuscript["version_id"]
    with zipfile.ZipFile(tmp_path / "latest.zip") as archive:
        assert archive.read("manuscript/manuscript.md") == b"Rewritten draft.\n"


def tamper_payload(pp: PaperProject, digest: str, data: bytes) -> None:
    """Replace the retained bytes stored under ``digest`` through a raw connection. Ask the app
    for anything it needs BEFORE calling this: while a raw writable handle is open the store
    admits no one (decision 0025: an idle external writer makes the app answer ``busy``). The
    handle exists only for the tampering and is closed on every path."""
    with closing(sqlite3.connect(pp.env.store_dir / "record.sqlite")) as conn:
        changed = conn.execute("UPDATE payloads SET data = ? WHERE sha256 = ?", (data, digest))
        assert changed.rowcount == 1  # the retained bytes really were replaced
        conn.commit()


def test_bundle_refuses_corrupt_or_unretained_manuscript(pp: PaperProject, tmp_path: Path) -> None:
    paper = pp.paper()
    digest = pp.get(pp.manuscript["version_id"])["body"]["sha256"]  # read before any raw handle
    tamper_payload(pp, digest, b"tampered")
    with pytest.raises(PrometheosError) as err:
        pp.svc.paper_export(pp.pid, AGENT, paper, tmp_path / "x.zip")
    assert err.value.code == "source_unavailable"
    assert not (tmp_path / "x.zip").exists()


def test_a_failed_injection_leaves_no_writable_handle_on_the_store(pp: PaperProject) -> None:
    version = pp.manuscript["version_id"]
    with pytest.raises(AssertionError) as failed:  # no payload has this digest: nothing changes
        tamper_payload(pp, "0" * 64, b"tampered")
    assert failed.traceback  # the failing frame stays alive here, so only an explicit close helps
    assert pp.get(version)["body"]["sha256"]  # the store still admits: no handle was leaked


def test_agent_bundle_withholds_restricted_artifacts(pp: PaperProject, tmp_path: Path) -> None:
    paper = pp.paper()
    (pp.env.research / "collaborator.md").write_text("Unpublished collaborator data.\n", "utf-8")
    secret = pp.env.import_files(["collaborator.md"], sensitivity="restricted")["plan"]["items"][0]
    used_in(pp, secret["version_id"], paper)
    agent = pp.svc.paper_export(pp.pid, AGENT, paper, tmp_path / "agent.zip")
    with zipfile.ZipFile(tmp_path / "agent.zip") as archive:
        manifest_text = archive.read("manifest.json").decode()
        assert all("collaborator" not in n for n in archive.namelist())
    assert secret["version_id"] not in manifest_text and secret["source_id"] not in manifest_text
    assert agent["withheld_by_sensitivity"] == 1
    owner = pp.svc.paper_export(pp.pid, OWNER, paper, tmp_path / "owner.zip")
    assert any(f["version_id"] == secret["version_id"] for f in owner["files"])


def test_handoff_plan_and_apply_preserve_the_folder(pp: PaperProject, tmp_path: Path) -> None:
    made = bundle_project(pp)
    into = tmp_path / "overleaf-clone"
    into.mkdir()
    (into / "notes.txt").write_text("my own notes", "utf-8")
    (into / "manuscript.md").write_text("Edited in the cloud editor.\n", "utf-8")
    (into / ".git").mkdir()
    (into / ".git" / "HEAD").write_text("ref: refs/heads/main\n", "utf-8")
    seq = pp.env.seq()

    plan = pp.svc.paper_handoff_plan(pp.pid, AGENT, made["paper"], into)
    actions = {e["path"]: e["action"] for e in plan["entries"]}
    manifest_name = next(p for p in actions if p.startswith("prometheos-handoff-"))
    assert actions == {
        "manuscript.md": "conflict",
        "prometheos-artifacts/baseline.md": "new",
        "prometheos-artifacts/plant-result.md": "new",
        manifest_name: "new",
    }
    assert plan["target_only_preserved"] == ["notes.txt"]  # .git is never listed or touched
    assert any("No Git command" in limit for limit in plan["limits"])
    assert pp.env.seq() == seq

    with pytest.raises(PrometheosError) as err:  # a conflict needs an explicit name
        pp.svc.paper_handoff_apply(pp.pid, AGENT, made["paper"], into, plan["digest"], [])
    assert err.value.code == "policy_denied"
    assert err.value.details["conflicts"] == ["manuscript.md"]
    with pytest.raises(PrometheosError) as err:
        pp.svc.paper_handoff_apply(pp.pid, AGENT, made["paper"], into, "0" * 64, ["manuscript.md"])
    assert err.value.code == "revision_conflict"
    (into / "late.txt").write_text("added after the review", "utf-8")
    with pytest.raises(PrometheosError) as err:  # the folder changed after review
        pp.svc.paper_handoff_apply(
            pp.pid, AGENT, made["paper"], into, plan["digest"], ["manuscript.md"]
        )
    assert err.value.code == "revision_conflict"
    (into / "late.txt").unlink()
    assert (into / "manuscript.md").read_text("utf-8") == "Edited in the cloud editor.\n"

    applied = pp.svc.paper_handoff_apply(
        pp.pid, AGENT, made["paper"], into, plan["digest"], ["manuscript.md"]
    )
    assert sorted(w["path"] for w in applied["written"]) == sorted(actions)
    assert (into / "manuscript.md").read_bytes() == (pp.env.research / "manuscript.md").read_bytes()
    assert (into / "notes.txt").read_text("utf-8") == "my own notes"
    assert (into / ".git" / "HEAD").read_text("utf-8") == "ref: refs/heads/main\n"
    assert sorted(p.name for p in (into / ".git").iterdir()) == ["HEAD"]
    again = pp.svc.paper_handoff_plan(pp.pid, AGENT, made["paper"], into)
    assert {e["action"] for e in again["entries"]} == {"identical"}
    assert pp.env.seq() == seq  # nothing recorded in the research store


def test_cli_export_emits_one_document(pp: PaperProject, tmp_path: Path) -> None:
    made = bundle_project(pp)
    cli = Cli(pp.env.tmp)
    target = str(tmp_path / "b.zip")
    code, out, _ = cli("paper", "export", made["paper"], "--out", target, "--project", pp.pid)
    assert code == 0 and out["files"][0]["role"] == "manuscript"
    code, out, _ = cli("paper", "export", made["paper"], "--out", target, "--project", pp.pid)
    assert code == 2 and out["error"]["code"] == "validation_error"
    code, out, _ = cli("paper", "check", made["paper"], "--project", pp.pid)
    assert code == 0 and out["paper_id"] == made["paper"]


NETWORK_OR_PROCESS = {
    "socket",
    "http",
    "urllib",
    "requests",
    "httpx",
    "subprocess",
    "ftplib",
    "smtplib",
    "webbrowser",
}


@pytest.mark.parametrize("module", ["app/papers.py", "app/paper_rules.py", "app/jobs.py"])
def test_paper_and_job_code_cannot_reach_a_network_or_run_tools(module: str) -> None:
    """No cloud write, compile run or publication path exists in the CP7 modules."""
    tree = ast.parse((SRC / module).read_text("utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute) and node.attr in ("system", "popen", "startfile"):
            pytest.fail(f"{module} calls {node.attr}")
    assert not imported & NETWORK_OR_PROCESS
