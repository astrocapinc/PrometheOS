"""C10-PARITY, equivalence half (spec/04, decision 0022 item 4): the same requests give the same
records, errors and authority outcomes through the service, the MCP family tools (official SDK
client) and the local HTTP API (agent bearer token), including denied requests.

One scripted agent session runs on three fresh worlds, one per route. Every step is a registry
operation with parameters built from the world; its outcome (result or error code, with IDs,
times and local paths normalised) must be equal on the three routes, and the reference route's
outcome must be the one the step is declared to produce (so a step that is denied is checked as
denied, not merely equal). The CLI is compared on the pure reads it shares.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cp10_world import World, failed, normalise, ok, running_routes
from test_cp1_cli import Cli

from prometheos import agentops, registry
from prometheos.domain.ids import new_request_id
from prometheos.domain.redact import ABSOLUTE, is_path_key

agentops.load()

Memo = dict[str, Any]


@dataclass
class Step:
    label: str
    op: str
    params: Callable[[World, Memo], dict[str, Any]]
    expect: str = "ok"  # "ok" or an error code
    capture: Callable[[Any, Memo], None] | None = None
    http: bool = True  # False: the HTTP form is a binary route covered elsewhere


def P(**fixed: Any) -> Callable[[World, Memo], dict[str, Any]]:
    return lambda w, m: dict(fixed)


def R(alias: str = "research", **more: Any) -> dict[str, Any]:
    return {"root_alias": alias, **more}


STEPS: list[Step] = [
    # ----- audio (first; the test also keeps the never-captured session from reading abandoned)
    Step("devices", "audio_devices", P()),
    Step("transcriptions", "transcription_list", P()),
    Step("recordings", "recording_list", P(include_done=True)),
    Step("recording status", "recording_status", lambda w, m: {"session_id": w.session}),
    Step(
        "pause the agent's recording",
        "recording_control",
        lambda w, m: {"session_id": w.session, "action": "pause"},
    ),
    Step(
        "control the owner's recording",
        "recording_control",
        lambda w, m: {"session_id": w.owner_session, "action": "stop"},
        "policy_denied",
    ),
    Step(
        "discard the owner's recording",
        "recording_discard",
        lambda w, m: {"session_id": w.owner_session, "confirm": True},
        "policy_denied",
    ),
    Step(
        "discard without confirming",
        "recording_discard",
        lambda w, m: {"session_id": w.session},
        "validation_error",
    ),
    Step(
        "recover a session that recorded nothing",
        "recording_recover",
        lambda w, m: {"session_id": w.session},
        "validation_error",
    ),
    Step(
        "discard a live recording",
        "recording_discard",
        lambda w, m: {"session_id": w.session, "confirm": True},
        "validation_error",
    ),
    Step(
        "discard an ended recording",
        "recording_discard",
        lambda w, m: {"session_id": w.ended_session, "confirm": True},
    ),
    # ----- records, context, meetings, workflows -----------------------------------------
    Step("history", "record_history", lambda w, m: {"record_id": w.paper_id}),
    Step(
        "history of an unknown record",
        "record_history",
        P(record_id="paper_00000000-0000-4000-8000-000000000009"),
        "not_found",
    ),
    Step(
        "version text page 1",
        "version_text",
        lambda w, m: {"version_id": w.venue_version, "offset": 0, "limit": 80},
    ),
    Step(
        "version text page 2",
        "version_text",
        lambda w, m: {"version_id": w.venue_version, "offset": 80, "limit": 80},
    ),
    Step(
        "version text of an unknown version",
        "version_text",
        P(version_id="version_00000000-0000-4000-8000-000000000009"),
        "not_found",
    ),
    Step(
        "version content page",
        "version_content",
        lambda w, m: {"version_id": w.manuscript_version, "offset": 0, "limit": 64},
        http=False,
    ),
    Step("next steps", "context_next", P()),
    Step("notebook export (Markdown)", "notebook_export", P()),
    Step("notebook export (JSON, small pages)", "notebook_export", P(format="json", limit=40)),
    Step("agenda", "context_agenda", lambda w, m: {"meeting_id": w.meeting}),
    Step("reconcile", "meeting_reconcile", lambda w, m: {"meeting_id": w.meeting}),
    Step(
        "capture a meeting",
        "meeting_capture",
        lambda w, m: {
            "title": "Check-in",
            "text": "We agreed to rerun the sweep.",
            "request_id": m.setdefault("capture", new_request_id()),
        },
    ),
    Step(
        "replay the capture",
        "meeting_capture",
        lambda w, m: {
            "title": "Check-in",
            "text": "We agreed to rerun the sweep.",
            "request_id": m["capture"],
        },
    ),
    Step("workflow export", "workflow_export", lambda w, m: {"run_id": w.run}),
    # ----- skills --------------------------------------------------------------------------
    Step("catalogue", "skill_catalog", P()),
    Step("verify", "skill_verify", P(ref="synthetic-evidence@1.0.0")),
    Step("inspect", "skill_inspect", P(**R(path="pkgs/second-evidence"))),
    Step("validate", "skill_validate", P(**R(path="skills-src/bare-skill"))),
    Step(
        "validate a folder that is not a skill",
        "skill_validate",
        P(**R(path="exports")),
        "validation_error",
    ),
    Step("inspect outside the root", "skill_inspect", P(**R(path="../outside")), "policy_denied"),
    Step(
        "inspect in an unapproved root",
        "skill_inspect",
        P(**R("elsewhere", path="x")),
        "policy_denied",
    ),
    Step(
        "import plan",
        "skill_import_plan",
        P(**R(path="pkgs/second-evidence")),
        capture=lambda r, m: m.update(import_digest=r["plan_sha256"]),
    ),
    Step(
        "import the reviewed plan",
        "skill_import",
        lambda w, m: {**R(path="pkgs/second-evidence"), "plan_sha256": m["import_digest"]},
    ),
    Step(
        "import with a stale digest",
        "skill_import",
        P(**R(path="pkgs/second-evidence"), plan_sha256="0" * 64),
        "revision_conflict",
    ),
    Step(
        "install plan",
        "skill_install",
        P(ref="second-evidence@1.0.0", **R(folder="project-a")),
        capture=lambda r, m: m.update(install_digest=r["plan"]["plan_sha256"]),
    ),
    Step(
        "install the reviewed plan",
        "skill_install",
        lambda w, m: {
            "ref": "second-evidence@1.0.0",
            **R(folder="project-a"),
            "plan_sha256": m["install_digest"],
            "record_in_project": True,
        },
        "approval_required",
    ),
    Step("update plan", "skill_update", P(ref="second-evidence@1.0.0", **R(folder="project-a"))),
    Step(
        "remove plan",
        "skill_remove",
        P(name="synthetic-evidence", **R(folder="installed-a")),
        capture=lambda r, m: m.update(remove_digest=r["plan"]["plan_sha256"]),
    ),
    Step(
        "remove the reviewed plan",
        "skill_remove",
        lambda w, m: {
            "name": "synthetic-evidence",
            **R(folder="installed-a"),
            "plan_sha256": m["remove_digest"],
        },
        "approval_required",
    ),
    Step(
        "create a package",
        "skill_create",
        P(name="new-skill", description="A new skill.", license="MIT", **R(folder="exports")),
    ),
    Step(
        "create it again",
        "skill_create",
        P(name="new-skill", description="A new skill.", license="MIT", **R(folder="exports")),
        "revision_conflict",
    ),
    Step("refresh", "skill_refresh", P(**R(path="exports/new-skill"))),
    Step("export", "skill_export", P(ref="second-evidence@1.0.0", **R(path="exports/pkg.zip"))),
    Step(
        "adapt",
        "skill_adapt",
        P(ref="second-evidence@1.0.0", name="adapted-skill", **R(folder="exports")),
    ),
    Step("host setup plan", "host_setup", P(host="claude-code", **R(folder="project-a"))),
    Step(
        "host setup apply", "host_setup", P(host="claude-code", apply=True, **R(folder="project-a"))
    ),
    Step("revoke", "skill_revoke", P(ref="second-evidence@1.0.0")),
    # ----- papers --------------------------------------------------------------------------
    Step("paper overview", "paper_get", lambda w, m: {"paper_id": w.paper_id}),
    Step("paper numbers", "paper_numbers", lambda w, m: {"paper_id": w.paper_id}),
    Step("research compass", "research_compass", P(limit=5)),
    Step(
        "a receipt naming a file that is not there (decision 0112)",
        "source_receipt_from_paths",
        P(
            run={
                "reporter": "agent",
                "command": "run",
                "working_root": None,
                "environment": {"os": "TestOS", "tools": []},
                "started_at": None,
                "ended_at": None,
                "exit_status": 0,
                "outcome": "succeeded",
                "limitations": [],
            },
            inputs=[R(path="no-such-file.csv", role="data")],
            outputs=[],
            title="Run",
            request_id="7d3c4a52-3c1e-4d2a-9a51-1d7f2b0e9c11",
        ),
        "source_unavailable",
    ),
    Step(
        "requirements draft",
        "paper_requirements_draft",
        lambda w, m: {"version_id": w.venue_version},
    ),
    Step(
        "handoff plan",
        "paper_handoff_plan",
        lambda w, m: {"paper_id": w.paper_id, **R(folder="handoff")},
        capture=lambda r, m: m.update(handoff_digest=r["digest"]),
    ),
    Step(
        "handoff apply",
        "paper_handoff_apply",
        lambda w, m: {"paper_id": w.paper_id, **R(folder="handoff"), "digest": m["handoff_digest"]},
    ),
    Step(
        "handoff apply with a stale digest",
        "paper_handoff_apply",
        lambda w, m: {"paper_id": w.paper_id, **R(folder="handoff"), "digest": "0" * 64},
        "revision_conflict",
    ),
    Step(
        "save the readiness check",
        "paper_check_save",
        lambda w, m: {
            "paper_id": w.paper_id,
            "request_id": m.setdefault("check", new_request_id()),
        },
    ),
    Step(
        "export the bundle",
        "paper_export",
        lambda w, m: {"paper_id": w.paper_id, **R(path="exports/bundle.zip")},
    ),
    Step(
        "export it again",
        "paper_export",
        lambda w, m: {"paper_id": w.paper_id, **R(path="exports/bundle.zip")},
        "validation_error",
    ),
    # ----- jobs ----------------------------------------------------------------------------
    Step("job plan", "job_plan", lambda w, m: {"job": w.job_body}),
    Step(
        "add a job",
        "job_add",
        lambda w, m: {"job": w.job_body},
        capture=lambda r, m: m.update(job=r["job_id"]),
    ),
    Step(
        "jobs",
        "job_list",
        P(),
        capture=lambda r, m: m.update(slot=r["jobs"][0]["next_slots"][0]["due_utc"]),
    ),
    Step("slots", "job_slots", lambda w, m: {"job_id": m["job"]}),
    Step(
        "disable",
        "job_set_enabled",
        lambda w, m: {"job_id": m["job"], "base_rev": 1, "enabled": False},
    ),
    Step(
        "disable with a stale revision",
        "job_set_enabled",
        lambda w, m: {"job_id": m["job"], "base_rev": 1, "enabled": True},
        "revision_conflict",
    ),
    Step(
        "cancel a slot that is not recorded yet",
        "job_cancel",
        lambda w, m: {"job_id": m["job"], "slot": m["slot"]},
        "not_found",
    ),
    Step("tick", "jobs_tick", P()),
    Step("inbox", "jobs_inbox", P()),
    Step("dismiss an unknown item", "jobs_dismiss", P(notification_id=99), "not_found"),
    # ----- administration, schemas, provider, engine --------------------------------------
    Step("doctor", "doctor", P()),
    Step("schema names", "schema_list", P()),
    Step("one schema", "schema_show", P(name="entry_body")),
    Step("an unknown schema", "schema_show", P(name="no_such_schema"), "not_found"),
    # ----- adoption (A05, CP17; decision 0039): reads of stored scans --------------------------
    Step("adoption scans", "adoption_scans", P()),
    Step("files of an unknown scan", "adoption_files", P(scan_gen=9), "not_found"),
    Step("evidence of an unknown scan", "adoption_evidence", P(scan_gen=9), "not_found"),
    Step("analyzer declarations", "adoption_analyzers", P()),
    Step("assist tasks of an unknown scan", "adoption_assist_tasks", P(scan_gen=9), "not_found"),
    Step(
        "prepare a task in an unknown scan",
        "adoption_assist_prepare",
        P(scan_gen=9, task_kind="classify_file", subject="baseline.md"),
        "not_found",
    ),
    Step(
        "submit to an unknown scan",
        "adoption_assist_submit",
        P(
            scan_gen=9,
            task_kind="classify_file",
            subject="baseline.md",
            sample=1,
            response={"role": "notes", "quote": None},
        ),
        "not_found",
    ),
    Step("families of an unknown scan", "adoption_families", P(scan_gen=9), "not_found"),
    Step(
        "a family of an unknown scan",
        "adoption_family",
        P(scan_gen=9, family_id="fam_0123456789abcdef"),
        "not_found",
    ),
    # CP17.5 (decision 0043): the plan, step reports, owner acts as requests, baselines
    Step("plan of an unknown scan", "adoption_plan", P(scan_gen=9), "not_found"),
    Step("plan an unknown scan", "adoption_plan_create", P(scan_gen=9), "not_found"),
    Step(
        "report a step of an unknown scan",
        "adoption_step_report",
        P(scan_gen=9, step_id="st_0123456789abcdef", result={"summary": "x"}),
        "not_found",
    ),
    Step(
        "decide in an unknown scan (an agent's request)",
        "adoption_decide",
        P(scan_gen=9, family_id="fam_0123456789abcdef", current="a.md"),
        "not_found",
    ),
    Step(
        "classify in an unknown scan (an agent's request; A06.1, CP20.4)",
        "adoption_classify",
        P(scan_gen=9, family_id="fam_0123456789abcdef", decision="variant"),
        "not_found",
    ),
    Step(
        "the outline of an unknown scan (A06.1, CP20.4)",
        "adoption_outline",
        P(scan_gen=9),
        "not_found",
    ),
    Step(
        "cite from an unknown baseline",
        "adoption_cite",
        P(baseline_id="bl_000000000000000000000000", path="a.csv"),
        "not_found",
    ),
    Step(
        "preview an unknown scan's outline",
        "adoption_outline_preview",
        P(scan_gen=9, proposal_sha256="0" * 64),
        "not_found",
    ),
    Step(
        "confirm an unknown scan's outline (an agent's request)",
        "adoption_outline_confirm",
        P(scan_gen=9, proposal_sha256="0" * 64),
        "not_found",
    ),
    Step(
        "accept an unknown scan (an agent's request)",
        "adoption_baseline_accept",
        P(scan_gen=9),
        "not_found",
    ),
    Step(
        "an unknown baseline",
        "adoption_baseline",
        P(baseline_id="bl_000000000000000000000000"),
        "not_found",
    ),
    # CP17.6 (decision 0045): drift, the baseline package, vault import, the fenced export
    Step("baselines", "adoption_baselines", P()),
    Step("drift of an unknown scan", "adoption_drift", P(scan_gen=9), "not_found"),
    Step(
        "export an unknown baseline",
        "adoption_baseline_export",
        P(baseline_id="bl_000000000000000000000000"),
        "not_found",
    ),
    Step(
        "a vault that is not an approved root",
        "adoption_vault_import",
        P(root_alias="no-such-vault"),
        "policy_denied",
    ),
    Step(
        "export an unknown baseline to a note (an agent's request)",
        "adoption_export_markdown",
        P(baseline_id="bl_000000000000000000000000", root_alias="research"),
        "not_found",
    ),
    Step("projects", "project_list", P()),
    Step(
        "purge plan (owner only)",
        "retention_purge_plan",
        lambda w, m: {"ids": [w.paper_id]},
        "policy_denied",
    ),
    Step("orphans", "maintenance_orphans", P()),
    Step("provider calls", "provider_calls", P()),
    # A07, CP22.4 (decision 0094): the intelligence layer
    Step("the standing policy", "ai_policy_show", P()),
    Step("layer results", "ai_results", P()),
    Step("layer evaluations", "ai_evaluations", P()),
    Step("root sensitivities", "root_sensitivity_show", P()),
    Step(
        "raise a root's sensitivity",
        "root_sensitivity_set",
        P(root_alias="research", sensitivity="private"),
    ),
    Step(
        "an invalid policy grant (an agent's request)",
        "ai_policy_grant",
        P(policy={"tasks": []}, binding_sha256="0" * 64),
        "validation_error",
    ),
    Step("revoke the policy (the owner's alone)", "ai_policy_revoke", P(), "policy_denied"),
    Step(
        "accept a layer proposal in an unknown scan (an agent's request)",
        "ai_accept",
        P(task_kind="adoption.classify_file", scan_gen=9, subject="a.md"),
        "not_found",
    ),
    Step(
        "a map task with no standing policy",
        "ai_map_suggest",
        P(dry_run=True),
        "capability_unavailable",
    ),
    Step(
        "a layer run on an unknown scan",
        "ai_run",
        P(task_kind="adoption.classify_file", scan_gen=9, paths=["a.md"]),
        "not_found",
    ),
    Step(
        "provider configuration preview",
        "provider_configure_preview",
        P(config={"policy": "no-ai"}),
        "validation_error",
    ),
    Step("engine plan", "tex_engine_plan", P()),
    Step(
        "create a project",
        "project_init",
        P(title="Other study", objective="Compare."),
        "policy_denied",
    ),
    # ----- the literature library (CP11 S11.1): each world imports the same synthetic PDFs
    # (owner, wait mode) when the first library step runs; the steps then read them.
    Step("library works", "library_works", lambda w, m: {"limit": 10, **library(w, {})}),
    Step("library work", "library_work", lambda w, m: {"work_id": library(w)["drag"]}),
    Step("library search", "library_search", P(query="drag coefficient", limit=5)),
    Step("library search without words", "library_search", P(query="?!"), "validation_error"),
    Step(
        "library read",
        "library_read",
        lambda w, m: {"work_id": library(w)["drag"], "page": 2, "max_chars": 40},
    ),
    Step(
        "library read past the last page",
        "library_read",
        lambda w, m: {"work_id": library(w)["drag"], "page": 9},
        "validation_error",
    ),
    Step(
        "library quote check (match)",
        "library_quote_check",
        lambda w, m: {**library(w)["where"], "quote": "drag coefficient"},
    ),
    Step(
        "library quote check (altered)",
        "library_quote_check",
        lambda w, m: {**library(w)["where"], "quote": "drag coefficients"},
    ),
    Step(
        "library quote check of an unknown work",
        "library_quote_check",
        P(
            work_id="work_00000000-0000-4000-8000-000000000009",
            page=1,
            start=0,
            end=4,
            quote="Aero",
        ),
        "not_found",
    ),
    # CP11 S11.2: candidates, export, annotations, and the owner-only resolve.
    Step("library dedup", "library_dedup", P()),
    Step("library export", "library_export", P(format="bibtex")),
    Step("library export as CSV", "library_export", P(format="csv")),
    Step(
        "library export of an unknown work",
        "library_export",
        P(format="ris", work_ids=["work_00000000-0000-4000-8000-000000000009"]),
        "not_found",
    ),
    Step(
        "library annotate",
        "library_annotate",
        lambda w, m: {
            **library(w)["where"],
            "text": "Compare with the rig.",
            "request_id": "00000000-0000-4000-8000-0000000000a1",
        },
    ),
    Step(
        "library annotations", "library_annotations", lambda w, m: {"work_id": library(w)["drag"]}
    ),
    Step(
        "library resolve (an agent asks)",
        "library_dedup_resolve",
        lambda w, m: {**library(w)["pair"], "choice": "distinct"},
        "approval_required",
    ),
    Step(
        "library undo with nothing to undo (an agent asks)",
        "library_dedup_undo",
        lambda w, m: dict(library(w)["pair"]),
        "not_found",
    ),
    # CP11 S11.4b: research folders (listed; an agent's addition is an approval request).
    Step("project roots", "project_roots", P()),
    Step(
        "add a research folder (an agent asks)",
        "project_root_add",
        lambda w, m: {"alias": "papers", "path": str(papers_folder(w))},
        "approval_required",
    ),
    Step(
        # One refusal for every location rule, checked without touching the disk (S11.4b
        # review F5): an agent cannot probe this computer's folders.
        "add a research folder at a refused location (an agent asks)",
        "project_root_add",
        lambda w, m: {"alias": "probe", "path": str(Path(w.root.anchor))},
        "validation_error",
    ),
    # CP11 S11.5: the read-only Zotero adapter's probe (a loopback stub per world; the stub is
    # stopped by the last step, so that one reads "not running").
    Step("zotero status", "zotero_status", lambda w, m: zotero(w)),
    Step("zotero status with collections", "zotero_status", P(collections=True)),
    Step(
        "zotero status when Zotero is not running",
        "zotero_status",
        lambda w, m: zotero(w, stop=True),
    ),
    # CP11 S11.4: evidence matrices (grounded cells, the owner's review, rules, briefs).
    Step(
        "matrix create",
        "matrix_create",
        lambda w, m: {
            "title": "Flywheel losses (agent)",
            "columns": MATRIX_COLUMNS,
            "work_ids": [library(w)["drag"]],
            "request_id": "00000000-0000-4000-8000-0000000000b1",
        },
    ),
    Step("matrix list", "matrix_list", P()),
    Step("matrix show", "matrix_show", lambda w, m: {"matrix_id": matrix(w)["matrix_id"]}),
    Step(
        "matrix show of an unknown matrix",
        "matrix_show",
        P(matrix_id="matrix_00000000-0000-4000-8000-000000000009"),
        "not_found",
    ),
    Step(
        "matrix propose a quoted quantity",
        "matrix_propose",
        lambda w, m: {
            **matrix(w)["cell"],
            "quantities": [{"decimal": "0.0042", "unit": ""}, {"decimal": "10", "unit": "Pa"}],
            "request_id": "00000000-0000-4000-8000-0000000000b2",
        },
    ),
    Step(
        "matrix propose a misquoted number",
        "matrix_propose",
        lambda w, m: {
            **matrix(w)["cell"],
            "value_text": "0.042",
            "quantities": [{"decimal": "0.042", "unit": ""}],
        },
        "validation_error",
    ),
    Step(
        "matrix propose with an invented version",
        "matrix_propose",
        lambda w, m: {
            **matrix(w)["cell"],
            "evidence": [
                {
                    **matrix(w)["cell"]["evidence"][0],
                    "version_id": "version_00000000-0000-4000-8000-000000000009",
                }
            ],
        },
        "not_found",
    ),
    Step(
        "matrix review (an agent asks)",
        "matrix_review",
        lambda w, m: {"matrix_id": matrix(w)["matrix_id"], "work_id": library(w)["drag"]},
        "approval_required",
    ),
    Step(
        "matrix unsupported",
        "matrix_unsupported",
        lambda w, m: {
            "matrix_id": matrix(w)["matrix_id"],
            "work_id": library(w)["drag"],
            "column_id": "finding",
            "reason": "no finding stated",
            "request_id": "00000000-0000-4000-8000-0000000000b3",
        },
    ),
    Step(
        "matrix extract",
        "matrix_extract",
        lambda w, m: {
            "matrix_id": matrix(w)["matrix_id"],
            "rules": {
                "rules": [
                    {
                        "id": "pressure",
                        "column": "pressure",
                        "pattern": "evacuated to {value}",
                        "unit": "Pa",
                    },
                ]
            },
        },
    ),
    Step(
        "matrix export as CSV",
        "matrix_export",
        lambda w, m: {"matrix_id": matrix(w)["matrix_id"], "format": "csv"},
    ),
    Step(
        "matrix export the brief",
        "matrix_export",
        lambda w, m: {
            "matrix_id": matrix(w)["matrix_id"],
            "format": "markdown",
            "request_id": "00000000-0000-4000-8000-0000000000b4",
        },
    ),
]


def papers_folder(world: World) -> Path:
    """A folder beside the research root (no overlap, outside the PrometheOS home)."""
    folder = world.root.parent / "papers-parity"
    folder.mkdir(exist_ok=True)
    return folder


MATRIX_COLUMNS = [
    {"id": "pressure", "label": "Pressure", "value_kind": "quantity", "instructions": ""},
    {"id": "drag", "label": "Drag coefficient", "value_kind": "quantity", "instructions": ""},
    {"id": "windage", "label": "Windage loss", "value_kind": "quantity", "instructions": ""},
    {"id": "finding", "label": "Key finding", "value_kind": "text", "instructions": ""},
]


def matrix(world: World) -> Any:
    """A matrix over the library's drag paper (made by the owner once) and the evidence of the
    sentence "The drag coefficient was 0.0042 at 10 Pa." on its page 2."""
    from prometheos.app.reads import Caller

    cached = getattr(world, "matrix", None)
    if cached is None:
        owner = Caller(actor="test", scope="owner")
        drag = library(world)["drag"]
        made = world.svc.matrix_create(
            world.pid, owner, title="Flywheel losses", columns=MATRIX_COLUMNS, work_ids=[drag]
        )
        sentence = "The drag coefficient was 0.0042 at 10 Pa."
        text = world.svc.library_read(world.pid, owner, drag, page=2, max_chars=8000)["text"]
        at = text.index(sentence)
        read = world.svc.library_read(
            world.pid, owner, drag, page=2, start=at, end=at + len(sentence)
        )
        cached = {
            "matrix_id": made["matrix_id"],
            "cell": {
                "matrix_id": made["matrix_id"],
                "work_id": drag,
                "column_id": "drag",
                "value_text": "0.0042 at 10 Pa",
                "evidence": [read["evidence"]],
            },
        }
        world.matrix = cached  # type: ignore[attr-defined]
    return cached


def zotero(world: World, stop: bool = False) -> dict[str, Any]:
    """A Zotero stub on loopback for ``world`` (started once, pointed at by the home's
    ``zotero.json``); ``stop`` ends it. Returns no parameters."""
    import json as _json

    from zotero_stub import ZoteroStub

    stub = getattr(world, "zotero_stub", None)
    if stub is None:
        stub = ZoteroStub()
        port = stub.start()
        (world.env.home / "zotero.json").write_text(_json.dumps({"port": port}), encoding="utf-8")
        world.zotero_stub = stub  # type: ignore[attr-defined]
    if stop:
        stub.stop()
    return {}


PARITY_BIB = (
    "@article{a, title={Windage in sealed housings}, author={Doe, J.}, year={2024}}\n"
    "@article{b, title={Windage in Sealed Housings}, author={Doe, Jane}, year={2025}}\n"
)


def library(world: World, extra: dict[str, Any] | None = None) -> Any:
    """Import two synthetic PDFs into ``world`` once; ``extra`` (a dict) is returned as is."""
    from synthetic_pdf import DRAG, KERS, paper, write_folder

    from prometheos.app.reads import Caller

    cached = getattr(world, "library", None)
    if cached is None:
        write_folder(
            world.root / "library",
            {
                "drag.pdf": paper(DRAG),
                "kers.pdf": paper(KERS),
                "refs.bib": PARITY_BIB.encode("utf-8"),
            },
        )
        owner = Caller(actor="test", scope="owner")
        plan = world.svc.library_import_plan(
            world.pid, owner, root_alias="research", path="library"
        )
        world.svc.library_import_commit(world.pid, plan["plan_id"], owner)
        works = world.svc.library_works(world.pid, owner)["works"]
        drag = next(w["work_id"] for w in works if w["title"].startswith("Aerodynamic"))
        hit = world.svc.library_search(world.pid, owner, "drag coefficient", work_id=drag)
        first = next(h for h in hit["hits"] if h["page"] == 2)
        pair = world.svc.library_dedup(world.pid, owner)["candidates"][0]["works"]
        cached = {
            "pair": {"work_a": pair[0]["work_id"], "work_b": pair[1]["work_id"]},
            "drag": drag,
            "where": {
                "work_id": drag,
                "page": 2,
                "start": first["start"],
                "end": first["end"],
            },
        }
        world.library = cached  # type: ignore[attr-defined]
    return cached if extra is None else extra


LABEL = (
    r"<(?:research root [^>]*|PrometheOS home|user home|temp|local path|project store)>"
    r"(?:[/\\][^\s\"']*)?"
)


def scrub(value: Any) -> Any:
    """Local paths in any form (an absolute path, a root label) are the transports' own concern
    (the hub drops path fields, MCP relabels them): compared as absent."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if (
                is_path_key(key)
                and isinstance(item, str)
                and (ABSOLUTE.match(item) or item[:1] == "<")
            ):
                continue
            if is_path_key(key) and isinstance(item, list):
                item = [
                    i
                    for i in item
                    if not (isinstance(i, str) and (ABSOLUTE.match(i) or i[:1] == "<"))
                ]
            out[key] = scrub(item)
        return out
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        if ABSOLUTE.match(value):
            return "<path>"
        # the Zotero stub of each world listens on a port of its own
        return re.sub(r"127\.0\.0\.1:[0-9]+", "127.0.0.1:<port>", re.sub(LABEL, "<path>", value))
    return value


