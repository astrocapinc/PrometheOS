"""CLI for the LaTeX workspace, the Tectonic engine and the Overleaf Git connector (CP9).

Serialization only, like :mod:`prometheos.cli`: every command calls the application
service with the caller's scope. Installing the engine and pushing to Overleaf are owner
actions: they need an interactive confirmation (or, for push, a capability file the
owner produced), so an agent running the CLI cannot do either on its own.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from prometheos import cli as _cli
from prometheos.app.reads import Caller
from prometheos.cli import (
    CapabilityOpt,
    JsonOpt,
    NoWaitOpt,
    ProjectOpt,
    ScopeOpt,
    _caller,
    _confirm_owner,
    _maybe_project,
    _mode,
    _owner_act,
    _read_secret,
    _request_approval,
    _run,
    _service,
    _worker_args,
    app,
)
from prometheos.domain.models import PrometheosError

tex_app = typer.Typer(
    help="The pinned Tectonic TeX engine (explicit install).", no_args_is_help=True
)
engine_app = typer.Typer(help="Engine status and explicit install.", no_args_is_help=True)
tex_app.add_typer(engine_app, name="engine")
app.add_typer(tex_app, name="tex")

workspace_app = typer.Typer(
    help="A paper's LaTeX workspace: bind, files, read, write, snapshot, structure, insert.",
    no_args_is_help=True,
)
overleaf_app = typer.Typer(
    help="Overleaf Git: link, status, pull with preview, commit, owner-approved push.",
    no_args_is_help=True,
)


def register(paper_app: typer.Typer) -> None:
    paper_app.add_typer(workspace_app, name="workspace")
    paper_app.add_typer(overleaf_app, name="overleaf")
    paper_app.command("compile")(paper_compile_cmd)
    paper_app.command("compile-status")(paper_compile_status_cmd)
    paper_app.command("synctex")(paper_synctex_cmd)


# ----- engine ----------------------------------------------------------------------------------


@engine_app.command("status")
def engine_status_cmd(as_json: bool = JsonOpt) -> None:
    """Installed engine, digest verification, cache size and the network disclosure."""
    svc = _service()
    _run(as_json, svc.tex_engine_status)


@engine_app.command("install")
def engine_install_cmd(
    digest: str = typer.Option(
        None, "--digest", help="Plan digest to install (omit to show the plan)."
    ),
    archive: Path = typer.Option(
        None, "--archive", help="Install from an already downloaded release zip (verified)."
    ),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Show the install plan (size, licence, destination, network use); with --digest and
    the owner's interactive confirmation, download, verify and install it. Without an
    interactive terminal, --digest becomes an approval request in the project
    (approval_required) that the owner grants; `approval resume` then installs."""
    svc = _service()

    def action() -> Any:
        plan = svc.tex_engine_plan()
        if digest is None:
            return {"plan": plan, "installed": False}
        # Looked up on prometheos.cli when called, as _confirm_owner does: one notion of an
        # interactive owner for both the approval route and the confirmation it replaces.
        if not _cli._interactive():
            pid = svc.resolve_project_id(project)
            _request_approval(svc, pid, Caller("cli"), "tex_engine_install", {"digest": digest})
        if digest != plan["plan_digest"]:
            raise PrometheosError(
                "revision_conflict", "the install plan changed; review it again", {"plan": plan}
            )
        _confirm_owner(
            f"Install Tectonic {plan['version']} ({plan['license']}), "
            f"{plan['download_bytes']} bytes from {plan['download_url']}\n"
            f"SHA-256 {plan['sha256']}\ninto {plan['destination']}\n{plan['network']}",
            purpose="To download and install exactly this",
        )
        status = svc.tex_engine_install(
            digest, archive, authority=_owner_act("cli-owner-engine"),
            project_id=_maybe_project(svc, project),
            # The owner's install is recorded at owner rank: an agent can neither read its local
            # archive path nor resume it (decision 0017 round 2, F3).
            caller=Caller("cli", "owner"),
        )  # fmt: skip
        run_id = status.pop("run_id", None)
        return {"plan": plan, "installed": True, "status": status, "run_id": run_id}

    _run(as_json, action)


# ----- workspace -------------------------------------------------------------------------------


