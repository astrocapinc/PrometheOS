"""Start the real hub on a disposable synthetic project for UI development and browser QA.

It builds the E01 synthetic KERS project (two scoped claims over a retained baseline),
captures the synthetic supervision meeting notes and adds a few notebook entries, then
serves the hub on 127.0.0.1 (free port) and writes ``{"url", "pairing_token",
"project_id", ...}`` to ``--info``. Each new pairing token is appended there when the
previous one is used (``--tokens N`` pre-mints a queue of one-use tokens for tests).

Synthetic fixtures only; never point this at real research.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn

from prometheos.hub.security import SESSION_TTL, Gate, _digest, _Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# Synthetic audio for the browser tests: a tone recorder (real time) and the labelled synthetic
# transcription engine. These switches exist only here; a real hub never sets them.
os.environ.setdefault("PROMETHEOS_ENABLE_SYNTHETIC_ENGINE", "1")
os.environ.setdefault("PROMETHEOS_CAPTURE_BACKEND", "synthetic")
os.environ.setdefault("PROMETHEOS_SYNTHETIC_SPEED", "1")


def build(work: Path) -> dict[str, object]:
    from e01_setup import build_project

    from prometheos.app.service import Service
    from prometheos.domain.ids import new_id, new_request_id

    project = build_project(work)
    svc = Service(home=Path(str(project["home"])), actor="hub-fixture")
    pid = str(project["project_id"])
    notes = (ROOT / "fixtures" / "meeting.txt").read_text(encoding="utf-8")
    meeting = svc.capture_meeting(
        pid,
        title="Supervision meeting",
        text=notes,
        held_at="2026-09-28T10:00:00Z",
        request_id=new_request_id(),
    )
    seq = svc.project_info(pid, _owner())["project_seq"]
    entries = [
        ("Baseline imported", "Retained the baseline comparison for the reference controller."),
        ("Open question", "Does the constant-deceleration result hold on the plant model?"),
    ]
    svc.apply(
        pid,
        json.dumps(
            {
                "schema_version": 1,
                "project_id": pid,
                "request_id": new_request_id(),
                "expected_seq": seq,
                "operations": [
                    {
                        "op": "create",
                        "kind": "entry",
                        "id": new_id("entry"),
                        "base_rev": 0,
                        "sensitivity": "normal",
                        "occurred_at": None,
                        "body": {
                            "title": title,
                            "text": text,
                            "category": "note",
                            "evidence": [],
                            "claim_refs": [],
                            "author_ref": None,
                            "origin": "typed",
                            "quantities": [],
                            "review": "reviewed",
                        },
                    }
                    for title, text in entries
                ],
            }
        ).encode(),
    )
    _seed_objective_entry(svc, pid)

    # Agent work the researcher reviews in the hub: proposed meeting items (as a coding
    # agent would submit through a task) and a pending correction of the overbroad claim.
    def ref(line: int, source: str, version: str) -> dict[str, object]:
        return {
            "source_id": source,
            "version_id": version,
            "locator": {"type": "text", "start_line": line, "end_line": line},
            "quote_sha256": None,
        }

    m_src, m_ver = meeting["source_id"], meeting["version_id"]
    task = svc.task_prepare(
        pid,
        purpose="meeting_extract",
        meeting_id=meeting["meeting_id"],
        request_id=new_request_id(),
    )
    base = {"proposed_owner": None, "due_at": None}
    items = [
        ("suggestion", 2, "Compare the controllers with actuator limits before the main claim."),
        ("commitment", 3, "Attach the constrained-plant result before the next meeting."),
        ("question", 4, "Has anyone checked the venue's figure requirements?"),
    ]
    result = {
        "purpose": "meeting_extract",
        "items": [
            {**base, "type": t, "text": text, "evidence": [ref(line, m_src, m_ver)]}
            for t, line, text in items
        ],
    }
    svc.task_submit(pid, task["task_id"], json.dumps(result).encode(), new_request_id())
    plan = svc.import_plan(
        pid,
        root_alias="research",
        paths=["plant-result.md"],
        retention="copy",
        sensitivity="normal",
    )
    svc.import_commit(pid, plan["plan_id"])
    plant = plan["items"][0]
    lines = (ROOT / "fixtures" / "plant-result.md").read_text(encoding="utf-8").splitlines()
    plant_line = next(i for i, text in enumerate(lines, 1) if "PLANT-B" in text)
    overbroad, narrow = project["overbroad_claim"], project["narrow_claim"]
    correction = new_id("correction")
    body = {
        "target_refs": [{"id": overbroad, "rev": 1}],
        "replacement_refs": [{"id": narrow, "rev": 1}],
        "reason": "Overbroad: the plant result (PLANT-B) contradicts it outside the "
        "simplified event.",
        "evidence": [ref(plant_line, plant["source_id"], plant["version_id"])],
        "effects": [
            {
                "target_ref": {"id": overbroad, "rev": 1},
                "effect": "supersede",
                "replacement_ref": {"id": narrow, "rev": 1},
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    op = {
        "op": "create",
        "kind": "correction",
        "id": correction,
        "base_rev": 0,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": body,
    }
    seq = svc.project_info(pid, _owner())["project_seq"]
    request = {
        "schema_version": 1,
        "project_id": pid,
        "request_id": new_request_id(),
        "expected_seq": seq,
        "operations": [op],
    }
    svc.apply(pid, json.dumps(request).encode())
    seed = _seed_skill_and_audit(svc, pid, plant)
    seed |= _seed_paper(svc, pid, project, meeting)
    seed |= _seed_latex(svc, pid, project, work)
    _seed_adoption_sample(project)
    _seed_library_folder(project)
    return {
        **project,
        "meeting_id": meeting["meeting_id"],
        "meeting_version": meeting["version_id"],
        "correction_id": correction,
        "seed": seed,
    }


def _seed_objective_entry(svc: Any, pid: str) -> None:
    """CP20.4 (gap audit #16): an entry standing for the project objective, named in the
    settings' ``objective_entry_ref``, so an adoption outline's questions for the objective are
    linked to it and the research map lists them (e2e 29). The settings change is protected:
    the fixture grants it the way the hub's owner decision does."""
    from prometheos.domain.ids import new_id, new_request_id

    owner = _owner()
    entry = new_id("entry")
    body = {
        "title": "Objective: compare the candidate controller with the reference",
        "text": "The project's objective, as an entry questions can address.",
        "category": "note",
        "evidence": [],
        "claim_refs": [],
        "author_ref": None,
        "origin": "typed",
        "quantities": [],
        "review": "reviewed",
    }
    create = {"op": "create", "kind": "entry", "id": entry, "base_rev": 0,
              "sensitivity": "normal", "occurred_at": None, "body": body}  # fmt: skip
    seq = svc.project_info(pid, owner)["project_seq"]
    svc.apply(pid, json.dumps({"schema_version": 1, "project_id": pid,
                               "request_id": new_request_id(), "expected_seq": seq,
                               "operations": [create]}).encode(), owner)  # fmt: skip
    current = svc.get(pid, owner, pid, None)
    settings = dict(current["body"]) | {"objective_entry_ref": {"id": entry, "rev": 1}}
    raw = json.dumps(
        {
            "schema_version": 1,
            "project_id": pid,
            "request_id": new_request_id(),
            "expected_seq": svc.project_info(pid, owner)["project_seq"],
            "operations": [{"op": "project_update", "base_rev": current["rev"],
                            "settings": settings}],
        }
    ).encode()  # fmt: skip
    digest = svc.review_request(pid, raw, owner)["digest"]
    grant = svc.grant(pid, operation="exact_request", digest=digest, ttl_seconds=60, max_uses=1,
                      label="hub fixture objective entry")  # fmt: skip
    svc.apply(pid, raw, owner, grant.secret)


def _seed_adoption_sample(project: dict[str, Any]) -> None:
    """CP17.5 (decision 0043): two synthetic versions of one result in the research folder, so
    the Adoption page has a family the owner can decide (the scan itself reads only)."""
    results = Path(str(project["research_root"])) / "adoption-sample"
    results.mkdir(exist_ok=True)
    (results / "sweep_v1.csv").write_text("speed,loss\n10,0.31\n20,0.42\n", encoding="utf-8")
    (results / "sweep_v2.csv").write_text("speed,loss\n10,0.29\n20,0.40\n", encoding="utf-8")


def _seed_library_folder(project: dict[str, Any]) -> None:
    """CP11 S11.1 (decision 0060): synthetic papers and hostile files in research/library for the
    Library page's import journey (files only; the journey plans and commits them)."""
    from synthetic_pdf import hostile_set, synthetic_set, write_folder

    folder = Path(str(project["research_root"])) / "library"
    write_folder(folder, {**synthetic_set(), **hostile_set()})
    # CP11 S11.2 (decision 0062): synthetic bibliographies and their hostile set, in their own
    # folder so the PDF journey's counts stay as they were.
    import synthetic_bib

    synthetic_bib.write_folder(
        Path(str(project["research_root"])) / "bibliography",
        {**synthetic_bib.synthetic_set(), **synthetic_bib.hostile_set()},
    )


SYNTHETIC_VENUE = """# SYNTHETIC venue guide (hub fixture, not a real venue)

- Manuscripts must not exceed eight pages including references.
- State the model fidelity of every simulated result.
- Provide a data availability statement.
"""
MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _seed_paper(
    svc: Any, pid: str, project: dict[str, Any], meeting: dict[str, Any]
) -> dict[str, Any]:
    """CP7: a paper on the synthetic manuscript with an anchor on the overbroad sentence
    (which the pending correction will flag), unknown venue requirement rows from a dated
    synthetic venue guide, an open reviewer comment, a registered compiled PDF and two
    recurring checks whose first findings sit in the inbox."""
    from datetime import UTC, datetime, timedelta

    from prometheos.domain.ids import new_id, new_request_id

    owner = _owner()
    research = Path(str(project["research_root"]))
    (research / "venue-guide.md").write_text(SYNTHETIC_VENUE, encoding="utf-8")
    (research / "compiled-draft.pdf").write_bytes(MINIMAL_PDF)
    start = datetime.now(UTC).replace(second=0, microsecond=0)
    budget = {
        "max_inputs": 20,
        "max_output_chars": 2000,
        "max_provider_calls": 0,
        "max_concurrent_runs": 1,
        "spend_cap_estimate_usd": None,
    }
    schedule = {
        "type": "interval",
        "minutes": 60,
        "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    digest = {"handler": "digest", "parameters": {}, "schedule": schedule, "enabled": True,
              "budget": budget, "last_slot": None}  # fmt: skip
    svc.job_add(pid, owner, digest, new_request_id())
    plan = svc.import_plan(
        pid,
        root_alias="research",
        paths=["manuscript.md", "venue-guide.md", "compiled-draft.pdf"],
        retention="copy",
        sensitivity="normal",
    )
    svc.import_commit(pid, plan["plan_id"])
    manuscript, venue, pdf = plan["items"]
    lines = (research / "manuscript.md").read_text(encoding="utf-8").splitlines()
    overbroad_line = next(i for i, t in enumerate(lines, 1) if "across operating conditions" in t)
    rows = svc.requirements_draft(pid, owner, venue["version_id"])["requirements"]
    paper, anchor = new_id("paper"), new_id("anchor")
    comment = {
        "source_id": meeting["source_id"],
        "version_id": meeting["version_id"],
        "locator": {"type": "text", "start_line": 2, "end_line": 2},
        "quote_sha256": None,
    }
    paper_body = {
        "title": "Synthetic KERS manuscript",
        "objective": "Report the simplified comparison within its stated regime.",
        "claim_refs": [{"id": project["overbroad_claim"], "rev": 1}],
        "manuscript_source_id": manuscript["source_id"],
        "venue_source_refs": [{"id": venue["version_id"], "rev": 1}],
        "status": "drafting",
        "deadline": "2026-12-01T17:00:00Z",
        "requirements": rows,
        "contribution": "A scoped comparison on synthetic events.",
        "related_papers": [],
        "review_rounds": [
            {
                "id": "supervision",
                "label": "Supervisor comments (synthetic)",
                "source_refs": [{"id": meeting["version_id"], "rev": 1}],
                "received_at": "2026-09-28T10:00:00Z",
                "items": [
                    {
                        "id": "c1",
                        "comment_source_ref": comment,
                        "text": "Compare with actuator limits before the main claim.",
                        "response_text": "",
                        "change_refs": [],
                        "status": "open",
                        "resolution_evidence": [],
                    }
                ],
            }
        ],
    }
    anchor_body = {
        "paper_id": paper,
        "manuscript_version_id": manuscript["version_id"],
        "locator": {"type": "text", "start_line": overbroad_line, "end_line": overbroad_line},
        "claim_refs": [{"id": project["overbroad_claim"], "rev": 1}],
        "evidence": [],
        "status": "current",
        "reason": None,
    }

    def create(kind: str, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"op": "create", "kind": kind, "id": rid, "base_rev": 0, "sensitivity": "normal",
                "occurred_at": None, "body": body}  # fmt: skip

    request = {
        "schema_version": 1,
        "project_id": pid,
        "request_id": new_request_id(),
        "expected_seq": svc.project_info(pid, owner)["project_seq"],
        "operations": [
            create("paper", paper, paper_body),
            create("anchor", anchor, anchor_body),
            {
                "op": "relation_add",
                "source_ref": {"id": pdf["version_id"], "rev": 1},
                "target_ref": {"id": paper, "rev": 1},
                "relation": "used_in",
            },
        ],
    }
    svc.apply(pid, json.dumps(request).encode("utf-8"), owner)
    check = dict(digest, handler="paper_check", parameters={"paper_id": paper})
    svc.job_add(pid, owner, check, new_request_id())
    svc.jobs_tick(pid, now=start + timedelta(minutes=61))
    return {
        "paper_id": paper,
        "anchor_id": anchor,
        "pdf_version_id": pdf["version_id"],
        "venue_version_id": venue["version_id"],
        "manuscript_version_id": manuscript["version_id"],
    }


def _seed_skill_and_audit(svc: Any, pid: str, plant: dict[str, Any]) -> dict[str, Any]:
    """An imported (synthetic) skill package and a notebook entry citing exact lines of the
    retained plant result, for the "use a skill" and "audit" browser paths."""
    sys.path.insert(0, str(ROOT / "tests"))
    from cp4_support import import_package, write_package

    from prometheos.domain.ids import new_id, new_request_id

    with tempfile.TemporaryDirectory(prefix="hub-fixture-skill-") as tmp:
        import_package(svc, write_package(Path(tmp) / "pkg"))
    owner = _owner()
    text = svc.version_text(pid, owner, plant["version_id"])["text"]
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines, 1) if "with the candidate" in line)
    entry_id = new_id("entry")
    evidence = {
        "source_id": plant["source_id"],
        "version_id": plant["version_id"],
        "locator": {"type": "text", "start_line": start, "end_line": start + 1},
        "quote_sha256": None,
    }
    body = {
        "title": "Plant result recorded",
        "text": "On the constrained plant the candidate recovered less energy than the "
        "reference (1.80 kJ versus 1.90 kJ).",
        "category": "finding",
        "evidence": [evidence],
        "claim_refs": [],
        "author_ref": None,
        "origin": "typed",
        "quantities": [],
        "review": "reviewed",
    }
    request = {
        "schema_version": 1,
        "project_id": pid,
        "request_id": new_request_id(),
        "expected_seq": svc.project_info(pid, owner)["project_seq"],
        "operations": [
            {
                "op": "create",
                "kind": "entry",
                "id": entry_id,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": body,
            }
        ],
    }
    svc.apply(pid, json.dumps(request).encode("utf-8"), owner)
    return {
        "skill_ref": "synthetic-evidence",
        "audit_entry_id": entry_id,
        "audit_version_id": plant["version_id"],
        "audit_lines": [start, start + 1],
    }