def first_difference(a: Any, b: Any, path: str = "") -> str | None:
    if type(a) is not type(b):
        return f"{path}: {a!r:.120} != {b!r:.120}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}/{key}: only in {'first' if key in a else 'second'}"
            found = first_difference(a[key], b[key], f"{path}/{key}")
            if found:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: lengths {len(a)} != {len(b)}"
        for index, (x, y) in enumerate(zip(a, b, strict=True)):
            found = first_difference(x, y, f"{path}[{index}]")
            if found:
                return found
        return None
    return None if a == b else f"{path}: {a!r:.120} != {b!r:.120}"


def run_script(
    world: World, call: Callable[[str, dict[str, Any]], Any], route: str
) -> dict[str, Any]:
    memo: Memo = {}
    seen: dict[str, Any] = {}
    for step in STEPS:
        if route == "http" and not step.http:
            continue
        outcome = call(step.op, step.params(world, memo))
        if outcome["ok"] and step.capture is not None:
            step.capture(outcome["result"], memo)
        seen[step.label] = outcome
    return seen


@pytest.fixture
def synthetic_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMETHEOS_ENABLE_SYNTHETIC_ENGINE", "1")
    monkeypatch.setenv("PROMETHEOS_CAPTURE_BACKEND", "synthetic")


@pytest.fixture
def live_sessions_stay_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """The world's sessions are never captured, so nothing renews their heartbeat: after
    ``ABANDONED_AFTER`` the "live" one reads as abandoned and may be discarded. The MCP route
    (a server per call) reaches that step about 8 s after the heartbeat on an idle machine, so
    a slow one crossed the 20 s limit and the routes differed. Every route runs the script
    against a session that is live throughout."""
    from datetime import timedelta

    from prometheos.audio import recorder

    monkeypatch.setattr(recorder, "ABANDONED_AFTER", timedelta(days=1))