@workspace_app.command("bind")
def workspace_bind_cmd(
    paper_id: str,
    root: str = typer.Option(..., "--root", help="Approved research root alias."),
    main: str = typer.Option(..., "--main", help="Root-relative path of the main .tex file."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Bind the paper to the folder holding its main .tex file (a paper revise)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.workspace_bind(
            pid, _caller(svc, pid, scope, capability_file), paper_id, root, main
        )

    _run(as_json, action)


@workspace_app.command("files")
def workspace_files_cmd(
    paper_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Bounded file listing with digests and version state (pure)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.workspace_files(pid, _caller(svc, pid, scope, capability_file), paper_id)

    _run(as_json, action)


@workspace_app.command("read")
def workspace_read_cmd(
    paper_id: str,
    path: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """A workspace file's text and digest (the digest a later write must name)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.workspace_read(pid, _caller(svc, pid, scope, capability_file), paper_id, path)

    _run(as_json, action)


@workspace_app.command("write")
def workspace_write_cmd(
    paper_id: str,
    path: str,
    from_file: Path = typer.Option(..., "--from-file", help="UTF-8 file with the new content."),
    base: str = typer.Option(None, "--base", help="SHA-256 of the content you loaded."),
    create: bool = typer.Option(False, "--create", help="Create a new file (no --base)."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Atomically replace a file only if it still has the --base digest."""
    svc = _service()

    def action() -> Any:
        if (base is None) != create:
            raise PrometheosError(
                "validation_error", "give --base for an existing file or --create for a new one", {}
            )
        pid = svc.resolve_project_id(project)
        text = from_file.read_bytes().decode("utf-8")
        return svc.workspace_write(
            pid, _caller(svc, pid, scope, capability_file), paper_id, path, text, base
        )

    _run(as_json, action)


@workspace_app.command("snapshot")
def workspace_snapshot_cmd(
    paper_id: str,
    path: list[str] = typer.Option(None, "--path", help="Only these files (repeat)."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record a version of every changed workspace file (anchor remap rules apply)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.workspace_snapshot(
            pid, _caller(svc, pid, scope, capability_file), paper_id, list(path) if path else None
        )

    _run(as_json, action)


@workspace_app.command("structure")
def workspace_structure_cmd(
    paper_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Labels, references, citation and BibTeX keys, outline and anchors (pure)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.workspace_structure(pid, _caller(svc, pid, scope, capability_file), paper_id)

    _run(as_json, action)


@workspace_app.command("insert")
def workspace_insert_cmd(
    paper_id: str,
    version_id: str,
    figure: bool = typer.Option(False, "--figure", help="Insert a figure instead of a citation."),
    bib: str = typer.Option(None, "--bib", help="Workspace .bib file for citations."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Write the citation entry or figure file for a recorded version; print the snippet."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.workspace_insert(
            pid,
            _caller(svc, pid, scope, capability_file),
            paper_id,
            "figure" if figure else "cite",
            version_id,
            bib,
        )

    _run(as_json, action)


# ----- compile ---------------------------------------------------------------------------------


def paper_compile_cmd(
    paper_id: str,
    fetch: bool = typer.Option(
        False, "--fetch", help="Allow Tectonic's bundle fetch for packages not yet cached."
    ),
    no_wait: bool = NoWaitOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Compile a staged copy of the workspace with the pinned Tectonic (offline by default).
    An operation run: its run_id is in the answer; --no-wait answers the run at once."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        svc.worker_args = _worker_args(scope, capability_file)
        return svc.paper_compile(
            pid, _caller(svc, pid, scope, capability_file), paper_id, fetch=fetch,
            mode=_mode(no_wait),
        )  # fmt: skip

    _run(as_json, action)


def paper_compile_status_cmd(
    paper_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Last compile, its diagnostics and the PDF on show with its own inputs (pure)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.paper_compile_status(pid, _caller(svc, pid, scope, capability_file), paper_id)

    _run(as_json, action)


def paper_synctex_cmd(
    paper_id: str,
    file: str = typer.Option(None, "--file", help="Workspace file for source-to-PDF."),
    line: int = typer.Option(None, "--line"),
    page: int = typer.Option(None, "--page", help="PDF page for PDF-to-source."),
    x: float = typer.Option(None, "--x", help="Points from the left edge."),
    y: float = typer.Option(None, "--y", help="Points from the top edge."),
    pdf_version: str = typer.Option(None, "--pdf-version"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """SyncTeX: --file/--line gives a PDF position; --page/--x/--y gives a source line."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        if file is not None and line is not None:
            return svc.synctex_forward(pid, caller, paper_id, file, line, pdf_version)
        if page is not None and x is not None and y is not None:
            return svc.synctex_inverse(pid, caller, paper_id, page, x, y, pdf_version)
        raise PrometheosError("validation_error", "give --file and --line, or --page --x --y", {})

    _run(as_json, action)


# ----- Overleaf --------------------------------------------------------------------------------


@overleaf_app.command("link")
def overleaf_link_cmd(
    paper_id: str,
    url: str = typer.Option(..., "--url", help="https://git.overleaf.com/<id> (no token)."),
    root: str = typer.Option(..., "--root", help="Approved research root alias."),
    folder: str = typer.Option(..., "--folder", help="New root-relative folder for the clone."),
    main: str = typer.Option("main.tex", "--main", help="Main file inside the clone."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Clone the Overleaf project with the system git and bind the paper's workspace."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.overleaf_link(
            pid, _caller(svc, pid, scope, capability_file), paper_id, url, root, folder, main
        )

    _run(as_json, action)


@overleaf_app.command("status")
def overleaf_status_cmd(
    paper_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Branch, ahead/behind as of the last fetch, local changes and refused settings."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.overleaf_status(pid, _caller(svc, pid, scope, capability_file), paper_id)

    _run(as_json, action)


@overleaf_app.command("pull")
def overleaf_pull_cmd(
    paper_id: str,
    digest: str = typer.Option(None, "--digest", help="Apply the previewed plan with this digest."),
    no_fetch: bool = typer.Option(False, "--no-fetch", help="Preview without fetching."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Without --digest: fetch and preview. With --digest: integrate exactly that plan."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        if digest is None:
            return svc.overleaf_pull_preview(pid, caller, paper_id, fetch=not no_fetch)
        return svc.overleaf_pull_apply(pid, caller, paper_id, digest)

    _run(as_json, action)


@overleaf_app.command("commit")
def overleaf_commit_cmd(
    paper_id: str,
    message: str = typer.Option(..., "--message", "-m"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Commit the workspace's local changes in the clone (local only; nothing is sent)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.overleaf_commit(
            pid, _caller(svc, pid, scope, capability_file), paper_id, message
        )

    _run(as_json, action)


@overleaf_app.command("push")
def overleaf_push_cmd(
    paper_id: str,
    digest: str = typer.Option(None, "--digest", help="Push exactly the plan with this digest."),
    push_capability_file: Path = typer.Option(
        None, "--push-capability-file", help="File holding the owner's overleaf_push capability."
    ),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Without --digest: fetch and show the push plan. With --digest: push exactly that plan,
    after the owner's interactive confirmation (or with an overleaf_push capability file).
    Never forces."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        if digest is None:
            return svc.overleaf_push_preview(pid, caller, paper_id)
        secret = _read_secret(push_capability_file, env="PROMETHEOS_PUSH_CAPABILITY")
        if secret is None and not _cli._interactive():
            params = {"paper_id": paper_id, "digest": digest}
            _request_approval(svc, pid, caller, "overleaf_push", params)
        if secret is None:
            preview = svc.overleaf_push_preview(pid, caller, paper_id)
            plan = preview["plan"]
            commits = "\n".join(f"  {c['oid'][:10]} {c['subject']}" for c in plan["commits"])
            # The destination is the URL bound into the digest and named by the push itself
            # (integration review INT2-F3: the owner sees where the commits go). The prompt
            # escapes invisible characters in every line: the destination, the branch and
            # the commit subjects, which agents can write (pre-check rounds 2-3).
            _confirm_owner(
                f"Push to Overleaf branch {plan['branch']} (no force)\n"
                f"Destination: {plan['remote_url']}\nCommits:\n{commits}\n"
                f"Plan digest: {preview['digest']}",
                purpose="To push exactly these commits",
            )
            if preview["digest"] != digest:
                raise PrometheosError(
                    "revision_conflict", "the push plan changed; review it again", {}
                )
            secret = svc.overleaf_push_grant(pid, digest, "cli owner push").secret
            sys.stderr.write("Owner approved; pushing.\n")
        return svc.overleaf_push(pid, caller, paper_id, digest, secret)

    _run(as_json, action)