LATEX_FIXTURE = ROOT / "tests" / "fixtures" / "latex" / "basic"


def _git(cwd: Path, *args: str) -> None:
    import msys_retry

    # Fixture git only: retried when the MSYS runtime aborts at start-up (decision 0037).
    msys_retry.run(
        ["git", "-c", "user.name=Collaborator", "-c", "user.email=collab@example.invalid", *args],
        cwd=cwd,
    )


def _seed_latex(svc: Any, pid: str, project: dict[str, Any], work: Path) -> dict[str, Any]:
    """CP9: a LaTeX paper linked to a local bare repository standing in for Overleaf (plus a
    collaborator clone the browser tests push from), with an anchor binding a claim to a
    line of main.tex. The real pinned engine is used when it is installed; the fixture
    points PROMETHEOS_TEX_ENGINE_DIR at the default home's engine (test tool only)."""
    import os
    import shutil

    from prometheos.app import texengine
    from prometheos.domain.ids import new_id, new_request_id

    base = os.environ.get("LOCALAPPDATA")
    if base and not os.environ.get(texengine.ENGINE_DIR_ENV):
        default = Path(base) / "PrometheOS-v1" / "tools" / f"tectonic-{texengine.TECTONIC.version}"
        if (default / "install.json").is_file():
            os.environ[texengine.ENGINE_DIR_ENV] = str(default)
    owner = _owner()
    bare = work / "overleaf.git"
    seed_dir = work / "overleaf-seed"
    collaborator = work / "collaborator"
    shutil.copytree(LATEX_FIXTURE, seed_dir)
    _git(work, "init", "-q", "--bare", "-b", "master", str(bare))
    _git(seed_dir, "init", "-q", "-b", "master")
    _git(seed_dir, "add", "-A")
    _git(seed_dir, "commit", "-qm", "Initial Overleaf project")
    _git(seed_dir, "push", "-q", str(bare), "master")
    _git(work, "clone", "-q", str(bare), str(collaborator))
    paper = new_id("paper")
    seq = svc.project_info(pid, owner)["project_seq"]
    body = {
        "title": "Synthetic LaTeX workspace paper",
        "objective": "Exercise the LaTeX workspace on synthetic content.",
        "claim_refs": [],
        "manuscript_source_id": None,
        "venue_source_refs": [],
        "status": "drafting",
        "deadline": None,
        "requirements": [],
        "contribution": "Synthetic.",
        "related_papers": [],
        "review_rounds": [],
    }
    op = {"op": "create", "kind": "paper", "id": paper, "base_rev": 0, "sensitivity": "normal",
          "occurred_at": None, "body": body}  # fmt: skip
    request = {"schema_version": 1, "project_id": pid, "request_id": new_request_id(),
               "expected_seq": seq, "operations": [op]}  # fmt: skip
    svc.apply(pid, json.dumps(request).encode("utf-8"), owner)
    svc.overleaf_link(pid, owner, paper, str(bare), "research", "latex")
    research = Path(str(project["research_root"]))
    main = research / "latex" / "main.tex"
    lines = main.read_text(encoding="utf-8").splitlines()
    line = next(i for i, t in enumerate(lines, 1) if "settling time" in t)
    source_id = svc.get(pid, owner, paper, None)["body"]["manuscript_source_id"]
    versions = svc.list_records(pid, owner, kind="version", limit=200)["items"]
    version = next(v for v in versions if v["body"]["source_id"] == source_id)
    claim, anchor = new_id("claim"), new_id("anchor")
    evidence = {"source_id": source_id, "version_id": version["id"],
                "locator": {"type": "text", "start_line": line, "end_line": line},
                "quote_sha256": None}  # fmt: skip
    claim_body = {
        "statement": "The synthetic controller reduces settling time in the simulated regime.",
        "scope": {"system": "synthetic controller", "operating_regime": "simulated regime",
                  "comparator": "reference controller", "model_fidelity": "toy model",
                  "validation_level": "none (synthetic)"},
        "evidence": [evidence],
        "assessment_refs": [],
        "state": "proposed",
        "superseded_by": None,
    }  # fmt: skip
    anchor_body = {"paper_id": paper, "manuscript_version_id": version["id"],
                   "locator": {"type": "text", "start_line": line, "end_line": line},
                   "claim_refs": [{"id": claim, "rev": 1}], "evidence": [evidence],
                   "status": "current", "reason": None}  # fmt: skip
    seq = svc.project_info(pid, owner)["project_seq"]
    ops = [
        {"op": "create", "kind": "claim", "id": claim, "base_rev": 0, "sensitivity": "normal",
         "occurred_at": None, "body": claim_body},
        {"op": "create", "kind": "anchor", "id": anchor, "base_rev": 0, "sensitivity": "normal",
         "occurred_at": None, "body": anchor_body},
    ]  # fmt: skip
    request = {"schema_version": 1, "project_id": pid, "request_id": new_request_id(),
               "expected_seq": seq, "operations": ops}  # fmt: skip
    svc.apply(pid, json.dumps(request).encode("utf-8"), owner)
    return {
        "latex_paper_id": paper,
        "latex_claim_id": claim,
        "latex_anchor_id": anchor,
        "latex_folder": str(research / "latex"),
        "latex_collaborator": str(collaborator),
        "latex_bare": str(bare),
        "latex_anchor_line": line,
        "tex_engine": texengine.status(Path(str(project["home"])))["state"],
    }