def comparable(world: World, outcome: dict[str, Any]) -> Any:
    if outcome["ok"]:
        return {"ok": True, "result": scrub(normalise(world, outcome["result"]))}
    return {
        "ok": False,
        "code": outcome["code"],
        "details": scrub(normalise(world, outcome["details"])),
    }


def test_every_step_is_equivalent_through_the_service_mcp_and_http(
    tmp_path: Path, synthetic_audio: None, live_sessions_stay_live: None
) -> None:
    worlds: dict[str, World] = {}
    seen: dict[str, dict[str, Any]] = {}
    for name in ("service", "mcp", "http"):
        world = worlds[name] = World(tmp_path / name)  # built just before its run
        with running_routes(world) as routes:
            seen[name] = {
                label: comparable(world, outcome)
                for label, outcome in run_script(world, getattr(routes, name), name).items()
            }
    reference = seen["service"]
    assert len(reference) == len(STEPS)
    # What each step is declared to produce on the reference route.
    for step in STEPS:
        outcome = reference[step.label]
        if step.expect == "ok":
            assert outcome["ok"], (step.label, outcome)
        else:
            assert not outcome["ok"] and outcome["code"] == step.expect, (step.label, outcome)
    # Every other route gives the same answer, step by step, denied requests included.
    for name in ("mcp", "http"):
        for label, expected in reference.items():
            if label in seen[name]:
                assert seen[name][label] == expected, (
                    f"{name} differs from the service at {label!r}: "
                    f"{first_difference(expected, seen[name][label])}"
                )
    # The scripted session covered the registry's CP10.6 family operations.
    covered = {s.op for s in STEPS}
    closed = {o.id for o in registry.operations() if o.id in agentops.OPS}
    uncovered = (
        closed
        - covered
        - {
            "source_receipt_record",
            "task_cancel",
            "workflow_render",
            "workflow_start",
            "workflow_step",
            "workflow_finish",
            "audio_span",
        }
    )
    assert not uncovered, f"operations without a step: {sorted(uncovered)}"


def test_protected_branches_have_equal_full_approval_required_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trust, model management and purge do not silently become agent-authorised writes."""
    import json
    from types import SimpleNamespace

    import anyio
    from cp1_support import AGENT, OWNER, Env, create_entry
    from cp4_support import import_package, write_package
    from hub_support import running_hub
    from mcp import Client
    from test_cp10_agentops import agent_request

    from prometheos.domain.models import PrometheosError
    from prometheos.mcp_adapter import build_server

    monkeypatch.setenv("PROMETHEOS_MODELS_DIR", str(tmp_path / "missing-models"))
    seen: dict[str, dict[str, Any]] = {}
    for transport in ("service", "mcp", "http"):
        (tmp_path / transport).mkdir()
        env = Env(tmp_path / transport)
        svc, pid = env.svc, env.project_id
        ref = "synthetic-evidence@1.0.0"
        import_package(svc, write_package(tmp_path / transport / "package"))
        svc.skill_revoke(ref)
        digest = svc.catalog.get(ref)["package_digest"]
        made = env.apply([create_entry(title="Purge candidate", text="synthetic only")])
        target = made["results"][0]["id"]
        purge = svc.purge_plan(pid, [target], OWNER)
        model = svc.audio_models_plan()
        branches = [
            ("skill_trust", {"ref": ref}, f"/skills/{ref}/trust", {"package_digest": digest}),
            (
                "audio_models_install",
                {"plan_sha256": model["plan_sha256"]},
                "/audio/models/install",
                {"plan_sha256": model["plan_sha256"]},
            ),
            ("audio_models_remove", {}, "/audio/models/remove", {}),
            (
                "retention_purge_commit",
                {"plan_id": purge["plan_id"]},
                f"/retention/purge/{purge['plan_id']}/commit",
                {"plan_sha256": purge["plan_sha256"]},
            ),
        ]
        normaliser = SimpleNamespace(
            names=lambda pid=pid, target=target: {pid: "project", target: "target"}
        )
        with running_hub(svc, pid) as hub:
            seen[transport] = {}
            for operation, params, route, http_body in branches:
                if transport == "service":
                    request_params = params | (
                        {"model": "faster-whisper-base"}
                        if operation.startswith("audio_models_")
                        else {}
                    )
                    try:
                        svc.approval_request(pid, AGENT, operation, request_params)
                        raise AssertionError("protected effect ran without owner")
                    except PrometheosError as exc:
                        response = exc.to_response().model_dump(mode="json")["error"]
                elif transport == "http":
                    reply = agent_request(hub, "POST", "/api/v1" + route, http_body)
                    assert reply.status == 403
                    response = reply.json()["error"]
                else:

                    async def invoke(svc=svc, pid=pid, operation=operation, params=params) -> Any:
                        async with Client(build_server(svc, pid)) as client:
                            return await client.call_tool(operation, params)

                    result = anyio.run(invoke)
                    assert result.is_error
                    text = result.content[0].text
                    response = json.loads(text[text.index("{") :])["error"]
                assert response["code"] == "approval_required"
                assert set(response["details"]) == {
                    "approval_request_id",
                    "plan_sha256",
                    "required_scope",
                    "owner_route",
                    "expires_at",
                }
                seen[transport][operation] = normalise(normaliser, response)
        assert svc.catalog.get(ref)["status"] == "revoked"
        assert svc.get(pid, OWNER, target, None)["body"]["text"] == "synthetic only"
    assert seen["service"] == seen["mcp"] == seen["http"]


def test_the_cli_agrees_with_the_agent_routes_on_the_pure_reads(
    tmp_path: Path, synthetic_audio: None
) -> None:
    world = World(tmp_path)
    cli = Cli(tmp_path)
    base = ("--project", world.pid)
    with running_routes(world) as routes:
        for args, op, params in [
            (("schema", "list"), None, None),
            (
                ("record", "history", world.paper_id),
                "record_history",
                {"record_id": world.paper_id},
            ),
            (
                ("version", "text", world.venue_version, "--limit", "80"),
                "version_text",
                {"version_id": world.venue_version, "limit": 80},
            ),
            (
                ("version", "content", world.manuscript_version, "--limit", "64"),
                "version_content",
                {"version_id": world.manuscript_version, "limit": 64},
            ),
            (("paper", "get", world.paper_id), "paper_get", {"paper_id": world.paper_id}),
            (("context", "next"), "context_next", {}),
            (
                ("context", "agenda", "--meeting", world.meeting),
                "context_agenda",
                {"meeting_id": world.meeting},
            ),
            (("job", "list"), "job_list", {}),
            (("skill", "catalog"), "skill_catalog", {}),
        ]:
            if op is None:
                continue
            scoped = () if args[:2] == ("skill", "catalog") else base  # the catalogue is per user
            code, out, err = cli(*args, *scoped)
            assert code == 0, (args, out, err)
            for route in ("mcp",):
                expected = getattr(routes, route)(op, params)
                assert expected["ok"], (op, expected)
                assert comparable(world, ok(out)) == comparable(world, expected), (args, route)


def test_unknown_and_malformed_requests_fail_alike(tmp_path: Path, synthetic_audio: None) -> None:
    """A request the schema refuses is the same ``validation_error`` on MCP and HTTP."""
    world = World(tmp_path)
    with running_routes(world) as routes:
        for op, params in [
            ("version_text", {"version_id": world.venue_version, "limit": 0}),
            ("job_set_enabled", {"job_id": "job_x", "base_rev": 1, "enabled": True}),
            ("skill_inspect", {"root_alias": "research"}),
            ("recording_control", {"session_id": world.session, "action": "explode"}),
            ("schema_show", {"name": "BadName"}),
        ]:
            a = routes.mcp(op, params)
            b = routes.http(op, params)
            assert a["ok"] is False and b["ok"] is False, op
            assert a["code"] == b["code"] == "validation_error", (op, a, b)
        # An argument the operation does not have is refused, not ignored.
        extra = routes.mcp("job_list", {"unexpected": 1})
        assert extra["ok"] is False and extra["code"] == "validation_error"
    assert failed("x")["ok"] is False


# ----- HTTP routes of operations whose MCP tool already existed ---------------------------------


def _mcp_tool(world: World, name: str, args: dict[str, Any]) -> dict[str, Any]:
    import json

    import anyio
    from mcp import Client

    from prometheos.mcp_adapter import build_server

    async def go() -> Any:
        async with Client(build_server(world.svc, world.pid)) as client:
            return await client.call_tool(name, args)

    result = anyio.run(go)
    if result.is_error:
        text = result.content[0].text
        error = json.loads(text[text.index("{") :])["error"]
        return failed(error["code"], error.get("details"))
    return ok(result.structured_content)


def _http(routes: Any, method: str, path: str, body: Any = None) -> dict[str, Any]:
    reply = routes.hub.request(
        method, path, body, cookie=None, csrf=None, origin=None, headers=routes.headers
    )
    payload = reply.json()
    if reply.status >= 400:
        return failed(payload["error"]["code"], payload["error"].get("details"))
    return ok(payload)


def test_http_routes_of_existing_mcp_tools_answer_like_the_tools(
    tmp_path: Path, synthetic_audio: None
) -> None:
    from cp10_continuity_support import receipt_payload

    rid, start_rid = new_request_id(), new_request_id()
    scripts = {
        "mcp": lambda w, r: {
            "receipt": _mcp_tool(
                w,
                "source_receipt_record",
                {"receipt": receipt_payload([]), "title": "Run 1", "request_id": rid},
            ),
            "receipt replay": _mcp_tool(
                w,
                "source_receipt_record",
                {"receipt": receipt_payload([]), "title": "Run 1", "request_id": rid},
            ),
            "cancel the task": _mcp_tool(
                w, "task_cancel", {"task_id": w.task, "request_id": new_request_id()}
            ),
            "cancel it again": _mcp_tool(
                w, "task_cancel", {"task_id": w.task, "request_id": new_request_id()}
            ),
            "render": _mcp_tool(
                w,
                "workflow_render",
                {
                    "package": "synthetic-evidence",
                    "recipe_id": "evidence-pass",
                    "inputs": {"question": "Why?"},
                },
            ),
            "render with bad inputs": _mcp_tool(
                w,
                "workflow_render",
                {"package": "synthetic-evidence", "recipe_id": "evidence-pass", "inputs": {}},
            ),
            "start": _mcp_tool(
                w,
                "workflow_start",
                {
                    "package_id": w.package_record,
                    "recipe_id": "evidence-pass",
                    "inputs": {"question": "Does it hold?"},
                    "request_id": start_rid,
                },
            ),
            "span": _mcp_tool(
                w, "audio_span", {"version_id": w.transcript, "start_ms": 4000, "end_ms": 6000}
            ),
            "span of an unknown version": _mcp_tool(
                w,
                "audio_span",
                {
                    "version_id": "version_00000000-0000-4000-8000-000000000009",
                    "start_ms": 0,
                    "end_ms": 10,
                },
            ),
        },
        "http": lambda w, r: {
            "receipt": _http(
                r,
                "POST",
                "/api/v1/sources/receipts",
                {"receipt": receipt_payload([]), "title": "Run 1", "request_id": rid},
            ),
            "receipt replay": _http(
                r,
                "POST",
                "/api/v1/sources/receipts",
                {"receipt": receipt_payload([]), "title": "Run 1", "request_id": rid},
            ),
            "cancel the task": _http(
                r, "POST", f"/api/v1/tasks/{w.task}/cancel", {"request_id": new_request_id()}
            ),
            "cancel it again": _http(
                r, "POST", f"/api/v1/tasks/{w.task}/cancel", {"request_id": new_request_id()}
            ),
            "render": _http(
                r,
                "POST",
                "/api/v1/workflows/render",
                {
                    "package": "synthetic-evidence",
                    "recipe_id": "evidence-pass",
                    "inputs": {"question": "Why?"},
                },
            ),
            "render with bad inputs": _http(
                r,
                "POST",
                "/api/v1/workflows/render",
                {"package": "synthetic-evidence", "recipe_id": "evidence-pass", "inputs": {}},
            ),
            "start": _http(
                r,
                "POST",
                "/api/v1/workflows",
                {
                    "package_id": w.package_record,
                    "recipe_id": "evidence-pass",
                    "inputs": {"question": "Does it hold?"},
                    "request_id": start_rid,
                },
            ),
            "span": _http(
                r,
                "GET",
                f"/api/v1/transcripts/{w.transcript}/span?start_ms=4000&end_ms=6000",
            ),
            "span of an unknown version": _http(
                r,
                "GET",
                "/api/v1/transcripts/version_00000000-0000-4000-8000-000000000009/span"
                "?start_ms=0&end_ms=10",
            ),
        },
    }
    seen: dict[str, dict[str, Any]] = {}
    for name, script in scripts.items():
        world = World(tmp_path / name)
        with running_routes(world) as routes:
            seen[name] = {k: comparable(world, v) for k, v in script(world, routes).items()}
    assert seen["mcp"]["receipt"]["ok"] and seen["mcp"]["receipt replay"]["ok"]
    assert seen["mcp"]["cancel the task"]["ok"] and not seen["mcp"]["cancel it again"]["ok"]
    assert seen["mcp"]["render"]["ok"] and seen["mcp"]["render with bad inputs"]["ok"] is False
    assert seen["mcp"]["start"]["ok"] and seen["mcp"]["span"]["ok"]
    assert seen["mcp"]["span of an unknown version"]["code"] == "not_found"
    for label, expected in seen["mcp"].items():
        assert seen["http"][label] == expected, (
            f"{label}: {first_difference(expected, seen['http'][label])}"
        )