def _owner() -> object:
    from prometheos.app.reads import Caller

    return Caller(actor="hub-fixture", scope="owner")


def build_owner_flow(work: Path) -> dict[str, object]:
    """CP11 S11.4b (decision 0066 §3): a fresh project with NO research folder, and a synthetic
    papers folder (PDFs and .bib/.ris/.csv files) on disk for the owner to add in the hub."""
    import synthetic_bib
    from synthetic_pdf import synthetic_set, write_folder

    from prometheos.app.service import Service

    base = work / "owner-flow"
    papers = base / "papers"
    write_folder(papers, synthetic_set())
    synthetic_bib.write_folder(papers, synthetic_bib.synthetic_set())
    # Folders an agent asks for (S11.4b review F1): an ordinary one the owner grants, and a
    # hidden one with a sensitive name the owner sees flagged and denies.
    (base / "notes").mkdir()
    (base / ".api-keys").mkdir()
    svc = Service(home=base / "home", actor="hub-fixture")
    made = svc.init_project(
        title="Synthetic flywheel literature (owner flow)",
        objective="Compare flywheel losses across papers, every value quoted from its source.",
        timezone="Europe/London",
    )
    return {
        "home": str(base / "home"),
        "project_id": made["project_id"],
        "papers": str(papers),
        "notes": str(base / "notes"),
        "hidden": str(base / ".api-keys"),
    }


def build_damaged(work: Path) -> dict[str, object]:
    """CP10.5 (S27): a second disposable project, in its own home, with a verified backup in
    its store's exports folder and then a deliberately corrupted ``record.sqlite``. Its hub
    starts in maintenance mode (the recovery screen)."""
    import hashlib
    import shutil

    from prometheos.app.reads import Caller
    from prometheos.app.service import Service

    research = work / "recovery-research"
    research.mkdir(parents=True)
    shutil.copyfile(ROOT / "fixtures" / "baseline.md", research / "baseline.md")
    svc = Service(home=work / "recovery-home", actor="hub-fixture")
    made = svc.init_project(
        title="Damaged store (recovery fixture)",
        objective="Recover the record from a verified backup without touching the damaged store.",
        timezone=None,
        roots={"research": str(research)},
    )
    pid = str(made["project_id"])
    plan = svc.import_plan(
        pid, root_alias="research", paths=["baseline.md"], retention="copy", sensitivity="normal"
    )
    svc.import_commit(pid, plan["plan_id"])
    backup = svc.backup(pid, None, Caller(actor="hub-fixture", scope="owner", full_data=True))
    database = Path(str(made["store"])) / "record.sqlite"
    data = database.read_bytes()
    database.write_bytes(b"\x00" * 100 + data[100:])  # the header is gone: not a database
    return {
        "home": str(work / "recovery-home"),
        "project_id": pid,
        "backup_name": Path(str(backup["backup"])).name,
        "damaged_db_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        "project_seq": backup["manifest"]["project_seq"],
    }


def unavailable_models(work: Path) -> Path:
    """Explicit empty synthetic model folder for the unavailable-state browser journey.

    The fixture replaces the staged-model override only in this mode, never clearing it or
    falling back to owner models. An occupied folder refuses without deleting any bytes.
    """
    from prometheos.audio import models

    directory = work / "unavailable-speech-models"
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("the unavailable-model fixture directory must be empty")
    os.environ[models.MODELS_DIR_ENV] = str(directory)
    return directory


class RequestLog:
    """ASGI wrapper (this fixture only): one line per API request on stderr, which the browser
    tests keep as ``hub.log`` (decision 0028): time, server, method, path (never the query),
    status and milliseconds. With gitsync's slow-git lines it shows which request a loaded host
    held up, and for how long (review cp10-browser R4-F2)."""

    def __init__(self, app: Any, server: str) -> None:
        self.app, self.server = app, server

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status: list[int] = []

        async def recorded(message: Any) -> None:
            if message["type"] == "http.response.start":
                status.append(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, recorded)
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            answer = status[0] if status else "no-response"
            print(
                f"{time.strftime('%H:%M:%S')} {self.server} {scope['method']} {scope['path']} "
                f"{answer} {elapsed} ms",
                file=sys.stderr,
                flush=True,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--info", type=Path, required=True, help="JSON file to write")
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--tokens", type=int, default=20, help="one-use pairing tokens")
    parser.add_argument("--static", type=Path, default=None, help="serve web assets from here")
    parser.add_argument(
        "--unavailable-models",
        action="store_true",
        help="use an empty synthetic model folder for unavailable-state browser checks",
    )
    args = parser.parse_args()
    # Warnings (gitsync's slow or stalled git commands) go to stderr, kept as hub.log.
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    work = args.work or Path(tempfile.mkdtemp(prefix="prometheos-hub-fixture-"))
    work.mkdir(parents=True, exist_ok=True)
    if args.unavailable_models:
        unavailable_models(work)
    project = build(work)
    damaged = build_damaged(work)

    import threading

    from prometheos.app.service import Service
    from prometheos.hub.app import create_app

    svc = Service(home=Path(str(project["home"])), actor="hub")
    # CP10.5: the recovery hub (maintenance mode) on the damaged project, in a second thread.
    recovery_svc = Service(home=Path(str(damaged["home"])), actor="hub")
    recovery_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    recovery_sock.bind(("127.0.0.1", 0))
    recovery_port = recovery_sock.getsockname()[1]
    recovery_gate = MultiTokenGate(port=recovery_port)
    recovery_app = create_app(recovery_svc, str(damaged["project_id"]), recovery_gate, args.static)
    assert recovery_app.state.maintenance, "the damaged project must start in maintenance mode"
    recovery = {
        **damaged,
        "url": f"http://127.0.0.1:{recovery_port}/",
        "port": recovery_port,
        "pairing_tokens": [recovery_gate.add_token() for _ in range(10)],
        "agent_token": recovery_gate.new_agent_token(),
    }
    recovery_server = uvicorn.Server(
        uvicorn.Config(
            RequestLog(recovery_app, "recovery"),
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    threading.Thread(
        target=recovery_server.run, kwargs={"sockets": [recovery_sock]}, daemon=True
    ).start()
    # CP11 S11.4b: a third hub on a project with no research folder (e2e 24, the owner flow).
    owner_flow = build_owner_flow(work)
    flow_svc = Service(home=Path(str(owner_flow["home"])), actor="hub")
    flow_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    flow_sock.bind(("127.0.0.1", 0))
    flow_port = flow_sock.getsockname()[1]
    flow_gate = MultiTokenGate(port=flow_port)
    flow_app = create_app(flow_svc, str(owner_flow["project_id"]), flow_gate, args.static)
    # CP11 S11.5 (e2e 25): a Zotero stub for the main hub (the home's zotero.json points at it)
    # and one with the local API off for the owner-flow hub. Each logs every request it sees.
    from zotero_stub import ZoteroStub

    zotero = ZoteroStub(log_path=work / "zotero-requests.jsonl")
    zotero_off = ZoteroStub(mode="disabled", log_path=work / "zotero-off-requests.jsonl")
    for stub, home in ((zotero, project["home"]), (zotero_off, owner_flow["home"])):
        (Path(str(home)) / "zotero.json").write_text(
            json.dumps({"port": stub.start()}), encoding="utf-8"
        )
    owner_flow = {
        **owner_flow,
        "url": f"http://127.0.0.1:{flow_port}/",
        "port": flow_port,
        "pairing_tokens": [flow_gate.add_token() for _ in range(10)],
        "agent_token": flow_gate.new_agent_token(),
    }
    flow_server = uvicorn.Server(
        uvicorn.Config(
            RequestLog(flow_app, "owner-flow"),
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    threading.Thread(target=flow_server.run, kwargs={"sockets": [flow_sock]}, daemon=True).start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    gate = MultiTokenGate(port=port)
    tokens = [gate.add_token() for _ in range(args.tokens)]
    info = {
        **project,
        "url": f"http://127.0.0.1:{port}/",
        "port": port,
        "pairing_tokens": tokens,
        # A02, CP10.2: a local agent's bearer token (agent-scoped routes only).
        "agent_token": gate.new_agent_token(),
        "work": str(work),
        "recovery": recovery,
        "owner_flow": owner_flow,
        "zotero": {
            "log": str(work / "zotero-requests.jsonl"),
            "off_log": str(work / "zotero-off-requests.jsonl"),
            "port": zotero.port,
        },
    }
    args.info.write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps({"url": info["url"], "info": str(args.info)}), flush=True)
    app = create_app(svc, str(project["project_id"]), gate, args.static)
    uvicorn.Server(
        uvicorn.Config(
            RequestLog(app, "hub"), log_level="warning", access_log=False, lifespan="off"
        )
    ).run(sockets=[sock])
    return 0


@dataclass
class MultiTokenGate(Gate):
    """Test-only: a queue of independent one-use tokens (one per browser test)."""

    _extra: dict[str, float] = field(default_factory=dict)

    def add_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._extra[_digest(token)] = time.monotonic() + 3600
        return token

    def pair(self, token: str) -> tuple[str, str] | None:
        expires = self._extra.pop(_digest(token), None)
        if expires is None or time.monotonic() > expires:
            return super().pair(token)
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[_digest(session_id)] = _Session(csrf, time.monotonic() + SESSION_TTL)
        return session_id, csrf


if __name__ == "__main__":
    sys.exit(main())
