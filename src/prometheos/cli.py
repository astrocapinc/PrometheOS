"""Command-line interface. Serialization only; all behavior lives in app services.

With ``--json`` stdout carries exactly one JSON document (success payload or
error envelope); diagnostics go to stderr. Nothing prompts; ``--help`` needs no
project, credentials or server.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import typer

from prometheos import __version__
from prometheos.app.reads import Caller
from prometheos.domain.canonical import JSONDepthError, dumps_canonical, loads_strict
from prometheos.domain.ids import is_request_id, new_request_id
from prometheos.domain.models import EXIT_CODES, PrometheosError, depth_error
from prometheos.domain.text import json_for_terminal, visible
from prometheos.schemas import REGISTRY, schema_text

app = typer.Typer(
    name="prometheos",
    help=(
        "PrometheOS research record for coding agents. Agents execute; PrometheOS supplies "
        "context, records, evidence and decisions."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
schema_app = typer.Typer(help="Inspect exported public JSON Schemas.", no_args_is_help=True)
project_app = typer.Typer(help="Create and inspect projects.", no_args_is_help=True)
record_app = typer.Typer(help="Read records (pure queries).", no_args_is_help=True)
source_app = typer.Typer(help="Import and check research sources.", no_args_is_help=True)
backup_app = typer.Typer(help="Verified backup and staged restore.", no_args_is_help=True)
maint_app = typer.Typer(help="Store maintenance.", no_args_is_help=True)
version_app = typer.Typer(help="Retained version text and bytes (paged).", no_args_is_help=True)
for sub, name in (
    (schema_app, "schema"),
    (project_app, "project"),
    (record_app, "record"),
    (version_app, "version"),
    (source_app, "source"),
    (backup_app, "backup"),
    (maint_app, "maintenance"),
):
    app.add_typer(sub, name=name)

ProjectOpt = typer.Option(None, "--project", help="Project ID (else PROMETHEOS_PROJECT/link file).")
JsonOpt = typer.Option(False, "--json", help="Emit exactly one JSON document on stdout.")
ScopeOpt = typer.Option(
    "agent",
    "--scope",
    help=(
        "'agent' (default) hides restricted/private records; 'owner' shows all and needs "
        "--capability-file holding a read_sensitive capability."
    ),
)
CapabilityOpt = typer.Option(
    None,
    "--capability-file",
    help="File holding an owner capability secret (transport metadata; never logged).",
)
DecisionOpt = typer.Option(
    None,
    "--decision-file",
    help="File holding an owner decision for this exact request (protected effects).",
)


def _service() -> Any:
    from prometheos.app.service import Service

    svc = Service(actor="cli")
    svc.on_run_created = _announce_run
    return svc


def _announce_run(run_id: str, operation: str) -> None:
    """Long operations name their run at once (stderr): if this command is interrupted, the
    run resumes with `prometheos run resume RUN_ID` (decision 0017)."""
    sys.stderr.write(f"{operation}: operation run {run_id}\n")


def _emit(payload: Any, as_json: bool) -> None:
    # Invisible characters inside strings are written as JSON escapes: the same document
    # for a reader, and nothing that reorders or hides text in a terminal (R2-L1).
    text = json.dumps(payload, indent=None if as_json else 2, ensure_ascii=False)
    sys.stdout.write(json_for_terminal(text) + "\n")


def _fail(exc: PrometheosError, as_json: bool) -> NoReturn:
    response = exc.to_response().model_dump(mode="json")
    if as_json:
        sys.stdout.write(json_for_terminal(json.dumps(response, ensure_ascii=False)) + "\n")
    else:
        sys.stderr.write(f"error[{exc.code}]: {visible(exc.message)}\n")
        if exc.details:
            sys.stderr.write(json_for_terminal(json.dumps(exc.details, ensure_ascii=False)) + "\n")
    raise typer.Exit(exc.exit_code)


def _run(as_json: bool, action: Callable[[], Any]) -> None:
    try:
        result = action()
    except PrometheosError as exc:
        _fail(exc, as_json)
    except JSONDepthError as exc:
        _fail(depth_error(exc), as_json)
    except sqlite3.DatabaseError as exc:
        # CP10.5: a store that fails while it is read points to the maintenance service.
        from prometheos.app.maintenance import database_error

        _fail(database_error(os.environ.get("PROMETHEOS_PROJECT"), exc), as_json)
    except OSError as exc:
        _fail(PrometheosError("io_error", f"file system error: {exc.strerror or exc}", {}), as_json)
    except Exception as exc:  # last resort: keep the one-document contract, show the defect
        traceback.print_exc(file=sys.stderr)
        _fail(
            PrometheosError(
                "io_error", f"internal error ({type(exc).__name__}); details on stderr", {}
            ),
            as_json,
        )
    _emit(result, as_json)


def _pairs(values: list[str] | None, label: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values or []:
        alias, sep, path = value.partition("=")
        if not sep or not alias or not path:
            raise PrometheosError("validation_error", f"{label} must be ALIAS=PATH", {})
        out[alias] = path
    return out


def _read_secret(path: Path | None, env: str = "PROMETHEOS_CAPABILITY") -> str | None:
    if path is None:
        value = os.environ.get(env)
        return value.strip() if value else None
    text = path.read_text(encoding="utf-8").strip()
    return text.splitlines()[0].strip() if text else None


NoWaitOpt = typer.Option(
    False,
    "--no-wait",
    help="Answer with the operation run at once; a detached worker executes it "
    "(see `prometheos run get`).",
)


def _mode(no_wait: bool) -> str:
    """CLI long operations wait by default (the result, with its run_id); --no-wait answers
    the run view at once and a detached `prometheos run resume` worker executes it."""
    return "detached" if no_wait else "wait"


def _worker_args(scope: str, capability_file: Path | None) -> list[str]:
    if scope != "owner":
        return []
    return ["--scope", "owner"] + (
        ["--capability-file", str(capability_file)] if capability_file else []
    )


def _maybe_project(svc: Any, project: str | None) -> str | None:
    """A home-level install records its run in the project the caller works in, if any."""
    try:
        return str(svc.resolve_project_id(project))
    except PrometheosError:
        if project is not None:
            raise
        return None


def _caller(svc: Any, project_id: str, scope: str, capability_file: Path | None) -> Caller:
    """Agent scope by default; owner scope only by presenting a read_sensitive capability."""
    if scope == "agent":
        return Caller(actor="cli")
    if scope != "owner":
        raise PrometheosError("validation_error", "scope must be agent or owner", {})
    caller: Caller = svc.owner_caller(project_id, _read_secret(capability_file), "read_sensitive")
    return caller


def _owner_act(actor: str) -> Any:
    """The in-process proof of an interactive owner confirmation (only after one succeeded)."""
    from prometheos.app.authority import OwnerAct

    return OwnerAct(channel="cli-interactive", actor=actor)


def _request_approval(
    svc: Any,
    pid: str,
    caller: Caller,
    operation: str,
    params: dict[str, Any],
    request_id: str | None = None,
) -> NoReturn:
    """A non-interactive caller (an agent) asks for the owner's approval: the exact plan is
    stored and ``approval_required`` (exit 4) names the request, digest and owner route."""
    svc.approval_request(pid, caller, operation, params, request_id)
    raise AssertionError("approval_request always raises")  # pragma: no cover


def _write_agent_token(path: Path, gate: Any) -> None:
    """Review F15 / R2-6: the token file is created exclusively and restricted to the current
    user while still empty; only then is a token minted and written. A file that cannot be
    restricted is removed and the hub refuses to start."""
    from prometheos.store.private_files import write_private

    try:
        write_private(path, b"")
    except FileExistsError as exc:
        raise PrometheosError(
            "io_error", "cannot create the agent token file: it already exists", {}
        ) from exc
    with path.open("r+", encoding="utf-8") as handle:
        handle.write(gate.new_agent_token() + "\n")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"prometheos {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    """PrometheOS command-line interface."""


# ----- capability discovery (A02, CP10.1) ----------------------------------------------


@app.command("capabilities")
def capabilities_cmd(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Pure capability discovery: per feature, version, availability, required authority,
    local or remote execution, cost class and current blocker. Never a secret value."""
    svc = _service()

    def action() -> Any:
        try:
            pid = svc.resolve_project_id(project)
        except PrometheosError:
            if project is not None:
                raise
            pid = None  # discovery works without a project; provider reports not_configured
        return svc.capabilities(pid)

    _run(as_json, action)


# ----- schema ------------------------------------------------------------------------


@schema_app.command("list")
def schema_list() -> None:
    """List exported schema names."""
    for name in sorted(REGISTRY):
        typer.echo(name)


@schema_app.command("show")
def schema_show(name: str) -> None:
    """Print one JSON Schema."""
    if name not in REGISTRY:
        typer.echo(f"unknown schema: {name}", err=True)
        raise typer.Exit(2)
    sys.stdout.write(schema_text(name))


# ----- project -----------------------------------------------------------------------


@project_app.command("init")
def project_init(
    title: str = typer.Option(..., "--title"),
    objective: str = typer.Option(..., "--objective", help="Current objective; may say undecided."),
    timezone: str = typer.Option(None, "--timezone", help="Owner-supplied IANA timezone."),
    root_dirs: list[str] = typer.Option(None, "--root", help="Approved research root ALIAS=PATH."),
    store: Path = typer.Option(None, "--store", help="Explicit local store directory."),
    declare_local: bool = typer.Option(False, "--declare-local"),
    link: Path = typer.Option(None, "--link", help="Write .prometheos-link.json into this folder."),
    as_json: bool = JsonOpt,
) -> None:
    """Create one project store and register it."""

    def action() -> Any:
        result = _service().init_project(
            title=title,
            objective=objective,
            timezone=timezone,
            roots=_pairs(root_dirs, "--root"),
            store=store,
            declare_local=declare_local,
        )
        if link is not None:
            target = link / ".prometheos-link.json"
            if target.exists():
                raise PrometheosError("validation_error", "link file already exists", {})
            target.write_text(json.dumps({"project_id": result["project_id"]}) + "\n", "utf-8")
            result["link_file"] = str(target)
        return result

    _run(as_json, action)


root_app = typer.Typer(
    help="Approved research folders: list them, or add one (an owner decision). A folder "
    "cannot be removed here.",
    no_args_is_help=True,
)
project_app.add_typer(root_app, name="root")


@root_app.command("list")
def project_root_list_cmd(
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """The approved research folders and whether each is there now (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.project_roots(pid, _caller(svc, pid, scope, capability_file))

    _run(as_json, action)


@root_app.command("add")
def project_root_add_cmd(
    alias: str = typer.Argument(..., help="Short name (a-z, 0-9, '_' or '-')."),
    path: Path = typer.Argument(..., help="An existing local folder (absolute path)."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: add an approved research folder (it widens what PrometheOS may read).
    Without an interactive terminal it becomes an approval request (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        target = str(path.absolute())
        if not _interactive():
            _request_approval(
                svc, pid, Caller("cli"), "project_root_add", {"alias": alias, "path": target},
                request_id,
            )  # fmt: skip
        plan = svc.project_root_plan(pid, alias, target)
        _confirm_owner(f"Add the research folder {plan['alias']}: {plan['path']}")
        return svc.project_root_add(
            pid, alias, target, authority=_owner_act("cli-owner-roots"), request_id=request_id
        )

    _run(as_json, action)


@project_app.command("info")
def project_info(
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Show project settings and counts (pure read)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.project_info(pid, _caller(svc, pid, scope, capability_file))

    _run(as_json, action)


@project_app.command("list")
def project_list(as_json: bool = JsonOpt) -> None:
    """List registered projects (pure read)."""
    _run(as_json, lambda: {"projects": _service().list_projects()})


# ----- apply and records ----------------------------------------------------------------


@app.command("apply")
def apply_cmd(
    file: Path = typer.Option(..., "--file", help="Request JSON file, or - for stdin."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    decision_file: Path = DecisionOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Apply one typed transaction (request schema v1). All or nothing."""
    svc = _service()

    def action() -> Any:
        if str(file) != "-" and not file.is_file():
            # Name the file the caller gave and say a retry will not help (P28).
            what = (
                "request file is not a regular file" if file.exists() else "request file not found"
            )
            raise PrometheosError("validation_error", what, {"file": str(file)})
        raw = sys.stdin.buffer.read() if str(file) == "-" else file.read_bytes()
        target = project
        if target is None:
            try:
                peek = loads_strict(raw)
                target = peek.get("project_id") if isinstance(peek, dict) else None
            except ValueError:
                target = None
        pid = svc.resolve_project_id(target)
        decision = _read_secret(decision_file, "PROMETHEOS_DECISION")
        return svc.apply(pid, raw, _caller(svc, pid, scope, capability_file), decision)

    _run(as_json, action)


@record_app.command("get")
def record_get(
    record_id: str,
    project: str = ProjectOpt,
    at_seq: int = typer.Option(None, "--at-seq", help="As known at this project sequence."),
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Get one record envelope."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.get(pid, _caller(svc, pid, scope, capability_file), record_id, at_seq)

    _run(as_json, action)


@record_app.command("list")
def record_list(
    project: str = ProjectOpt,
    kind: str = typer.Option(None, "--kind"),
    limit: int = typer.Option(50, "--limit"),
    cursor: str = typer.Option(None, "--cursor"),
    at_seq: int = typer.Option(None, "--at-seq"),
    include_archived: bool = typer.Option(False, "--include-archived"),
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """List records with an opaque snapshot-bound cursor."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.list_records(
            pid,
            _caller(svc, pid, scope, capability_file),
            kind=kind,
            limit=limit,
            cursor=cursor,
            at_seq=at_seq,
            include_archived=include_archived,
        )

    _run(as_json, action)


# ----- retained version text and bytes (A02, CP10.6) ---------------------------------------


@version_app.command("text")
def version_text_cmd(
    version_id: str,
    offset: int = typer.Option(0, "--offset", min=0, help="First character of the page."),
    limit: int = typer.Option(50_000, "--limit", min=1, max=100_000, help="Characters per page."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Retained UTF-8 text of a version, one page (next_offset says where the next starts)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.version_text(pid, caller, version_id, offset, limit)

    _run(as_json, action)


@version_app.command("content")
def version_content_cmd(
    version_id: str,
    offset: int = typer.Option(0, "--offset", min=0, help="First byte of the page."),
    limit: int = typer.Option(65_536, "--limit", min=1, max=262_144, help="Bytes per page."),
    out: Path = typer.Option(None, "--out", help="Write all the verified bytes to a new file."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Retained bytes of a viewable version (PDF, image, audio, text): one base64 page, or with
    --out all the verified bytes in one new file (never overwritten)."""
    svc = _service()

    def action() -> Any:
        from prometheos.agentops.records import version_content_page

        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        if out is None:
            return version_content_page(svc, pid, caller, version_id, offset, limit)
        if out.exists():
            raise PrometheosError("validation_error", "output file already exists", {})
        data, media_type = svc.version_content(pid, caller, version_id)
        with open(out, "xb") as handle:
            handle.write(data)
        return {
            "written": str(out),
            "media_type": media_type,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    _run(as_json, action)


# ----- sources -----------------------------------------------------------------------


@source_app.command("import")
def source_import(
    paths: list[str] = typer.Argument(None, help="Files under the root (relative or absolute)."),
    plan: bool = typer.Option(False, "--plan", help="Create a persisted import plan."),
    commit: str = typer.Option(None, "--commit", help="Commit this plan ID."),
    root_alias: str = typer.Option(None, "--root", help="Approved research root alias."),
    retention: str = typer.Option(None, "--retention", help="copy or reference."),
    sensitivity: str = typer.Option("normal", "--sensitivity"),
    description: str = typer.Option("", "--description"),
    no_wait: bool = NoWaitOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Plan an explicit import, then commit it after reviewing the preview. Each step is an
    operation run (its run_id is in the answer; --no-wait answers the run at once).

    Agent scope by default; a plan made at owner scope commits (and replays) only at owner scope."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        if plan == bool(commit):
            raise PrometheosError("validation_error", "use exactly one of --plan or --commit", {})
        caller = _caller(svc, pid, scope, capability_file)
        svc.worker_args = _worker_args(scope, capability_file)
        if commit:
            return svc.import_commit(pid, commit, caller, mode=_mode(no_wait))
        if not root_alias or not paths:
            raise PrometheosError("validation_error", "--plan needs --root and file paths", {})
        return svc.import_plan(
            pid,
            caller,
            mode=_mode(no_wait),
            root_alias=root_alias,
            paths=list(paths),
            retention=retention,
            sensitivity=sensitivity,
            description=description,
        )

    _run(as_json, action)


@source_app.command("receipt")
def source_receipt(
    file: Path = typer.Option(..., "--file", help="execution_receipt_v1 JSON, or - for stdin."),
    title: str = typer.Option(..., "--title", help="Short name, e.g. the script or run."),
    request_id: str = typer.Option(..., "--request-id", help="UUID4; reuse it to retry safely."),
    description: str = typer.Option("", "--description"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record what an agent reports it ran; outputs must name stored versions by digest."""
    svc = _service()

    def action() -> Any:
        if not is_request_id(request_id):
            raise PrometheosError("validation_error", "request id must be a UUID4", {})
        raw = sys.stdin.buffer.read() if str(file) == "-" else file.read_bytes()
        pid = svc.resolve_project_id(project)
        return svc.receipt_record(
            pid,
            raw=raw,
            title=title,
            description=description,
            request_id=request_id,
            caller=_caller(svc, pid, scope, capability_file),
        )

    _run(as_json, action)


@source_app.command("receipt-paths")
def source_receipt_paths(
    file: Path = typer.Option(
        ..., "--file", help="JSON with run, inputs and outputs, or - for stdin."
    ),
    title: str = typer.Option(..., "--title", help="Short name, e.g. the script or run."),
    request_id: str = typer.Option(..., "--request-id", help="UUID4; reuse it to retry safely."),
    description: str = typer.Option("", "--description"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record what ran, naming its files by research-root path ({root_alias, path, role}); each
    is pinned to its recorded version. Files not recorded or changed since are listed."""
    svc = _service()

    def action() -> Any:
        if not is_request_id(request_id):
            raise PrometheosError("validation_error", "request id must be a UUID4", {})
        raw = sys.stdin.buffer.read() if str(file) == "-" else file.read_bytes()
        spec = loads_strict(raw)
        if not isinstance(spec, dict):
            raise PrometheosError("validation_error", "the file must hold a JSON object", {})
        # The same parameter model as MCP and HTTP (review frame-rerun-a F1).
        from prometheos import agentops

        agentops.load()
        p = agentops.parse(
            agentops.OPS["source_receipt_from_paths"],
            {**spec, "title": title, "request_id": request_id, "description": description},
        )
        pid = svc.resolve_project_id(project)
        return svc.receipt_from_paths(
            pid,
            run=dict(p.run),  # type: ignore[attr-defined]
            inputs=[a.model_dump() for a in p.inputs],  # type: ignore[attr-defined]
            outputs=[a.model_dump() for a in p.outputs],  # type: ignore[attr-defined]
            title=title,
            description=description,
            request_id=request_id,
            caller=_caller(svc, pid, scope, capability_file),
        )

    _run(as_json, action)


@source_app.command("check")
def source_check(
    ids: list[str] = typer.Argument(..., help="Source or version IDs."),
    save: bool = typer.Option(False, "--save", help="Record the observation (a transaction)."),
    request_id: str = typer.Option(None, "--request-id", help="Make a --save retry replay."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Observe retained and current bytes. Read-only unless --save."""
    svc = _service()

    def action() -> Any:
        if request_id is not None and not is_request_id(request_id):
            raise PrometheosError("validation_error", "request id must be a UUID4", {})
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.source_check(pid, list(ids), save, caller, request_id)

    _run(as_json, action)


# ----- CP10.4: research continuity (A02; decision 0018) ----------------------------------

execution_app = typer.Typer(
    help=(
        "External executions, observe-only: register a run you own, report its status, see "
        "what awaits interpretation. PrometheOS never starts, stops or restarts a run."
    ),
    no_args_is_help=True,
)
app.add_typer(execution_app, name="execution")
artifact_app = typer.Typer(
    help="Availability and backup coverage of source versions (pure reads).", no_args_is_help=True
)
app.add_typer(artifact_app, name="artifact")


@execution_app.command("register")
def execution_register_cmd(
    title: str = typer.Option(..., "--title", help="Short name of the run."),
    host: str = typer.Option(..., "--host", help="Where it runs (host label)."),
    reporter: str = typer.Option(..., "--reporter", help="Who reports on it (agent or tool)."),
    command_summary: str = typer.Option(None, "--command-summary"),
    root: str = typer.Option(None, "--root", help="Approved research root alias it works in."),
    external_ref: str = typer.Option(None, "--external-ref", help="Job label, never a secret."),
    expected_output: list[str] = typer.Option(None, "--expected-output"),
    sensitivity: str = typer.Option("normal", "--sensitivity"),
    request_id: str = typer.Option(None, "--request-id", help="UUID4; reuse it to retry safely."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Register an external run (state `registered`). Nothing is started."""
    svc = _service()

    def action() -> Any:
        if request_id is not None and not is_request_id(request_id):
            raise PrometheosError("validation_error", "request id must be a UUID4", {})
        pid = svc.resolve_project_id(project)
        return svc.execution_register(
            pid,
            _caller(svc, pid, scope, capability_file),
            title=title,
            host_label=host,
            reporter=reporter,
            command_summary=command_summary,
            working_root_alias=root,
            external_ref=external_ref,
            expected_outputs=list(expected_output or []),
            sensitivity=sensitivity,
            request_id=request_id,
        )

    _run(as_json, action)


@execution_app.command("report")
def execution_report_cmd(
    execution_id: str,
    state: str = typer.Option(
        ...,
        "--state",
        help="reported_running, reported_completed, reported_failed, reported_cancelled or "
        "status_unknown.",
    ),
    reported_at: str = typer.Option(None, "--reported-at", help="UTC timestamp (default now)."),
    receipt: list[str] = typer.Option(None, "--receipt", help="Receipt version ID to attach."),
    request_id: str = typer.Option(None, "--request-id", help="UUID4; reuse it to retry safely."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Report a run's status (what you say, never an observation) and attach receipts."""
    svc = _service()

    def action() -> Any:
        if request_id is not None and not is_request_id(request_id):
            raise PrometheosError("validation_error", "request id must be a UUID4", {})
        pid = svc.resolve_project_id(project)
        return svc.execution_report(
            pid,
            _caller(svc, pid, scope, capability_file),
            execution_id,
            state=state,
            reported_at=reported_at,
            receipt_version_ids=list(receipt or []),
            request_id=request_id,
        )

    _run(as_json, action)


@execution_app.command("list")
def execution_list_cmd(
    state: str = typer.Option(None, "--state", help="Filter on the effective state."),
    awaiting: bool = typer.Option(None, "--awaiting/--not-awaiting"),
    limit: int = typer.Option(100, "--limit"),
    silence_after_hours: int = typer.Option(24, "--silence-after-hours"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Executions with reported and effective state (silence reads as status_unknown)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.execution_list(
            pid,
            _caller(svc, pid, scope, capability_file),
            state=state,
            awaiting=awaiting,
            limit=limit,
            silence_after_hours=silence_after_hours,
        )

    _run(as_json, action)


@execution_app.command("awaiting")
def execution_awaiting_cmd(
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Completed executions and bare receipts no assessment or reviewed entry cites yet."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.awaiting_list(pid, _caller(svc, pid, scope, capability_file))

    _run(as_json, action)


@execution_app.command("resolve")
def execution_resolve_cmd(
    target_id: str,
    resolution: str = typer.Option(..., "--resolution", help="interpreted or dismissed."),
    text: str = typer.Option(..., "--text", help="The interpretation, or the dismissal reason."),
    request_id: str = typer.Option(None, "--request-id", help="UUID4; reuse it to retry safely."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    decision_file: Path = DecisionOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Take an item out of the queue: a reviewed entry that gives the interpretation or the
    dismissal reason and cites it. Protected: without an owner decision the answer is
    approval_required."""
    svc = _service()

    def action() -> Any:
        if request_id is not None and not is_request_id(request_id):
            raise PrometheosError("validation_error", "request id must be a UUID4", {})
        pid = svc.resolve_project_id(project)
        return svc.awaiting_resolve(
            pid,
            _caller(svc, pid, scope, capability_file),
            target_id,
            resolution,
            text,
            request_id or new_request_id(),
            _read_secret(decision_file, "PROMETHEOS_DECISION"),
        )

    _run(as_json, action)


@artifact_app.command("coverage")
def artifact_coverage_cmd(
    source: str = typer.Option(None, "--source", help="Only this source's versions."),
    limit: int = typer.Option(100, "--limit"),
    cursor: str = typer.Option(None, "--cursor"),
    newest_first: bool = typer.Option(False, "--newest-first"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Availability and backup coverage per source version. Reference-only bytes are never
    shown as backed up."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.artifact_coverage(
            pid,
            _caller(svc, pid, scope, capability_file),
            source_id=source,
            limit=limit,
            cursor=cursor,
            newest_first=newest_first,
        )

    _run(as_json, action)


@app.command("now")
def now_cmd(
    since_seq: int = typer.Option(None, "--since-seq", help="Also list records changed after it."),
    due_within_days: int = typer.Option(7, "--due-within-days"),
    silence_after_hours: int = typer.Option(24, "--silence-after-hours"),
    limit: int = typer.Option(50, "--limit"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """What changed, is due, is running, awaits interpretation or awaits approval, each with
    its reason (generated from structured state; no prose handoff)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.now(
            pid,
            _caller(svc, pid, scope, capability_file),
            since_seq=since_seq,
            due_within_days=due_within_days,
            silence_after_hours=silence_after_hours,
            limit=limit,
        )

    _run(as_json, action)


# ----- CP18.2-CP18.3: research events and the agent-visible queue (decision 0035) -------


@app.command("queue")
def queue_cmd(
    limit: int = typer.Option(100, "--limit"),
    proposed: str = typer.Option(
        "fold", "--proposed", help="fold: suggested items as groups; list: one entry each."
    ),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Open work items and unacknowledged events with reasons, by due time then age.
    Read-only: it starts, claims and changes nothing."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.work_queue(pid, _caller(svc, pid, scope, capability_file), limit, proposed)

    _run(as_json, action)


agent_app = typer.Typer(
    help="Commands a coding host's hooks run (the agent package, CP19.3).", no_args_is_help=True
)
app.add_typer(agent_app, name="agent")


@agent_app.command("session-start")
def agent_session_start(
    project: str = ProjectOpt,
    limit: int = typer.Option(20, "--limit", help="Entries shown (1..200)."),
    home: Path = typer.Option(
        None, "--home", help="The PrometheOS home, when the package was set up with one."
    ),
) -> None:
    """The session-start summary for a coding host's hook: open work items and events as
    text for the agent, which asks the researcher. Read-only, like `queue`. It never fails a
    session: any problem is one line on stderr, nothing on stdout, and exit status 0."""
    from prometheos.agent_package import session_start_text

    try:
        if home is not None:
            os.environ["PROMETHEOS_HOME"] = str(home)
        svc = _service()
        pid = svc.resolve_project_id(project)
        text = session_start_text(svc.work_queue(pid, Caller(actor="cli"), limit))
    except Exception as exc:  # a hook must not break the host's session
        code = exc.code if isinstance(exc, PrometheosError) else type(exc).__name__
        sys.stderr.write(f"prometheos session-start: no summary ({code})\n")
        return
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


events_app = typer.Typer(
    help="Research events: list them; the owner acknowledges them.", no_args_is_help=True
)
app.add_typer(events_app, name="events")


@events_app.command("list")
def events_list_cmd(
    event_type: str = typer.Option(None, "--type", help="One catalogue type."),
    subject_id: str = typer.Option(None, "--subject", help="The subject's ID."),
    state: str = typer.Option("open", "--state", help="open, acknowledged or all."),
    cursor: str = typer.Option(None, "--cursor"),
    limit: int = typer.Option(50, "--limit"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Events by type, subject and acknowledgement state, oldest first (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.event_list(
            pid,
            _caller(svc, pid, scope, capability_file),
            event_type=event_type,
            subject_id=subject_id,
            state=state,
            cursor=cursor,
            limit=limit,
        )

    _run(as_json, action)


@events_app.command("ack")
def events_ack_cmd(
    event_ids: list[str],
    note: str = typer.Option(None, "--note", help="Why (a dismissal reason, for example)."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: acknowledge events. Without an interactive terminal it becomes an
    approval request in the project (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        if not _interactive():
            params: dict[str, Any] = {"event_ids": list(event_ids), "note": note}
            _request_approval(svc, pid, Caller("cli"), "event_acknowledge", params, request_id)
        from prometheos.app import events

        owner = Caller("cli-owner", scope="owner")
        found = svc._read(pid, lambda c, d: events.open_events(c, owner, list(event_ids)))
        lines = [f"- {e.event_id}: {events.reason(e)}" for e in found]
        _confirm_owner("Acknowledge these events:\n" + "\n".join(lines))
        return svc.event_acknowledge(
            pid, list(event_ids), note, authority=_owner_act("cli-owner-events")
        )

    _run(as_json, action)


# ----- A05, CP17: adoption of an existing folder (decision 0039) -------------------------

adopt_app = typer.Typer(
    help="Adopt an existing folder (an approved research root) without writing into it.",
    no_args_is_help=True,
)
app.add_typer(adopt_app, name="adopt")


def _note_unavailable(result: Any, as_json: bool) -> Any:
    """Name, on stderr, analyzers this installation could not run (decision 0053); the
    document on stdout carries the same list (``analyzers_unavailable`` or ``unavailable``)."""
    if not as_json and isinstance(result, dict):
        scans = result.get("scans") or [result]
        # Newest first: only each folder's latest scan speaks for what is missing now (an older
        # scan made before the extra was installed is history; review cp17-0053-a-r2 R2-1).
        newest: dict[Any, dict[str, Any]] = {}
        for scan in scans:
            newest.setdefault(scan.get("root_alias"), scan)
        scans = list(newest.values())
        missing = (
            [
                item
                for scan in scans
                for item in (scan.get("analysis") or {}).get("analyzers_unavailable") or []
            ]
            or result.get("unavailable")
            or []
        )
        once = {(item["analyzer"], item["reason"]): item for item in missing}
        for item in once.values():
            sys.stderr.write(
                f"note: {item['analyzer']} did not run: {visible(item['reason'])}; "
                f"{visible(item['install'])}\n"
            )
    return result


@adopt_app.command("scan")
def adopt_scan_cmd(
    root_alias: str = typer.Option(..., "--root", help="Approved research root alias."),
    exclude: list[str] = typer.Option(None, "--exclude", help="Path glob to exclude (repeat)."),
    max_members: int = typer.Option(None, "--max-members", help="Archive member limit."),
    max_depth: int = typer.Option(None, "--max-depth", help="Archive nesting limit."),
    profile: str = typer.Option("generic", "--profile"),
    no_wait: bool = NoWaitOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Inventory the root: stat, digests, container and zone of every file; cloud
    placeholders are not opened; archives are listed within limits; nothing is written into
    the folder (root digest before and after). An operation run."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        svc.worker_args = _worker_args(scope, capability_file)
        limits = {k: v for k, v in (("max_members", max_members), ("max_depth", max_depth)) if v}
        return _note_unavailable(
            svc.adoption_scan(
                pid, caller, root_alias=root_alias, exclusions=list(exclude or []),
                limits=limits or None, profile=profile, mode=_mode(no_wait),
            ),
            as_json,
        )  # fmt: skip

    _run(as_json, action)


@adopt_app.command("scans")
def adopt_scans_cmd(
    root_alias: str = typer.Option(None, "--root", help="Only this root's scans."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Scan generations, newest first, with state, counts and root digests (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return _note_unavailable(
            svc.adoption_scans(pid, _caller(svc, pid, scope, capability_file), root_alias),
            as_json,
        )

    _run(as_json, action)


@adopt_app.command("files")
def adopt_files_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    status: str = typer.Option(None, "--status", help="read, listed, placeholder, ..."),
    zone: str = typer.Option(None, "--zone"),
    container: str = typer.Option(None, "--container"),
    prefix: str = typer.Option(None, "--prefix", help="Path prefix inside the root."),
    offset: int = typer.Option(0, "--offset"),
    limit: int = typer.Option(200, "--limit"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Inventory rows of one scan, paged (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_files(
            pid, _caller(svc, pid, scope, capability_file), scan_gen, status=status,
            zone=zone, container=container, prefix=prefix, offset=offset, limit=limit,
        )  # fmt: skip

    _run(as_json, action)


@adopt_app.command("evidence")
def adopt_evidence_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    path: str = typer.Option(None, "--path", help="Everything said about this file."),
    predicate: str = typer.Option(None, "--predicate"),
    tier: str = typer.Option(None, "--tier", help="T1..T8"),
    analyzer: str = typer.Option(None, "--analyzer"),
    offset: int = typer.Option(0, "--offset"),
    limit: int = typer.Option(200, "--limit"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Typed evidence of one scan with tiers, locators and quotes (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_evidence(
            pid, _caller(svc, pid, scope, capability_file), scan_gen, path=path,
            predicate=predicate, tier=tier, analyzer=analyzer, offset=offset, limit=limit,
        )  # fmt: skip

    _run(as_json, action)


@adopt_app.command("families")
def adopt_families_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    band: str = typer.Option(None, "--band", help="decided, supported, weak or undecided."),
    offset: int = typer.Option(0, "--offset"),
    limit: int = typer.Option(200, "--limit"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Families of one scan with their ranked current member (proposals; read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_families(
            pid, _caller(svc, pid, scope, capability_file), scan_gen, band=band,
            offset=offset, limit=limit,
        )  # fmt: skip

    _run(as_json, action)


@adopt_app.command("family")
def adopt_family_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    family_id: str = typer.Argument(..., help="fam_..."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One family: members, ranks, reasons and conflicts (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_family(
            pid, _caller(svc, pid, scope, capability_file), scan_gen, family_id
        )

    _run(as_json, action)


assist_app = typer.Typer(
    help="Analysis tasks the coding agent performs and reports (results are validated).",
    no_args_is_help=True,
)
adopt_app.add_typer(assist_app, name="assist")


@assist_app.command("tasks")
def adopt_assist_tasks_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Suggested analysis tasks, most useful first (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_assist_tasks(pid, _caller(svc, pid, scope, capability_file), scan_gen)

    _run(as_json, action)


@assist_app.command("prepare")
def adopt_assist_prepare_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    task_kind: str = typer.Argument(..., help="extract_status_statements, classify_file, ..."),
    subject: str = typer.Argument(..., help="The file (path in the inventory)."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One task's input, closed schema and instructions (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_assist_prepare(
            pid, _caller(svc, pid, scope, capability_file), scan_gen, task_kind, subject
        )

    _run(as_json, action)


@assist_app.command("submit")
def adopt_assist_submit_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    task_kind: str = typer.Argument(...),
    subject: str = typer.Argument(...),
    response_file: Path = typer.Option(
        ..., "--response", help="The result as JSON, or - for stdin."
    ),
    sample: int = typer.Option(1, "--sample"),
    model: str = typer.Option(None, "--model", help="The model or tool that produced it."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Submit one sample of a task's result (validated; proposals only)."""
    svc = _service()

    def action() -> Any:
        raw = sys.stdin.buffer.read() if str(response_file) == "-" else response_file.read_bytes()
        pid = svc.resolve_project_id(project)
        return svc.adoption_assist_submit(
            pid, _caller(svc, pid, scope, capability_file), scan_gen, task_kind, subject,
            sample, loads_strict(raw), model,
        )  # fmt: skip

    _run(as_json, action)


plan_app = typer.Typer(
    help="The consolidation plan: steps as work items, agent results (decision 0043).",
    no_args_is_help=True,
)
adopt_app.add_typer(plan_app, name="plan")


@plan_app.command("create")
def adopt_plan_create_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation (complete)."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Derive a new plan version; each step becomes a proposed work item."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_plan_create(pid, _caller(svc, pid, scope, capability_file), scan_gen)

    _run(as_json, action)


@plan_app.command("show")
def adopt_plan_show_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """The plan's steps with state, work item and result (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_plan(pid, _caller(svc, pid, scope, capability_file), scan_gen)

    _run(as_json, action)


@plan_app.command("report")
def adopt_plan_report_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    step_id: str = typer.Argument(..., help="The step (st_...)."),
    result_file: Path = typer.Option(..., "--result", help="The result as JSON, or - for stdin."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Report an agent step's result (validated against its schema and the inventory)."""
    svc = _service()

    def action() -> Any:
        raw = sys.stdin.buffer.read() if str(result_file) == "-" else result_file.read_bytes()
        pid = svc.resolve_project_id(project)
        return svc.adoption_step_report(
            pid, _caller(svc, pid, scope, capability_file), scan_gen, step_id, loads_strict(raw)
        )

    _run(as_json, action)


@adopt_app.command("decide")
def adopt_decide_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    family_id: str = typer.Argument(..., help="The family (fam_...)."),
    current: str = typer.Argument(..., help="The member that is current (inventory path)."),
    note: str = typer.Option(None, "--note", help="Why."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: a family's current file. Without an interactive terminal it becomes an
    approval request in the project (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        params: dict[str, Any] = {
            "scan_gen": scan_gen, "family_id": family_id, "current": current, "note": note,
        }  # fmt: skip
        if not _interactive():
            _request_approval(svc, pid, Caller("cli"), "adoption_decide", params, request_id)
        family = svc.adoption_family(pid, Caller("cli-owner", scope="owner"), scan_gen, family_id)
        _confirm_owner(
            f"Make {current} the current file of family {family['key']} "
            f"(now: {family['current'] or 'undecided'})"
        )
        return svc.adoption_decide(
            pid, scan_gen, family_id, current, note, authority=_owner_act("cli-owner-adoption")
        )

    _run(as_json, action)


@adopt_app.command("classify")
def adopt_classify_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    family_id: str = typer.Argument(..., help="The family (fam_...)."),
    decision: str = typer.Argument(..., help="variant, noise or split."),
    note: str = typer.Option(None, "--note", help="Why."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision (A06.1): a family is a variant, noise or a split, not competition. Without
    an interactive terminal it becomes an approval request in the project (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        params: dict[str, Any] = {
            "scan_gen": scan_gen, "family_id": family_id, "decision": decision, "note": note,
        }  # fmt: skip
        if not _interactive():
            _request_approval(svc, pid, Caller("cli"), "adoption_classify", params, request_id)
        family = svc.adoption_family(pid, Caller("cli-owner", scope="owner"), scan_gen, family_id)
        _confirm_owner(f"Record family {family['key']} as {decision}")
        return svc.adoption_classify(
            pid, scan_gen, family_id, decision, note, authority=_owner_act("cli-owner-adoption")
        )

    _run(as_json, action)


@adopt_app.command("cite")
def adopt_cite_cmd(
    baseline_id: str = typer.Argument(..., help="The accepted baseline (bl_...)."),
    path: str = typer.Argument(..., help="The file's path in the adopted folder."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Cite a baseline file as a reference-only version bound to the scan's digest (not
    copied; unverified until `source check` confirms its bytes)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_cite(
            pid, _caller(svc, pid, scope, capability_file), baseline_id, path, request_id
        )

    _run(as_json, action)


outline_app = typer.Typer(
    help="The scan's proposed outline, confirmed once by the owner (A06.1).", no_args_is_help=True
)
adopt_app.add_typer(outline_app, name="outline")


def _outline_edits(edits_file: Path | None) -> dict[str, Any]:
    if edits_file is None:
        return {}
    raw = sys.stdin.buffer.read() if str(edits_file) == "-" else edits_file.read_bytes()
    value = loads_strict(raw)
    if not isinstance(value, dict):
        raise PrometheosError("validation_error", "the edits are a JSON object", {})
    return value


@outline_app.command("show")
def adopt_outline_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation (complete)."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """The proposed outline: tracks, deliverables, questions, results with their producers and
    stale candidates, each citing its file and line, and the outline's SHA-256 (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_outline(pid, _caller(svc, pid, scope, capability_file), scan_gen)

    _run(as_json, action)


@outline_app.command("preview")
def adopt_outline_preview_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation (complete)."),
    sha256: str = typer.Argument(..., help="The outline's SHA-256 as shown."),
    edits_file: Path = typer.Option(None, "--edits", help="JSON corrections file, or - for stdin."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """What confirming the outline with these corrections would record (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_outline_preview(
            pid, _caller(svc, pid, scope, capability_file), scan_gen, sha256,
            _outline_edits(edits_file),
        )  # fmt: skip

    _run(as_json, action)


@outline_app.command("confirm")
def adopt_outline_confirm_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation (complete)."),
    sha256: str = typer.Argument(..., help="The outline's SHA-256 as shown."),
    edits_file: Path = typer.Option(None, "--edits", help="JSON corrections file, or - for stdin."),
    note: str = typer.Option(None, "--note"),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: confirm the outline once. Without an interactive terminal it becomes an
    approval request in the project (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        edits = _outline_edits(edits_file)
        params: dict[str, Any] = {
            "scan_gen": scan_gen, "proposal_sha256": sha256, "edits": edits, "note": note,
        }  # fmt: skip
        if not _interactive():
            _request_approval(
                svc, pid, Caller("cli"), "adoption_outline_confirm", params, request_id
            )
        seen = svc.adoption_outline_preview(
            pid, Caller("cli-owner", scope="owner"), scan_gen, sha256, edits
        )
        kept = seen["kept"]
        _confirm_owner(
            f"Confirm the outline of scan {scan_gen}: {kept['track']} tracks, "
            f"{kept['deliverable']} deliverables, {kept['question']} questions; "
            f"{seen['records_created']} records created, {seen['relations']} links added"
        )
        return svc.adoption_outline_confirm(
            pid, scan_gen, sha256, edits, note, authority=_owner_act("cli-owner-adoption")
        )

    _run(as_json, action)


baseline_app = typer.Typer(help="Adoption baselines (accepted by the owner).", no_args_is_help=True)
adopt_app.add_typer(baseline_app, name="baseline")


@baseline_app.command("accept")
def adopt_baseline_accept_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation (complete)."),
    note: str = typer.Option(None, "--note"),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: accept a scan as the folder's baseline. Without an interactive terminal
    it becomes an approval request in the project (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        if not _interactive():
            params: dict[str, Any] = {"scan_gen": scan_gen, "note": note}
            _request_approval(
                svc, pid, Caller("cli"), "adoption_baseline_accept", params, request_id
            )
        current = svc.adoption_plan(pid, Caller("cli-owner", scope="owner"), scan_gen)
        validated = sum(1 for s in current["steps"] if s["state"] == "validated")
        _confirm_owner(
            f"Accept scan {scan_gen} of {current['root_alias']} as the adoption baseline "
            f"({validated} validated results become accepted)"
        )
        return svc.adoption_baseline_accept(
            pid, scan_gen, note, authority=_owner_act("cli-owner-adoption")
        )

    _run(as_json, action)


@baseline_app.command("show")
def adopt_baseline_show_cmd(
    baseline_id: str = typer.Argument(..., help="The baseline (bl_...)."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One accepted baseline with its payload and digest (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_baseline(pid, _caller(svc, pid, scope, capability_file), baseline_id)

    _run(as_json, action)


@baseline_app.command("list")
def adopt_baseline_list_cmd(
    root_alias: str = typer.Option(None, "--root", help="Only this folder's baselines."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Accepted baselines, newest first (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_baselines(pid, _caller(svc, pid, scope, capability_file), root_alias)

    _run(as_json, action)


@baseline_app.command("export")
def adopt_baseline_export_cmd(
    baseline_id: str = typer.Argument(..., help="The baseline (bl_...)."),
    out: Path = typer.Option(..., "--out", help="The package file to write (JSON)."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Write a baseline as a self-checking package (re-importable into another store)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        package = svc.adoption_baseline_export(
            pid, _caller(svc, pid, scope, capability_file), baseline_id
        )
        if out.exists():
            raise PrometheosError("validation_error", "the output file exists", {})
        out.write_bytes(dumps_canonical(package))
        return {
            "baseline_id": baseline_id,
            "sha256": package["sha256"],
            "bytes": out.stat().st_size,
        }

    _run(as_json, action)


@baseline_app.command("import")
def adopt_baseline_import_cmd(
    package_file: Path = typer.Argument(..., help="A package from 'adopt baseline export'."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: import an exported baseline into this store (interactive terminal
    only; the package carries owner decisions)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        package = loads_strict(package_file.read_bytes())
        if not isinstance(package, dict):
            raise PrometheosError("validation_error", "not an adoption baseline package", {})
        baseline = package.get("baseline") or {}
        _confirm_owner(
            f"Import baseline {baseline.get('baseline_id')} of "
            f"{(baseline.get('payload') or {}).get('root_alias')} with "
            f"{len(package.get('decisions') or [])} decision(s) into this project"
        )
        return svc.adoption_baseline_import(
            pid, package, authority=_owner_act("cli-owner-adoption")
        )

    _run(as_json, action)


@adopt_app.command("drift")
def adopt_drift_cmd(
    scan_gen: int = typer.Argument(..., help="Scan generation."),
    against: str = typer.Option("previous", "--against", help="'previous' or a baseline ID."),
    change: str = typer.Option(None, "--change", help="added, changed, moved or deleted."),
    offset: int = typer.Option(0, "--offset"),
    limit: int = typer.Option(200, "--limit"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """What changed since the previous scan or an accepted baseline (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_drift(
            pid, _caller(svc, pid, scope, capability_file), scan_gen, against, change, offset,
            limit,
        )  # fmt: skip

    _run(as_json, action)


vault_app = typer.Typer(help="An approved notes vault's Markdown as sources.", no_args_is_help=True)
adopt_app.add_typer(vault_app, name="vault")


@vault_app.command("import")
def adopt_vault_import_cmd(
    root_alias: str = typer.Argument(..., help="The vault's approved root alias."),
    offset: int = typer.Option(0, "--offset", help="Where the batch starts."),
    retention: str = typer.Option(None, "--retention", help="copy or reference."),
    sensitivity: str = typer.Option("normal", "--sensitivity"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Plan a source import of one batch of the vault's notes (commit with 'import commit')."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.adoption_vault_import(
            pid, _caller(svc, pid, scope, capability_file), root_alias, offset, retention,
            sensitivity,
        )  # fmt: skip

    _run(as_json, action)


export_app = typer.Typer(help="One-way exports of adoption baselines.", no_args_is_help=True)
adopt_app.add_typer(export_app, name="export")


@export_app.command("markdown")
def adopt_export_markdown_cmd(
    baseline_id: str = typer.Argument(..., help="The baseline (bl_...)."),
    root_alias: str = typer.Argument(..., help="The approved folder to write the note in."),
    folder: str = typer.Option("", "--folder", help="A folder inside it (relative)."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: write the baseline's fenced block into adoption-<folder>.md. Without
    an interactive terminal it becomes an approval request (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        params: dict[str, Any] = {
            "baseline_id": baseline_id, "root_alias": root_alias, "folder": folder,
        }  # fmt: skip
        if not _interactive():
            _request_approval(
                svc, pid, Caller("cli"), "adoption_export_markdown", params, request_id
            )
        planned = svc.adoption_export_markdown_plan(
            pid, Caller("cli-owner", scope="owner"), baseline_id, root_alias, folder
        )
        _confirm_owner(
            f"{planned['action'].capitalize()} the note {planned['note']} in {root_alias} "
            "(text outside PrometheOS's block is kept)"
        )
        return svc.adoption_export_markdown(
            pid, baseline_id, root_alias, folder, authority=_owner_act("cli-owner-adoption")
        )

    _run(as_json, action)


@adopt_app.command("analyzers")
def adopt_analyzers_cmd(
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """The analyzer declarations (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return _note_unavailable(
            svc.adoption_analyzers(pid, _caller(svc, pid, scope, capability_file)), as_json
        )

    _run(as_json, action)


# ----- A02, CP11 S11.1: the literature library (decision 0060) ---------------------------

library_app = typer.Typer(
    help="The literature library: PDFs from an approved folder as located, searchable works.",
    no_args_is_help=True,
)
app.add_typer(library_app, name="library")
quote_app = typer.Typer(help="Quotation checks against stored page text.", no_args_is_help=True)
library_app.add_typer(quote_app, name="quote")


@library_app.command("import")
def library_import_cmd(
    plan: bool = typer.Option(False, "--plan", help="Plan the import (lists every PDF found)."),
    commit: str = typer.Option(None, "--commit", help="Commit this plan ID."),
    root_alias: str = typer.Option(None, "--root", help="Approved research root alias."),
    path: str = typer.Option(None, "--path", help="Only this folder or file inside the root."),
    sensitivity: str = typer.Option("normal", "--sensitivity"),
    csv_column: list[str] = typer.Option(
        None, "--csv-column", help="CSV HEADER=FIELD (title, contributors, year, venue, doi...)."
    ),
    no_wait: bool = NoWaitOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Plan importing the PDFs and bibliographies (.bib, .ris, .csv) under a root (digests,
    pages, entries, identifiers, refusals with reasons; nothing is written into the folder),
    then commit the plan. Each step is an operation run."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        if plan == bool(commit):
            raise PrometheosError("validation_error", "use exactly one of --plan or --commit", {})
        caller = _caller(svc, pid, scope, capability_file)
        svc.worker_args = _worker_args(scope, capability_file)
        if commit:
            return svc.library_import_commit(pid, commit, caller, mode=_mode(no_wait))
        if not root_alias:
            raise PrometheosError("validation_error", "--plan needs --root", {})
        columns = {}
        for pair in csv_column or []:
            header, sep, target = pair.partition("=")
            if not sep or not header or not target:
                raise PrometheosError("validation_error", "--csv-column is HEADER=FIELD", {})
            columns[header] = target
        return svc.library_import_plan(
            pid, caller, root_alias=root_alias, path=path, sensitivity=sensitivity,
            csv_columns=columns or None, mode=_mode(no_wait),
        )  # fmt: skip

    _run(as_json, action)


@library_app.command("works")
def library_works_cmd(
    offset: int = typer.Option(0, "--offset", min=0),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Works in the library, by title (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.library_works(pid, caller, offset=offset, limit=limit)

    _run(as_json, action)


@library_app.command("work")
def library_work_cmd(
    work_id: str = typer.Argument(..., help="Work ID."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One work with the provenance of each field and its pages (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.library_work(pid, _caller(svc, pid, scope, capability_file), work_id)

    _run(as_json, action)


@library_app.command("search")
def library_search_cmd(
    query: str = typer.Argument(..., help="Words to find (all of them, on one page)."),
    limit: int = typer.Option(20, "--limit", min=1, max=50),
    work_id: str = typer.Option(None, "--work", help="Only this work."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Page-level search: located snippets with quote digests (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.library_search(pid, caller, query, limit=limit, work_id=work_id)

    _run(as_json, action)


@library_app.command("read")
def library_read_cmd(
    work_id: str = typer.Argument(..., help="Work ID."),
    page: int = typer.Option(1, "--page", min=1),
    start: int = typer.Option(None, "--start", min=0, help="First character (default 0)."),
    end: int = typer.Option(None, "--end", min=0, help="End character (exclusive)."),
    max_chars: int = typer.Option(4000, "--max-chars", min=1, max=8000),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """A bounded, located chunk of one page (never a whole paper; read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.library_read(
            pid, caller, work_id, page=page, start=start, end=end, max_chars=max_chars
        )

    _run(as_json, action)


dedup_app = typer.Typer(
    help="Duplicate and version candidates; the owner resolves them.",
    invoke_without_command=True,
)
library_app.add_typer(dedup_app, name="dedup")


def _dedup_list(
    project: str | None, scope: str, capability_file: Path | None, as_json: bool
) -> None:
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.library_dedup(pid, _caller(svc, pid, scope, capability_file))

    _run(as_json, action)


@dedup_app.callback()
def dedup_default(
    ctx: typer.Context,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Without a command: list the candidates (as `library dedup list`)."""
    if ctx.invoked_subcommand is None:
        _dedup_list(project, scope, capability_file, as_json)


@dedup_app.command("list")
def dedup_list_cmd(
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Candidates (similar title, first author and year), proposals only, with evidence."""
    _dedup_list(project, scope, capability_file, as_json)


@dedup_app.command("resolve")
def dedup_resolve_cmd(
    work_a: str = typer.Argument(..., help="One work of the pair."),
    work_b: str = typer.Argument(..., help="The other work."),
    merge: bool = typer.Option(False, "--merge", help="One work (the newer gets merged_into)."),
    distinct: bool = typer.Option(False, "--distinct", help="Two works; never propose again."),
    versions: bool = typer.Option(False, "--versions", help="Related versions (version_of)."),
    note: str = typer.Option(None, "--note", help="Why."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision for a candidate pair. Without an interactive terminal it becomes an
    approval request in the project (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        flags = (("merge", merge), ("distinct", distinct), ("versions", versions))
        chosen = [name for name, on in flags if on]
        if len(chosen) != 1:
            raise PrometheosError(
                "validation_error", "use exactly one of --merge, --distinct or --versions", {}
            )
        params: dict[str, Any] = {
            "work_a": work_a, "work_b": work_b, "choice": chosen[0], "note": note,
        }  # fmt: skip
        if not _interactive():
            _request_approval(svc, pid, Caller("cli"), "library_dedup_resolve", params, request_id)
        _confirm_owner(f"Resolve {work_a} and {work_b}: {chosen[0]}")
        return svc.library_dedup_resolve(
            pid, work_a, work_b, chosen[0], note, authority=_owner_act("cli-owner-library"),
            request_id=request_id,
        )  # fmt: skip

    _run(as_json, action)


@dedup_app.command("undo")
def dedup_undo_cmd(
    work_a: str = typer.Argument(..., help="One work of the pair."),
    work_b: str = typer.Argument(..., help="The other work."),
    note: str = typer.Option(None, "--note", help="Why."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: undo a pair's merge, version link or keep-separate choice, or an
    import's version link. Without an interactive terminal it becomes an approval request."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        params: dict[str, Any] = {"work_a": work_a, "work_b": work_b, "note": note}
        if not _interactive():
            _request_approval(svc, pid, Caller("cli"), "library_dedup_undo", params, request_id)
        _confirm_owner(f"Undo the decision or version link for {work_a} and {work_b}")
        return svc.library_dedup_undo(
            pid, work_a, work_b, note, authority=_owner_act("cli-owner-library"),
            request_id=request_id,
        )  # fmt: skip

    _run(as_json, action)


@library_app.command("export")
def library_export_cmd(
    fmt: str = typer.Option(..., "--format", help="bibtex, ris or csv."),
    work: list[str] = typer.Option(None, "--work", help="Only these works (repeat)."),
    out: Path = typer.Option(None, "--out", help="Write the export to this new file."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Export works with stable keys and annotations (read-only; --out writes a new file)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        result = svc.library_export(pid, caller, fmt, list(work or []) or None)
        if out is not None:
            if out.exists():
                raise PrometheosError("validation_error", "output file already exists", {})
            with open(out, "x", encoding="utf-8", newline="") as handle:
                handle.write(result["content"])
            result = {k: v for k, v in result.items() if k != "content"} | {"written": str(out)}
        return result

    _run(as_json, action)


@library_app.command("annotate")
def library_annotate_cmd(
    work_id: str = typer.Argument(..., help="Work ID."),
    page: int = typer.Option(..., "--page", min=1),
    start: int = typer.Option(..., "--start", min=0, help="First character (code points)."),
    end: int = typer.Option(..., "--end", min=1, help="End character (exclusive)."),
    text: str = typer.Option(..., "--text", help="The annotation."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """An entry citing the work's PDF at a verified span (page, offsets, quote digest)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.library_annotate(
            pid, caller, work_id, page=page, start=start, end=end, text=text,
            request_id=request_id,
        )  # fmt: skip

    _run(as_json, action)


@library_app.command("annotations")
def library_annotations_cmd(
    work_id: str = typer.Argument(..., help="Work ID."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """A work's annotations (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.library_annotations(pid, _caller(svc, pid, scope, capability_file), work_id)

    _run(as_json, action)


@quote_app.command("check")
def library_quote_check_cmd(
    page: int = typer.Option(..., "--page", min=1),
    start: int = typer.Option(..., "--start", min=0),
    end: int = typer.Option(..., "--end", min=1),
    work_id: str = typer.Option(None, "--work", help="Work ID (or --version)."),
    version_id: str = typer.Option(None, "--version", help="PDF or extracted version ID."),
    quote: str = typer.Option(None, "--quote", help="The quoted text (or --sha256)."),
    quote_sha256: str = typer.Option(None, "--sha256", help="The quotation's SHA-256."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Check a quotation at a page and character span: match or mismatch, with the true text."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.library_quote_check(
            pid, caller, work_id=work_id, version_id=version_id, page=page, start=start,
            end=end, quote=quote, quote_sha256=quote_sha256,
        )  # fmt: skip

    _run(as_json, action)


# ----- backup ----------------------------------------------------------------------


@backup_app.command("create")
def backup_create(
    out: Path = typer.Option(None, "--out"),
    capability_file: Path = CapabilityOpt,
    request_id: str = typer.Option(None, "--request-id", help="Agents: idempotent request key."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Create a verified snapshot archive (needs a full_data_export owner capability). Without
    one, a non-interactive caller (an agent) requests the backup: approval_required (CP10.5)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        secret = _read_secret(capability_file)
        if secret is None and not _interactive():
            if out is not None:
                raise PrometheosError(
                    "policy_denied",
                    "backups need the owner's full-data scope; a requested backup is written "
                    "into the store's exports folder (omit --out to request one)",
                    {},
                )
            _request_approval(svc, pid, Caller("cli"), "backup_create", {}, request_id)
        owner = svc.owner_caller(pid, secret, "full_data_export")
        return svc.backup(pid, out, owner)

    _run(as_json, action)


@backup_app.command("verify")
def backup_verify(path: Path, as_json: bool = JsonOpt) -> None:
    """Validate an archive completely without changing any store."""
    _run(as_json, lambda: _service().verify_backup(path))


@backup_app.command("restore")
def backup_restore(
    path: Path,
    into: Path = typer.Option(..., "--into", help="New directory; never overwritten."),
    rebind: list[str] = typer.Option(None, "--rebind", help="Rebind research root ALIAS=PATH."),
    declare_local: bool = typer.Option(False, "--declare-local"),
    as_json: bool = JsonOpt,
) -> None:
    """Verify into staging, then register the restored store (owner, interactive)."""

    def action() -> Any:
        # Restore may run on a machine with no store yet, so no capability can exist:
        # the owner confirms on a local terminal instead.
        svc = _service()
        verified = svc.verify_backup(path)  # hostile archives fail before any prompt
        summary, purpose = _restore_summary(svc, path, into, verified["manifest"])
        _confirm_owner(summary, purpose=purpose)
        owner = Caller(actor="cli-owner-restore", scope="owner", full_data=True)
        return _service().restore(
            path,
            into,
            rebind=_pairs(rebind, "--rebind"),
            declare_local=declare_local,
            caller=owner,
        )

    _run(as_json, action)


def _restore_summary(svc: Any, path: Path, into: Path, manifest: Any) -> tuple[str, str]:
    """What the owner confirms for ``backup restore`` (decision 0021 round 2, F2): over a
    registered project whose store is not damaged, the restore is a rollback, and the prompt
    says so with the current sequence against the backup's."""
    from prometheos.app.maintenance import DAMAGED
    from prometheos.store.schema import MIGRATIONS

    summary = f"Restore {path} into new directory {into}"
    if int(manifest["schema_version"]) < len(MIGRATIONS):
        # Decision 0052: the owner confirms the upgrade too.
        summary += (
            f"\nThis backup is at schema {manifest['schema_version']}; the restored copy is "
            f"upgraded to schema {len(MIGRATIONS)} before it is registered, and the backup is "
            "kept in the new store's exports/ as its pre-migration backup."
        )
    pid = str(manifest["project_id"])
    try:
        svc.registry.entry(pid)
    except PrometheosError as exc:
        if exc.code != "not_found":
            raise
        return summary, "To approve exactly this"
    # Registry absence is the only plain-restore case. A failed health preview must stop
    # before confirmation, and a registered project's rollback uses a fresh health/sequence.
    current = svc.maintenance_inspect(pid, Caller("cli-owner", scope="owner"), refresh=True)
    health, seq = current["health"], current["database"]["project_seq"]
    if health in DAMAGED:
        return (
            f"{summary}\nThe registered store of {pid} is {health}; it is kept unchanged and "
            "listed as quarantined.",
            "To approve exactly this",
        )
    return (
        f"{summary}\nROLLBACK: project {pid} is registered and its store is {health} at "
        f"sequence {seq if seq is not None else 'unknown'}; this backup is at sequence "
        f"{manifest['project_seq']} (made {manifest['created_at']}). The project would continue "
        "from the backup; later work stays only in the current store, which is kept and listed "
        "as quarantined.",
        "To roll the project back to this backup",
    )


def _location(value: str) -> str:
    """One location rule on every transport (round 2, F8): a ``<PrometheOS home>/...`` label
    stays a label; anything else is a path, made absolute here."""
    from prometheos.app.maintenance import HOME_LABEL

    return value if value.startswith(HOME_LABEL + "/") else str(Path(value).resolve())


# ----- maintenance and doctor --------------------------------------------------------


@maint_app.command("orphans")
def maintenance_orphans(
    collect: bool = typer.Option(False, "--collect"),
    grace_seconds: int = typer.Option(24 * 3600, "--grace-seconds"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Report (and optionally collect) unreferenced media and staging files."""
    svc = _service()
    _run(as_json, lambda: svc.orphans(svc.resolve_project_id(project), collect, grace_seconds))


@maint_app.command("migrate")
def maintenance_migrate(
    request_id: str = typer.Option(None, "--request-id", help="Agents: idempotent request key."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Upgrade the store schema after a verified backup of the old schema. An owner decision:
    confirm it in an interactive terminal. A non-interactive caller (an agent) gets
    approval_required: the request is held in the per-user registry (a store that needs
    migration cannot hold a project approval request; decisions 0016, 0021)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        if not _interactive():
            return svc.maintenance_migrate_request(pid, Caller("cli"), request_id)
        _confirm_owner(
            f"Migrate the store of {pid} to the current schema (a verified backup of the old "
            "schema is written first)",
            purpose="To migrate this store",
        )
        return svc.migrate(pid, authority=_owner_act("cli-owner-migrate"))

    _run(as_json, action)


# ----- CP10.5: the maintenance service (no project database; decision 0021) ---------------


@maint_app.command("inspect")
def maintenance_inspect(
    quarantine: str = typer.Option(
        None, "--quarantine", help="Inspect one quarantined store (its quarantine ID)."
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Recheck health even if metadata is unchanged."
    ),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Integrity, schema version and lock state of the project's store, its backups,
    maintenance requests and quarantined stores. Opens no project database."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.maintenance_inspect(
            svc.resolve_project_id(project), Caller("cli"), quarantine, refresh=refresh
        ),
    )


@maint_app.command("verify")
def maintenance_verify(backup: str, project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Verify a backup of this project (an archive name from inspect, or a path) in a scratch
    staging folder: member paths, digests, schema and database integrity."""
    svc = _service()
    _run(as_json, lambda: svc.maintenance_verify(svc.resolve_project_id(project), _backup(backup)))


def _backup(value: str) -> str:
    """An archive name stays a name; anything else is a path, made absolute here."""
    from prometheos.app.maintenance import BACKUP_NAME

    return value if BACKUP_NAME.fullmatch(value) else str(Path(value).resolve())


@maint_app.command("restore")
def maintenance_restore(
    backup: str,
    into: str = typer.Option(
        None,
        "--into",
        help="Owner only (interactive): a new folder or a <PrometheOS home>/... label. Without "
        "it the store lands in a new folder under <PrometheOS home>/stores/.",
    ),
    declare_local: bool = typer.Option(False, "--declare-local", help="Owner only."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Verify a backup and restore it into a new store. The registry does not change and the
    damaged store is never written; switch to the new store with `maintenance switch`. Naming
    the location is the owner's (interactive confirmation); an agent's restore always lands in
    a new folder PrometheOS creates under the home."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        chosen = _backup(backup)
        if into is None and not declare_local:
            return svc.maintenance_restore(pid, chosen)
        where = _location(into) if into is not None else None
        owner = None
        if _interactive():
            _confirm_owner(
                f"Restore {chosen} of project {pid} into {where or 'a new managed folder'}",
                purpose="To restore into this location",
            )
            owner = _owner_act("cli-owner-maintenance")
        return svc.maintenance_restore(pid, chosen, where, declare_local=declare_local, owner=owner)

    _run(as_json, action)


@maint_app.command("switch")
def maintenance_switch(
    backup: str,
    target: str = typer.Option(
        ..., "--target", help="The restored store: its <PrometheOS home>/... label or a path."
    ),
    request_id: str = typer.Option(None, "--request-id", help="Agents: idempotent request key."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Switch the registry to a verified restored store; the damaged store is quarantined and
    kept. In an interactive terminal the owner confirms and it switches at once; any other
    caller (an agent) gets approval_required with a request held in the per-user registry."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        chosen, where = _backup(backup), _location(target)
        if not _interactive():
            return svc.maintenance_switch(pid, Caller("cli"), chosen, where, request_id=request_id)
        _confirm_owner(
            f"Switch project {pid} to the restored store {where} (from backup {chosen}); the "
            "current store is kept unchanged and listed as quarantined",
            purpose="To switch the registry",
        )
        return svc.maintenance_switch(
            pid, Caller("cli"), chosen, where, owner=_owner_act("cli-owner-maintenance")
        )

    _run(as_json, action)


@maint_app.command("recover-journal")
def maintenance_recover_journal(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Recover an interrupted write (a hot rollback journal) into a NEW store: the store and
    its journal are copied, SQLite rolls the journal back into the copy, which becomes a
    verified backup restored under <PrometheOS home>/stores/. Never applied in place."""
    svc = _service()
    _run(as_json, lambda: svc.maintenance_journal_recover(svc.resolve_project_id(project)))


@maint_app.command("get")
def maintenance_get(approval_id: str, project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """One maintenance request (registry switch or migration): plan, state and decision."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.maintenance_approval_get(
            svc.resolve_project_id(project), Caller("cli"), approval_id
        ),
    )


@maint_app.command("resume")
def maintenance_resume(
    approval_id: str, project: str = ProjectOpt, as_json: bool = JsonOpt
) -> None:
    """Run a granted maintenance request exactly once (the grant is held in the registry). A
    changed store, backup or target refuses it: request again."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.maintenance_resume(svc.resolve_project_id(project), Caller("cli"), approval_id),
    )


@maint_app.command("withdraw")
def maintenance_withdraw(
    approval_id: str, project: str = ProjectOpt, as_json: bool = JsonOpt
) -> None:
    """Withdraw your own maintenance request; a granted one loses its grant."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.maintenance_withdraw(
            svc.resolve_project_id(project), Caller("cli"), approval_id
        ),
    )


@maint_app.command("review")
def maintenance_review(
    request: str = typer.Option(None, "--request", help="A maintenance request ID (mapreq_...)."),
    grant: bool = typer.Option(False, "--grant"),
    deny: bool = typer.Option(False, "--deny"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """The owner's interactive maintenance session: list pending maintenance requests, show
    one, and grant or deny exactly its plan (each step behind a fresh confirmation code)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        _confirm_owner(
            "Unlock the owner maintenance session (registry switches and migrations requested "
            "by agents)",
            purpose="To show the maintenance requests",
        )
        if request is None:
            if grant or deny:
                raise PrometheosError("validation_error", "--grant/--deny need --request ID", {})
            return svc.maintenance_queue(pid)
        shown = svc.maintenance_approval_get(pid, Caller("cli-owner", scope="owner"), request)
        if not (grant or deny):
            return shown
        if grant and deny:
            raise PrometheosError("validation_error", "choose --grant or --deny", {})
        lines = [
            f"Maintenance request {shown['approval_request_id']} ({shown['operation']}, requested "
            f"by {shown['actor']} at rank {shown['caller_rank']}; expires {shown['expires_at']})",
            "Effects:",
            *[f"  - {_escaped_text(line)}" for line in shown["summary"]],
            f"Plan digest: {shown['plan_sha256']}",
            _escaped_block("Plan", shown["plan"]),
        ]
        _confirm_owner(
            "\n".join(lines), purpose=f"To {'grant' if grant else 'deny'} exactly this plan"
        )
        return svc.maintenance_decide(
            pid,
            request,
            grant=grant,
            plan_sha256=shown["plan_sha256"],
            owner=_owner_act("cli-owner-maintenance"),
        )

    _run(as_json, action)


@app.command("doctor")
def doctor(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Read-only environment and store report."""
    svc = _service()

    def action() -> Any:
        pid = None
        try:
            pid = svc.resolve_project_id(project)
        except PrometheosError:
            if project:
                raise
        return svc.doctor(pid)

    _run(as_json, action)


# ----- CP2: owner decisions ------------------------------------------------------------

decision_app = typer.Typer(
    help="Owner decisions: review exact proposals, grant/revoke capabilities.", no_args_is_help=True
)
context_app = typer.Typer(
    help="Bounded deterministic context (pure queries).", no_args_is_help=True
)
notebook_app = typer.Typer(help="Deterministic notebook views.", no_args_is_help=True)
knowledge_app = typer.Typer(help="Search and links (pure queries).", no_args_is_help=True)
meeting_app = typer.Typer(help="Meeting capture and reconciliation.", no_args_is_help=True)
retention_app = typer.Typer(help="Owner-authorised purge.", no_args_is_help=True)
# A06.1, CP20.3 (decision 0084): planned checks under the owner's verification budget.
verify_app = typer.Typer(
    help="Planned checks and the read-only verification budget.", no_args_is_help=True
)
purge_app = typer.Typer(help="Plan and commit a purge.", no_args_is_help=True)
retention_app.add_typer(purge_app, name="purge")
for sub, name in (
    (decision_app, "decision"),
    (context_app, "context"),
    (notebook_app, "notebook"),
    (knowledge_app, "knowledge"),
    (meeting_app, "meeting"),
    (retention_app, "retention"),
    (verify_app, "verify"),
):
    app.add_typer(sub, name=name)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm_owner(summary: str, purpose: str = "To approve exactly this") -> None:
    """Local owner confirmation: shows the proposal and a fresh code the owner must retype."""
    if not _interactive():
        raise PrometheosError(
            "policy_denied",
            "owner decisions need an interactive terminal (or a capability file from one)",
            {},
        )
    code = secrets.token_hex(3)
    # Every line with its invisible characters escaped: a prompt shows text others can
    # write (commit subjects, branch names, request contents), and the owner approves
    # exactly what it says (CP9 integration review run 2, pre-check round 3 R2-L1).
    sys.stderr.write("\n".join(visible(line) for line in summary.split("\n")) + "\n")
    sys.stderr.write(f"{visible(purpose)}, type {code}: ")
    sys.stderr.flush()
    answer = sys.stdin.readline().strip()
    if answer != code:
        raise PrometheosError("policy_denied", "owner did not confirm", {})


def _review_queue(
    svc: Any, project: str | None, request: str | None, grant: bool, deny: bool, note: str | None
) -> Any:
    """``decision review`` without ``--file``: the pending approval requests (A02, CP10.2).

    Lists them, or with ``--request ID`` shows that request's exact plan (and, for a
    transaction, the same server preview as a file review) and grants or denies it after a
    fresh confirmation code. A grant is held by the server: no secret is printed."""
    pid = svc.resolve_project_id(project)
    _confirm_owner(
        "Unlock the owner review queue of pending approval requests",
        purpose="To show the pending requests with owner visibility",
    )
    if request is None:
        if grant or deny:
            raise PrometheosError("validation_error", "--grant/--deny need --request ID", {})
        return svc.approval_queue(pid, "pending")
    shown = svc.approval_owner_view(pid, request)
    if not (grant or deny):
        return shown
    if grant and deny:
        raise PrometheosError("validation_error", "choose --grant or --deny", {})
    lines = [
        f"Approval request {shown['approval_request_id']} ({shown['operation']}, requested by "
        f"{shown['actor']} at rank {shown['caller_rank']}; expires {shown['expires_at']})",
        "Effects:",
        *[f"  - {s}" for s in shown["summary"]],
        f"Plan digest: {shown['plan_sha256']}",
        _escaped_block("Plan", shown["plan"]),
    ]
    if shown.get("preview"):
        lines.append(_escaped_block("Server preview (owner visibility)", shown["preview"]))
    _confirm_owner("\n".join(lines), purpose=f"To {'grant' if grant else 'deny'} exactly this plan")
    decided = svc.approval_decide(
        pid,
        request,
        grant=grant,
        plan_sha256=shown["plan_sha256"],
        owner=_owner_act("cli-owner-review"),
        note=note,
    )
    if grant:  # review F5: the grant answers its receipt (no secret exists)
        return svc.approval_grant_receipt(pid, request)
    return decided


PLAN_SHOWN_CHARS = 20000


def _escaped_text(text: str) -> str:
    """One line of agent-supplied text for the owner's terminal: every non-printable or
    non-ASCII character (newlines, escape sequences, bidirectional and zero-width marks) is
    shown as an escape, never interpreted (review R2-3)."""
    return "".join(
        ch if ch.isascii() and ch.isprintable() else ch.encode("unicode_escape").decode("ascii")
        for ch in text
    )


def _escaped_block(label: str, value: Any) -> str:
    """JSON with every non-ASCII character escaped (so bidirectional or zero-width characters
    in agent text cannot reorder or hide what the owner reads, review F8); a plan longer than
    the terminal block says so explicitly instead of being cut silently."""
    text = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
    if len(text) <= PLAN_SHOWN_CHARS:
        return f"{label}:\n{text}"
    return (
        f"{label} (TRUNCATED: showing {PLAN_SHOWN_CHARS} of {len(text)} characters; read the "
        f"whole plan with `prometheos approval get` or in the hub before deciding):\n"
        f"{text[:PLAN_SHOWN_CHARS]}\n[... {len(text) - PLAN_SHOWN_CHARS} more characters]"
    )


@decision_app.command("review")
def decision_review(
    file: Path = typer.Option(None, "--file", help="Request JSON file to review."),
    request: str = typer.Option(None, "--request", help="Pending approval request ID."),
    grant_it: bool = typer.Option(False, "--grant", help="Grant the --request plan."),
    deny_it: bool = typer.Option(False, "--deny", help="Deny the --request plan."),
    note: str = typer.Option(None, "--note", help="Decision note kept with the request."),
    apply_now: bool = typer.Option(False, "--apply", help="Apply immediately after approval."),
    ttl: int = typer.Option(600, "--ttl", help="Capability lifetime in seconds."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner review. With --file: show a proposal's protected effects, then grant or apply
    it. Without --file: the queue of pending approval requests; --request ID shows one and
    --grant/--deny decides it (each step needs a fresh confirmation code)."""
    svc = _service()

    def action() -> Any:
        # The preview runs with owner visibility, so nothing about it may reach a
        # non-interactive caller (an agent): refuse before computing anything.
        if not _interactive():
            raise PrometheosError(
                "policy_denied",
                "owner decisions need an interactive terminal (or a capability file from one)",
                {},
            )
        if file is None:
            return _review_queue(svc, project, request, grant_it, deny_it, note)
        raw = file.read_bytes()
        pid = svc.resolve_project_id(project or _peek_project(raw))
        # Two owner confirmations, each with a fresh code. The first unlocks the
        # owner-visibility preview (an unconfirmed session learns nothing from the
        # store); the second approves exactly the digest after the owner has seen the
        # protected effects, so nothing is granted for effects the owner has not read.
        _confirm_owner(
            "Unlock the owner preview of this request:\n" + _ops_summary(raw),
            purpose="To show this request's effects with owner visibility",
        )
        owner = Caller(actor="cli-owner-review", scope="owner")
        preview = svc.review_request(pid, raw, owner)
        if not preview["would_apply"]:
            return {"preview": preview, "granted": False}
        if not preview["protected_effects"]:
            return {
                "preview": preview,
                "granted": False,
                "note": "no protected effects; apply needs no owner decision",
            }
        verb = "apply" if apply_now else "grant a single-use capability for"
        _confirm_owner(
            "Protected effects:\n  - "
            + "\n  - ".join(preview["protected_effects"])
            + f"\nRequest digest: {preview['digest']}",
            purpose=f"To {verb} exactly this request",
        )
        grant = svc.grant(
            pid,
            operation="exact_request",
            digest=preview["digest"],
            ttl_seconds=ttl,
            max_uses=1,
            label="cli decision review",
        )
        if apply_now:
            owner = Caller(actor="cli-owner-review", scope="owner")
            return {"preview": preview, "result": svc.apply(pid, raw, owner, grant.secret)}
        sys.stderr.write(
            "Capability secret (shown once; hand it to the client as --capability-file):\n"
            + grant.secret
            + "\n"
        )
        return {
            "preview": preview,
            "granted": True,
            "capability_id": grant.capability_id,
            "expires_at": grant.expires_at,
        }

    _run(as_json, action)


@decision_app.command("preview")
def decision_preview_cmd(
    file: Path = typer.Option(..., "--file", help="Request JSON file to preview."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Pure dry run at agent visibility: protected effects, whether it would apply and the
    plan digest an owner approves. Stores nothing (the owner's view is `decision review`)."""
    svc = _service()

    def action() -> Any:
        raw = file.read_bytes()
        pid = svc.resolve_project_id(project or _peek_project(raw))
        return svc.review_request(pid, raw, Caller(actor="cli"))

    _run(as_json, action)


@decision_app.command("grant")
def decision_grant(
    operation: str = typer.Option(..., "--operation", help="read_sensitive | full_data_export"),
    ttl: int = typer.Option(600, "--ttl"),
    uses: int = typer.Option(20, "--uses"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Grant a narrow, expiring owner capability after interactive confirmation."""
    svc = _service()

    def action() -> Any:
        if operation not in ("read_sensitive", "full_data_export"):
            raise PrometheosError(
                "validation_error",
                "only read_sensitive or full_data_export can be granted without a proposal",
                {},
            )
        from prometheos.domain.models import CapabilityGrant, CapabilityGrantReceipt

        asked = CapabilityGrant(operation=operation, digest=None, ttl_seconds=ttl, max_uses=uses)
        pid = svc.resolve_project_id(project)
        _confirm_owner(f"Grant {operation} for {ttl}s, at most {uses} uses, on {pid}")
        grant = svc.grant(
            pid,
            operation=asked.operation,
            digest=None,
            ttl_seconds=asked.ttl_seconds,
            max_uses=asked.max_uses,
            label=f"cli grant {operation}",
        )
        sys.stderr.write("Capability secret (shown once):\n" + grant.secret + "\n")
        receipt = CapabilityGrantReceipt(
            capability_id=grant.capability_id, operation=grant.operation, digest=grant.digest,
            expires_at=grant.expires_at, max_uses=grant.max_uses, server_held=False,
            approval_request_id=None,
        )  # fmt: skip
        return receipt.model_dump(mode="json")

    _run(as_json, action)


@verify_app.command("plan")
def verify_plan(
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Planned checks cheapest first, with whether an agent may start each within the budget."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.verification_plan(pid, _caller(svc, pid, scope, capability_file))

    _run(as_json, action)


@verify_app.command("budget")
def verify_budget(
    each: int = typer.Option(None, "--each", help="Minutes a single check may take."),
    day: int = typer.Option(None, "--day", help="Minutes of checks an agent may start a day."),
    folder: str = typer.Option(
        None, "--folder", help="Output folder as ROOT:PATH inside an approved research root."
    ),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner: set the read-only verification budget (A06.1). Shows the change and needs a fresh
    confirmation code; an agent can only request it (approval_required)."""
    svc = _service()

    def action() -> Any:
        if not _interactive():
            raise PrometheosError(
                "policy_denied",
                "owner decisions need an interactive terminal (or a capability file from one)",
                {},
            )
        pid = svc.resolve_project_id(project)
        owner = Caller(actor="cli-owner-review", scope="owner")
        current = svc.get(pid, owner, pid, None)
        budget = dict(current["body"].get("verification_budget") or {})
        if each is not None:
            budget["max_minutes_each"] = each
        if day is not None:
            budget["max_minutes_per_day"] = day
        if folder is not None:
            alias, _, path = folder.partition(":")
            budget["output_root_alias"], budget["output_path"] = alias or None, path or None
        # The same request the hub's budget control previews and applies.
        raw = svc.verification_budget_request(pid, budget)
        preview = svc.review_request(pid, raw, owner)
        if not preview["would_apply"]:
            # Review cp20-3-a-r2 R2-4: never ask to confirm a change that cannot apply.
            raise PrometheosError(
                "validation_error", "this budget cannot be applied", {"preview": preview}
            )
        _confirm_owner(
            "Protected effects:\n  - "
            + "\n  - ".join(preview["protected_effects"])
            + f"\nRequest digest: {preview['digest']}",
            purpose="To change the budget exactly so",
        )
        grant = svc.grant(
            pid,
            operation="exact_request",
            digest=preview["digest"],
            ttl_seconds=60,
            max_uses=1,
            label="cli verify budget",
        )
        return svc.apply(pid, raw, owner, grant.secret)

    _run(as_json, action)


@verify_app.command("propose")
def verify_propose(
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Run PrometheOS's check detector (records proposed checks; starts nothing)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.verification_propose(pid, _caller(svc, pid, scope, capability_file))

    _run(as_json, action)


@decision_app.command("cards")
def decision_cards_cmd(
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Pending approval requests that carry a decision card (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.decision_cards(pid, _caller(svc, pid, scope, capability_file))

    _run(as_json, action)


@decision_app.command("apply-recommended")
def decision_apply_recommended(
    request: list[str] = typer.Option(
        None, "--request", help="Approval request ID (repeatable); default: every card listed."
    ),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner: apply the listed card plans as one request, in one transaction (A06.1). Shows
    the set and its digest; each step needs a fresh confirmation code."""
    svc = _service()

    def action() -> Any:
        if not _interactive():
            raise PrometheosError(
                "policy_denied",
                "owner decisions need an interactive terminal (or a capability file from one)",
                {},
            )
        pid = svc.resolve_project_id(project)
        _confirm_owner(
            "Unlock the owner view of the decision cards.",
            purpose="To show the pending cards with owner visibility",
        )
        owner = Caller(actor="cli-owner-review", scope="owner")
        ids = list(request or svc.decision_cards(pid, owner)["recommended"])
        if not ids:
            return {"applied": False, "note": "no decision card is waiting"}
        act = _owner_act("cli-owner-review")
        preview = svc.recommended_preview(pid, owner, ids, act)
        cards = {c["approval_request_id"]: c for c in svc.decision_cards(pid, owner)["cards"]}
        # Review cp20-3-a F3: each card's own protected effects, not only the agent's wording.
        lines = []
        for i in ids:
            if i in cards:
                lines.append(f"  - {cards[i]['card']['what']} ({i}; written by the agent)")
                lines.extend(f"      effect: {e}" for e in cards[i]["protected_effects"])
        _confirm_owner(
            "Apply all recommended, in one transaction:\n"
            + "\n".join(lines)
            + "\nProtected effects of the whole set:\n  - "
            + "\n  - ".join(preview["preview"]["protected_effects"] or ["none"])
            + f"\nRequest digest: {preview['preview']['digest']}",
            purpose="To apply exactly this set",
        )
        return svc.recommended_apply(pid, owner, ids, preview["preview"]["digest"], act)

    _run(as_json, action)


@decision_app.command("revoke")
def decision_revoke(capability_id: str, project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Revoke a capability (always allowed; revocation only removes authority). Given an
    approval request ID (apreq_...), revoke that request's unused grant after a fresh
    confirmation code."""
    svc = _service()

    def action() -> Any:
        from prometheos.domain.models import CapabilityRevokeReceipt

        pid = svc.resolve_project_id(project)
        if capability_id.startswith("apreq_"):
            _confirm_owner(f"Revoke the grant of approval request {capability_id}")
            done = svc.approval_revoke(pid, capability_id, owner=_owner_act("cli-owner-revoke"))
            receipt = CapabilityRevokeReceipt(
                revoked=True, approval_request_id=capability_id, state=done["state"]
            )
            return receipt.model_dump(mode="json")
        # Revoking a capability secret's row only removes authority, so it needs no code.
        return CapabilityRevokeReceipt(revoked=svc.revoke(pid, capability_id)).model_dump(
            mode="json", exclude_none=True
        )

    _run(as_json, action)


# ----- A02, CP11 S11.5: the read-only Zotero adapter (decisions 0069, 0070) -----------------

zotero_app = typer.Typer(
    help="Read-only import from the local Zotero (its local API on this computer; no account).",
    no_args_is_help=True,
)
app.add_typer(zotero_app, name="zotero")


@zotero_app.command("status")
def zotero_status_cmd(
    collections: bool = typer.Option(False, "--collections", help="List the collections too."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Probe Zotero's local API: available, not_running, local_api_disabled or unexpected.
    Never fails: the library works whatever Zotero does."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.zotero_status(pid, caller, collections=collections)

    _run(as_json, action)


@zotero_app.command("import")
def zotero_import_cmd(
    plan: bool = typer.Option(False, "--plan", help="Plan the import (lists the items found)."),
    commit: str = typer.Option(None, "--commit", help="Commit this plan ID."),
    collection: str = typer.Option(None, "--collection", help="Only this collection (its key)."),
    key: list[str] = typer.Option(
        None,
        "--key",
        help="Item key (repeat): with --plan, only these items; with --commit, "
        "commit only these items of the plan.",
    ),
    sensitivity: str = typer.Option("normal", "--sensitivity"),
    no_wait: bool = NoWaitOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Plan importing items from the local Zotero (the library, a collection or item keys),
    then commit the plan. Zotero is only read (GET); each step is an operation run. Planning
    is open to an agent; committing is the owner's decision (it reads your reference library
    into the project): confirm it in an interactive terminal, otherwise it becomes an approval
    request (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        if plan == bool(commit):
            raise PrometheosError("validation_error", "use exactly one of --plan or --commit", {})
        keys = list(key or []) or None
        if commit:
            if collection:
                raise PrometheosError("validation_error", "--collection is for --plan", {})
            if not _interactive():
                params: dict[str, Any] = {"plan_id": commit}
                if keys:
                    params["item_keys"] = keys
                _request_approval(svc, pid, Caller("cli"), "zotero_import_commit", params)
            from prometheos.library import zotero as zotero_lib

            shown = zotero_lib.plan_for(svc.open(pid), commit, Caller("cli", "owner").rank)
            chosen = zotero_lib.selected(shown, keys)
            _confirm_owner(
                f"Read {len(chosen)} item(s) from your local Zotero library into the project "
                f"({shown['scope']['kind']}): "
                + "; ".join(str(i.get("title") or i["key"])[:60] for _, i in chosen[:3]),
                purpose="To import exactly these items",
            )
            svc.worker_args = _worker_args(scope, capability_file)
            return svc.zotero_import_commit(
                pid, commit, Caller("cli", "owner"), authority=_owner_act("cli-owner-zotero"),
                item_keys=keys, mode=_mode(no_wait),
            )  # fmt: skip
        caller = _caller(svc, pid, scope, capability_file)
        svc.worker_args = _worker_args(scope, capability_file)
        return svc.zotero_import_plan(
            pid, caller, collection=collection, item_keys=keys,
            sensitivity=sensitivity, mode=_mode(no_wait),
        )  # fmt: skip

    _run(as_json, action)


# ----- A02, CP11 S11.4: evidence matrices and grounding (decision 0064) ---------------------

matrix_app = typer.Typer(
    help="Evidence matrices: grounded cells over library works, rule extraction, briefs.",
    no_args_is_help=True,
)
app.add_typer(matrix_app, name="matrix")


def _matrix_json(path: Path, what: str) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PrometheosError("io_error", f"cannot read the {what} file", {}) from exc
    if len(raw) > 1024 * 1024:
        raise PrometheosError("budget_exceeded", f"the {what} file is over 1 MiB", {})
    try:
        return loads_strict(raw)
    except ValueError as exc:
        raise PrometheosError(
            "validation_error", f"the {what} file is not JSON: {exc}", {}
        ) from exc


@matrix_app.command("list")
def matrix_list_cmd(
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """The evidence matrices with their counts (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.matrix_list(pid, _caller(svc, pid, scope, capability_file))

    _run(as_json, action)


@matrix_app.command("show")
def matrix_show_cmd(
    matrix_id: str = typer.Argument(..., help="Matrix ID."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One matrix: rows with access states, cells, evidence, flags and briefs (read-only)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.matrix_show(pid, _caller(svc, pid, scope, capability_file), matrix_id)

    _run(as_json, action)


@matrix_app.command("create")
def matrix_create_cmd(
    title: str = typer.Option(..., "--title"),
    columns_file: Path = typer.Option(
        ..., "--columns", help="JSON list of {id, label, value_kind, instructions, choices?}."
    ),
    work: list[str] = typer.Option(None, "--work", help="A work ID (repeat for each)."),
    question: str = typer.Option(None, "--question", help="A question entry ID."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Create a matrix over works (rubric version 1)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        columns = _matrix_json(columns_file, "columns")
        if not isinstance(columns, list):
            raise PrometheosError("validation_error", "--columns holds a JSON list", {})
        return svc.matrix_create(
            pid, _caller(svc, pid, scope, capability_file), title=title, columns=columns,
            work_ids=list(work or []), question_id=question, request_id=request_id,
        )  # fmt: skip

    _run(as_json, action)


@matrix_app.command("propose")
def matrix_propose_cmd(
    matrix_id: str = typer.Argument(..., help="Matrix ID."),
    work: str = typer.Option(..., "--work", help="Work ID (the row)."),
    column: str = typer.Option(..., "--column", help="Column ID."),
    value: str = typer.Option(..., "--value", help="The cell's value, as quoted."),
    evidence_file: Path = typer.Option(
        ..., "--evidence", help="JSON list of evidence references (from library search/read)."
    ),
    quantity: list[str] = typer.Option(
        None, "--quantity", help="'DECIMAL' or 'DECIMAL UNIT', bound in the first evidence."
    ),
    basis: str = typer.Option(None, "--basis", help="full_text, abstract or metadata."),
    relies_on: list[str] = typer.Option(None, "--relies-on", help="Claim or assumption ID."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Propose one cell quoted from its evidence (grounded; numbers verbatim, no tolerance)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        evidence = _matrix_json(evidence_file, "evidence")
        if not isinstance(evidence, list):
            raise PrometheosError("validation_error", "--evidence holds a JSON list", {})
        quantities = []
        for item in quantity or []:
            decimal, _, unit = item.strip().partition(" ")
            quantities.append({"decimal": decimal, "unit": unit.strip()})
        return svc.matrix_propose(
            pid, _caller(svc, pid, scope, capability_file), matrix_id=matrix_id, work_id=work,
            column_id=column, value_text=value, evidence=evidence, quantities=quantities,
            basis=basis, relies_on=list(relies_on or []), request_id=request_id,
        )  # fmt: skip

    _run(as_json, action)


@matrix_app.command("review")
def matrix_review_cmd(
    matrix_id: str = typer.Argument(..., help="Matrix ID."),
    work: str = typer.Option(..., "--work", help="Work ID (the row)."),
    column: list[str] = typer.Option(None, "--column", help="Column to review (default: all)."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: review a row's cells. Without an interactive terminal it becomes an
    approval request in the project (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        columns = list(column) if column else None
        if not _interactive():
            return svc.matrix_review(
                pid, Caller("cli"), matrix_id=matrix_id, work_id=work, column_ids=columns,
                request_id=request_id,
            )  # fmt: skip
        _confirm_owner(
            f"Review row {work} of matrix {matrix_id}: "
            + (", ".join(columns) if columns else "every proposed cell")
        )
        return svc.matrix_review(
            pid, Caller("cli"), matrix_id=matrix_id, work_id=work, column_ids=columns,
            authority=_owner_act("cli-owner-matrix"), request_id=request_id,
        )  # fmt: skip

    _run(as_json, action)


@matrix_app.command("unsupported")
def matrix_unsupported_cmd(
    matrix_id: str = typer.Argument(..., help="Matrix ID."),
    work: str = typer.Option(..., "--work", help="Work ID (the row)."),
    column: str = typer.Option(..., "--column", help="Column ID."),
    reason: str = typer.Option(..., "--reason", help="Why nothing was found."),
    basis: str = typer.Option(None, "--basis", help="full_text, abstract or metadata."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Mark a cell unsupported (no value claim); over a reviewed cell an approval request."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.matrix_unsupported(
            pid, _caller(svc, pid, scope, capability_file), matrix_id=matrix_id, work_id=work,
            column_id=column, reason=reason, basis=basis, request_id=request_id,
        )  # fmt: skip

    _run(as_json, action)


@matrix_app.command("extract")
def matrix_extract_cmd(
    matrix_id: str = typer.Argument(..., help="Matrix ID."),
    rule_file: Path = typer.Option(..., "--rule", help='Rule set JSON ({"rules": [...]}).'),
    no_wait: bool = NoWaitOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Run deterministic keyword and pattern rules over the matrix's works (an operation run):
    hits are proposed cells quoting the matched span; no match is unsupported."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        rules = _matrix_json(rule_file, "rule")
        if not isinstance(rules, dict):
            raise PrometheosError("validation_error", "--rule holds a JSON object", {})
        caller = _caller(svc, pid, scope, capability_file)
        svc.worker_args = _worker_args(scope, capability_file)
        return svc.matrix_extract(
            pid, caller, matrix_id=matrix_id, rules=rules, mode=_mode(no_wait)
        )

    _run(as_json, action)


@matrix_app.command("export")
def matrix_export_cmd(
    matrix_id: str = typer.Argument(..., help="Matrix ID."),
    fmt: str = typer.Option("markdown", "--format", help="csv, markdown (the brief) or json."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Export a matrix. The Markdown brief is registered as a generated version."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.matrix_export(
            pid, _caller(svc, pid, scope, capability_file), matrix_id, fmt, request_id=request_id
        )

    _run(as_json, action)


# ----- CP10.2: the agent side of pending approvals (A02; decision 0016) -------------------

approval_app = typer.Typer(
    help=(
        "Pending approvals, agent side: list, get, resume and withdraw. The owner grants or "
        "denies in the hub queue or `decision review`."
    ),
    no_args_is_help=True,
)
app.add_typer(approval_app, name="approval")


@approval_app.command("list")
def approval_list_cmd(
    state: str = typer.Option(None, "--state", help="pending, granted, used, denied, ..."),
    limit: int = typer.Option(100, "--limit"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Approval requests made at or below your scope (expired ones are marked expired)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.approval_list(pid, _caller(svc, pid, scope, capability_file), state, limit)

    _run(as_json, action)


@approval_app.command("get")
def approval_get_cmd(
    approval_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One approval request: its exact plan, state, decision and (once used) result."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.approval_get(pid, _caller(svc, pid, scope, capability_file), approval_id)

    _run(as_json, action)


@approval_app.command("resume")
def approval_resume_cmd(
    approval_id: str,
    max_seconds: float = typer.Option(
        None, "--max-seconds", help="Recordings only: stop automatically after this long."
    ),
    backend: str = typer.Option(None, "--backend", help="Recordings only: capture backend."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Execute a granted request's stored plan exactly once (the grant stays server-held).
    A used request returns its stored result; pending, denied, expired, withdrawn and
    revoked requests do not run. A granted recording captures here until it stops."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        chosen = None
        if backend is not None:
            from prometheos.audio import capture

            chosen = capture.get_backend(backend)
        host = None
        shown = svc.approval_get(pid, caller, approval_id)
        if shown["operation"] == "recording_start" and shown["state"] == "granted":
            # Review F1: capture only where the owner sees it. An interactive terminal at the
            # computer, after a fresh code; otherwise the owner starts it in the hub.
            plan = shown["plan"]
            # Review R2-3: agent-supplied text is shown escaped (no control, ANSI or bidi).
            title = _escaped_text(str(plan["title"]))
            devices = ", ".join(_escaped_text(str(d)) for d in plan["device_ids"])
            _confirm_owner(
                f"Start the granted recording '{title}' from {devices} for at most "
                f"{int(plan['max_seconds'])} s here",
                purpose="To start this recording in this terminal",
            )
            host = _owner_act("cli-owner-recording")
        done: dict[str, Any] = svc.approval_resume(
            pid, caller, approval_id, backend=chosen, start_capture=False, capture_owner=host
        )
        result = done.get("result") or {}
        if done["operation"] == "recording_start" and not done["replayed"]:
            sys.stderr.write(f"RECORDING {result.get('session_id')}\nCtrl-C stops and saves.\n")
            limit = float(result["max_seconds"])
            final = svc.recording_run(
                pid,
                result["session_id"],
                chosen,
                max_seconds=min(limit, max_seconds) if max_seconds else limit,
            )
            sys.stderr.write(f"RECORDING {final.get('state')}\n")
            # Review R2-7: the owner start answers the approval_view contract, like the hub.
            view: dict[str, Any] = svc.approval_get(pid, caller, approval_id)
            return view
        return done

    _run(as_json, action)


@approval_app.command("stop")
def approval_stop_cmd(
    approval_id: str,
    cancel: bool = typer.Option(False, "--cancel", help="Cancel instead of stop-and-save."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Stop (or cancel) the recording your approved request started."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.approval_stop(pid, caller, approval_id, cancel)

    _run(as_json, action)


@approval_app.command("withdraw")
def approval_withdraw_cmd(
    approval_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Withdraw your own request (a granted one loses its grant)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.approval_withdraw(pid, _caller(svc, pid, scope, capability_file), approval_id)

    _run(as_json, action)


def _ops_summary(raw: bytes) -> str:
    try:
        value = loads_strict(raw)
        ops = value.get("operations", []) if isinstance(value, dict) else []
    except ValueError:
        return "  (unparseable request)"
    lines = [
        f"  - {o.get('op')} {o.get('kind', '')} {o.get('id', '')}".rstrip()
        for o in ops
        if isinstance(o, dict)
    ]
    return "\n".join(lines[:50]) or "  (no operations)"


def _peek_project(raw: bytes) -> str | None:
    try:
        value = loads_strict(raw)
    except ValueError:
        return None
    return value.get("project_id") if isinstance(value, dict) else None


# ----- CP2: context, notebook, knowledge -------------------------------------------------


@context_app.command("show")
def context_show(
    purpose: str = typer.Option("orient", "--purpose"),
    budget: int = typer.Option(12000, "--budget"),
    at_seq: int = typer.Option(None, "--at-seq"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Bounded context with provenance/coverage manifest (agent scope)."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.context(
            svc.resolve_project_id(project),
            Caller("cli"),
            purpose=purpose,
            budget=budget,
            at_seq=at_seq,
        ),
    )


@context_app.command("next")
def context_next(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Deterministic next-step candidates; never adopts anything."""
    svc = _service()
    _run(as_json, lambda: svc.next_steps(svc.resolve_project_id(project), Caller("cli")))


@context_app.command("agenda")
def context_agenda(
    meeting: str = typer.Option(..., "--meeting"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Prior commitments, unresolved questions and proposals for a meeting."""
    svc = _service()
    _run(as_json, lambda: svc.agenda(svc.resolve_project_id(project), Caller("cli"), meeting))


@notebook_app.command("show")
def notebook_show(
    subject: str = typer.Option(None, "--subject"),
    at_seq: int = typer.Option(None, "--at-seq"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Chronological deterministic notebook (agent scope)."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.notebook(
            svc.resolve_project_id(project), Caller("cli"), subject=subject, at_seq=at_seq
        ),
    )


@notebook_app.command("export")
def notebook_export(
    out: Path = typer.Option(..., "--out", help="New file to write (never overwritten)."),
    fmt: str = typer.Option("md", "--format", help="md or json"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Write the notebook to exactly one new file."""
    svc = _service()

    def action() -> Any:
        if fmt not in ("md", "json"):
            raise PrometheosError("validation_error", "format must be md or json", {})
        if out.exists():
            raise PrometheosError("validation_error", "output file already exists", {})
        view = svc.notebook(svc.resolve_project_id(project), Caller("cli"))
        from prometheos.app.views import notebook_markdown

        text = notebook_markdown(view) if fmt == "md" else json.dumps(view, indent=2)
        with open(out, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return {
            "written": str(out),
            "entries": len(view["entries"]),
            "project_seq": view["project_seq"],
        }

    _run(as_json, action)


@knowledge_app.command("search")
def knowledge_search(
    query: str,
    limit: int = typer.Option(20, "--limit"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Keyword search (agent scope)."""
    svc = _service()
    _run(as_json, lambda: svc.search(svc.resolve_project_id(project), Caller("cli"), query, limit))


@knowledge_app.command("status")
def knowledge_status(
    paper: str = typer.Option(None, "--paper", help="Only this paper's claims."),
    claim: list[str] = typer.Option(None, "--claim", help="Only these claims (repeatable)."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Each claim's derived status with its causes and basis (A06.1). Read-only; never support."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.claim_status(
            pid, _caller(svc, pid, scope, capability_file), claim_ids=claim or None, paper_id=paper
        )

    _run(as_json, action)


@knowledge_app.command("map")
def knowledge_map(
    paper: str = typer.Option(None, "--paper", help="Only this deliverable."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """The research map (A06.1): objective, deliverables and their stages, questions, claims
    and tracks. Read-only."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.research_map(pid, _caller(svc, pid, scope, capability_file), paper_id=paper)

    _run(as_json, action)


@knowledge_app.command("links")
def knowledge_links(record_id: str, project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Outgoing references, backlinks and relations of one record."""
    svc = _service()
    _run(as_json, lambda: svc.links(svc.resolve_project_id(project), Caller("cli"), record_id))


# ----- CP2: meetings and purge -------------------------------------------------------


@meeting_app.command("capture")
def meeting_capture(
    title: str = typer.Option(..., "--title"),
    notes: Path = typer.Option(..., "--notes", help="UTF-8 notes file, or - for stdin."),
    held_at: str = typer.Option(None, "--held-at", help="When it happened (UTC RFC3339)."),
    sensitivity: str = typer.Option("normal", "--sensitivity"),
    request_id: str = typer.Option(None, "--request-id", help="Reuse to retry safely."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Retain typed notes as a source and create the meeting record (one commit).

    Agent scope by default; a retry at a different scope conflicts instead of replaying."""
    svc = _service()

    def action() -> Any:
        text = sys.stdin.read() if str(notes) == "-" else notes.read_text(encoding="utf-8")
        if request_id is not None and not is_request_id(request_id):
            raise PrometheosError("validation_error", "request id must be a UUID4", {})
        pid = svc.resolve_project_id(project)
        return svc.capture_meeting(
            pid,
            _caller(svc, pid, scope, capability_file),
            title=title,
            text=text,
            held_at=held_at,
            sensitivity=sensitivity,
            request_id=request_id,
        )

    _run(as_json, action)


@meeting_app.command("reconcile")
def meeting_reconcile(
    meeting: str = typer.Option(..., "--meeting"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Show proposed items beside current actions; never merges or adopts."""
    svc = _service()
    _run(as_json, lambda: svc.reconcile(svc.resolve_project_id(project), Caller("cli"), meeting))


@purge_app.command("plan")
def purge_plan_cmd(
    ids: list[str] = typer.Argument(..., help="Records to purge (closure is computed)."),
    capability_file: Path = CapabilityOpt,
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Compute the purge closure and limitations (owner scope; no deletion)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        owner = svc.owner_caller(pid, _read_secret(capability_file), "read_sensitive")
        return svc.purge_plan(pid, list(ids), owner)

    _run(as_json, action)


@record_app.command("history")
def record_history(
    record_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Revision metadata plus recorded transition reasons and evidence."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.history(pid, _caller(svc, pid, scope, capability_file), record_id)

    _run(as_json, action)


@purge_app.command("commit")
def purge_commit_cmd(
    plan_id: str,
    capability_file: Path = CapabilityOpt,
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Purge after owner confirmation (interactive) or with a purge capability."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        secret = _read_secret(capability_file)
        if secret is None:
            if not _interactive():
                _request_approval(
                    svc, pid, Caller("cli"), "retention_purge_commit", {"plan_id": plan_id}
                )
            plan = svc.purge_plan_get(pid, plan_id, Caller(actor="cli-owner-purge", scope="owner"))
            _confirm_owner(
                f"Purge {len(plan['closure'])} record(s), {len(plan['payloads'])} payload(s), "
                f"{len(plan['media'])} media file(s).\n" + "\n".join(plan["limitations"])
            )
            secret = svc.grant(
                pid, operation="purge", digest=plan["plan_sha256"], label="cli purge confirmation"
            ).secret
        return svc.purge_commit(pid, plan_id, secret)

    _run(as_json, action)


# ----- CP3: tasks, MCP, skills, host setup ------------------------------------------------

task_app = typer.Typer(help="Bounded external-agent tasks.", no_args_is_help=True)
mcp_app = typer.Typer(help="MCP server (official SDK).", no_args_is_help=True)
skill_app = typer.Typer(help="Agent Skills shipped with PrometheOS.", no_args_is_help=True)
host_app = typer.Typer(help="Project-local coding-host setup (never global).", no_args_is_help=True)
for sub, name in ((task_app, "task"), (mcp_app, "mcp"), (skill_app, "skill"), (host_app, "host")):
    app.add_typer(sub, name=name)


@task_app.command("prepare")
def task_prepare_cmd(
    purpose: str = typer.Option(..., "--purpose", help="meeting_extract|notebook_draft|next_steps"),
    meeting: str = typer.Option(None, "--meeting"),
    budget: int = typer.Option(12000, "--budget"),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Persist a task (context, input bindings, output schema) before returning its ID."""
    svc = _service()

    def action() -> Any:
        rid = request_id or new_request_id()
        if not is_request_id(rid):
            raise PrometheosError("validation_error", "request id must be a UUID4", {})
        return svc.task_prepare(
            svc.resolve_project_id(project),
            purpose=purpose,
            budget=budget,
            meeting_id=meeting,
            request_id=rid,
        )

    _run(as_json, action)


@task_app.command("get")
def task_get_cmd(
    task_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Show a task with its persisted context and output schema (agent view by default)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.task_get(pid, task_id, _caller(svc, pid, scope, capability_file))

    _run(as_json, action)


@task_app.command("submit")
def task_submit_cmd(
    task_id: str,
    file: Path = typer.Option(..., "--file", help="Result JSON, or - for stdin."),
    request_id: str = typer.Option(..., "--request-id", help="UUID4; reuse it to retry safely."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Submit a structured proposal; stale or cancelled tasks are refused."""
    svc = _service()

    def action() -> Any:
        if not is_request_id(request_id):
            raise PrometheosError("validation_error", "request id must be a UUID4", {})
        raw = sys.stdin.buffer.read() if str(file) == "-" else file.read_bytes()
        return svc.task_submit(svc.resolve_project_id(project), task_id, raw, request_id)

    _run(as_json, action)


@task_app.command("cancel")
def task_cancel_cmd(
    task_id: str,
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Cancel a prepared task."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.task_cancel(
            svc.resolve_project_id(project), task_id, request_id or new_request_id()
        ),
    )


@mcp_app.command("serve")
def mcp_serve(
    stdio: bool = typer.Option(False, "--stdio", help="Serve over stdin/stdout."),
    maintenance: bool = typer.Option(
        False,
        "--maintenance",
        help="Serve only the maintenance tools and open no project database (also chosen "
        "automatically when the project store cannot be opened).",
    ),
    project: str = ProjectOpt,
) -> None:
    """Serve one project over MCP stdio with agent scope."""
    if not stdio:
        typer.echo("only --stdio is supported", err=True)
        raise typer.Exit(2)
    from prometheos.mcp_adapter import serve_stdio

    svc = _service()
    try:
        pid = svc.resolve_project_id(project)
        svc.actor = "mcp"
        serve_stdio(svc, pid, maintenance=maintenance)
    except PrometheosError as exc:
        _fail(exc, False)


@skill_app.command("list")
def skill_list(as_json: bool = JsonOpt) -> None:
    """List bundled original skills and the imported packages in the local catalog."""
    from prometheos.skills import bundled_skills

    svc = _service()
    _run(as_json, lambda: {"skills": bundled_skills(), **svc.skill_catalog()})


@skill_app.command("validate")
def skill_validate(path: Path, as_json: bool = JsonOpt) -> None:
    """Validate a skill directory against the Agent Skills specification."""
    from prometheos.skills import validate_skill

    def action() -> Any:
        errors = validate_skill(path)
        if errors:
            raise PrometheosError("validation_error", "skill is not valid", {"errors": errors})
        return {"valid": True, "skill": path.name}

    _run(as_json, action)


@host_app.command("setup")
def host_setup(
    host: str = typer.Option(
        "claude-code", "--host", help="claude-code | opencode | claude-code-plugin"
    ),
    target: Path = typer.Option(
        ...,
        "--target",
        help="The coding project folder (project-local), or for claude-code-plugin the folder "
        "that receives the local plugin marketplace.",
    ),
    apply_now: bool = typer.Option(False, "--apply", help="Write the planned files."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Show (default) or write the project-local MCP entry and skills for a coding host."""
    from prometheos.skills import host_apply, host_plan

    svc = _service()

    def action() -> Any:
        project_id = svc.resolve_project_id(project)
        plan = host_plan(host, target, project_id)
        written = host_apply(plan) if apply_now else []
        if apply_now and host == "claude-code-plugin":
            from prometheos.agent_package import record_setup

            record_setup(svc.home, Path(plan["target"]), project_id)
        return {k: v for k, v in plan.items() if k != "_contents"} | {
            "applied": apply_now,
            "written": written,
        }

    _run(as_json, action)


# ----- CP4: skill packages and recipe runs ---------------------------------------------------

workflow_app = typer.Typer(
    help="Recipe runs: render, start and record host-executed steps.", no_args_is_help=True
)
app.add_typer(workflow_app, name="workflow")


def _json_file(path: Path | None) -> Any:
    if path is None:
        return {}
    raw = sys.stdin.buffer.read() if str(path) == "-" else path.read_bytes()
    try:
        return loads_strict(raw)
    except ValueError as exc:
        raise PrometheosError("validation_error", f"not valid JSON: {exc}"[:300], {}) from exc


def _optional_project(svc: Any, project: str | None) -> str | None:
    return svc.resolve_project_id(project) if project else None


PlanOpt = typer.Option(None, "--plan-digest", help="Digest of the plan you reviewed; applies it.")
HostOpt = typer.Option("claude-code", "--host", help="claude-code | opencode")
TargetOpt = typer.Option(..., "--target", help="The coding project folder (project-local).")
RecordOpt = typer.Option(
    None, "--project", help="Also record the installation in this PrometheOS project."
)


@skill_app.command("create")
def skill_create_cmd(
    name: str,
    description: str = typer.Option(..., "--description"),
    license_expression: str = typer.Option(
        ..., "--license", help="SPDX expression you choose, or 'unresolved'."
    ),
    out: Path = typer.Option(Path("."), "--out"),
    as_json: bool = JsonOpt,
) -> None:
    """Scaffold an original skill package (SKILL.md, references, examples, sidecar)."""
    svc = _service()
    chosen = None if license_expression == "unresolved" else license_expression
    _run(as_json, lambda: svc.skill_create(name, description, chosen, out))


@skill_app.command("inspect")
def skill_inspect_cmd(source: Path, as_json: bool = JsonOpt) -> None:
    """Static inspection of a package folder or zip (nothing is executed or copied)."""
    svc = _service()
    _run(as_json, lambda: svc.skill_inspect(source))


@skill_app.command("plan")
def skill_plan_cmd(source: Path, as_json: bool = JsonOpt) -> None:
    """Plan importing a package into the local catalog; prints the digest to approve."""
    svc = _service()
    _run(as_json, lambda: svc.skill_import_plan(source))


@skill_app.command("import")
def skill_import_cmd(source: Path, plan: str = PlanOpt, as_json: bool = JsonOpt) -> None:
    """Import exactly the reviewed plan into the local catalog."""
    svc = _service()
    _run(as_json, lambda: svc.skill_import(source, plan))


@skill_app.command("catalog")
def skill_catalog_cmd(as_json: bool = JsonOpt) -> None:
    """List imported packages, their status, trust labels and the revocation list."""
    svc = _service()
    _run(as_json, svc.skill_catalog)


def _install(
    ref: str, host: str, target: Path, plan: str | None, resolve: str | None,
    project: str | None, as_json: bool, operation: str = "skill_install",
) -> None:  # fmt: skip
    if resolve not in (None, "keep-local", "take-package"):
        raise typer.BadParameter("--resolve must be keep-local or take-package")
    svc = _service()
    _run(
        as_json,
        lambda: svc.skill_install(
            ref,
            host,
            target,
            plan,
            resolve,
            _optional_project(svc, project),
            authority=_skill_projection_authority(
                svc,
                operation,
                target,
                project,
                plan,
                {"ref": ref, "host": host, "resolve": resolve},
            ),
        ),
    )


@skill_app.command("install")
def skill_install_cmd(
    ref: str,
    host: str = HostOpt,
    target: Path = TargetOpt,
    plan: str = PlanOpt,
    project: str = RecordOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Plan (default) or apply a project-local projection of a catalog package."""
    _install(ref, host, target, plan, None, project, as_json)


@skill_app.command("update")
def skill_update_cmd(
    ref: str,
    host: str = HostOpt,
    target: Path = TargetOpt,
    plan: str = PlanOpt,
    resolve: str = typer.Option(
        None, "--resolve", help="For local edits: keep-local or take-package."
    ),
    project: str = RecordOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Plan or apply an update to another version; local edits need an explicit choice."""
    _install(ref, host, target, plan, resolve, project, as_json, "skill_update")


@skill_app.command("remove")
def skill_remove_cmd(
    name: str,
    host: str = HostOpt,
    target: Path = TargetOpt,
    plan: str = PlanOpt,
    project: str = RecordOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Plan or apply removal of lock-owned, unmodified files only."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.skill_remove(
            name,
            host,
            target,
            plan,
            _optional_project(svc, project),
            authority=_skill_projection_authority(
                svc, "skill_remove", target, project, plan, {"name": name, "host": host}
            ),
        ),
    )


def _skill_projection_authority(
    svc: Any,
    operation: str,
    target: Path,
    project: str | None,
    digest: str | None,
    params: dict[str, Any],
) -> Any:
    if digest is None:
        return None
    if not _interactive():
        pid = svc.resolve_project_id(project)
        settings = svc.project_info(pid, Caller("cli"))["settings"]
        for root in settings["allowed_roots"]:
            try:
                relative = target.resolve().relative_to(Path(root["path"]).resolve()).as_posix()
            except ValueError:
                continue
            _request_approval(
                svc,
                pid,
                Caller("cli"),
                operation,
                {
                    **params,
                    "root_alias": root["alias"],
                    "folder": None if relative == "." else relative,
                    "plan_sha256": digest,
                    "record_in_project": project is not None,
                },
            )
        raise PrometheosError(
            "policy_denied", "noninteractive skill apply needs a project research root", {}
        )
    if operation == "skill_remove":
        preview = svc.skill_remove(params["name"], params["host"], target, None)["plan"]
    else:
        preview = svc.skill_install(
            params["ref"], params["host"], target, None, params.get("resolve")
        )["plan"]
    entry = svc.catalog.get(preview["package"])
    if preview["plan_sha256"] != digest:
        raise PrometheosError("revision_conflict", "the skill projection plan changed", {})
    from prometheos import packages

    inspection = packages.inspect(svc.catalog.entries(entry), svc.catalog)
    _confirm_owner(
        json.dumps(
            {
                "plan": preview,
                "status": entry["status"],
                "package_digest": entry["package_digest"],
                "files": inspection["files"],
                "findings": inspection["findings"],
            },
            indent=2,
        )
    )
    return _owner_act("cli-owner-skill-projection")


@skill_app.command("verify")
def skill_verify_cmd(ref: str, as_json: bool = JsonOpt) -> None:
    """Recompute catalog digests and licence completeness (nothing is executed)."""
    svc = _service()
    _run(as_json, lambda: svc.skill_verify(ref))


@skill_app.command("revoke")
def skill_revoke_cmd(
    ref: str,
    target: Path = typer.Option(
        None, "--target", help="Also show removal plans for installs in this project folder."
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Add a package to the local revocation list and quarantine it."""
    svc = _service()
    _run(as_json, lambda: svc.skill_revoke(ref, target))


@skill_app.command("trust")
def skill_trust_cmd(ref: str, project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Owner decision: release a quarantined or revoked package after review. Without an
    interactive terminal it becomes an approval request in the project (approval_required)."""
    svc = _service()

    def action() -> Any:
        if not _interactive():
            pid = svc.resolve_project_id(project)
            _request_approval(svc, pid, Caller("cli"), "skill_trust", {"ref": ref})
        report = svc.skill_verify(ref)
        _confirm_owner(
            f"Trust {report['package']} (status {report['status']}); unresolved rights: "
            f"{', '.join(report['unknown_rights']) or 'none'}. Inspection cannot certify safety."
        )
        return svc.skill_trust(ref, authority=_owner_act("cli-owner-trust"))

    _run(as_json, action)


@skill_app.command("export")
def skill_export_cmd(
    ref: str,
    out: Path = typer.Option(..., "--out"),
    draft: bool = typer.Option(False, "--draft", help="Local draft even with unresolved rights."),
    as_json: bool = JsonOpt,
) -> None:
    """Export a portable bundle with lock, notices and modification record."""
    svc = _service()
    _run(as_json, lambda: svc.skill_export(ref, out, draft))


@skill_app.command("adapt")
def skill_adapt_cmd(
    ref: str,
    name: str = typer.Option(..., "--name"),
    out: Path = typer.Option(Path("."), "--out"),
    as_json: bool = JsonOpt,
) -> None:
    """Copy a rights-resolved package into a new local identity with its parent recorded."""
    svc = _service()
    _run(as_json, lambda: svc.skill_adapt(ref, name, out))


@skill_app.command("refresh")
def skill_refresh_cmd(folder: Path, as_json: bool = JsonOpt) -> None:
    """Recompute component digests after editing and record modifications."""
    svc = _service()
    _run(as_json, lambda: svc.skill_refresh(folder))


@workflow_app.command("render")
def workflow_render_cmd(
    ref: str,
    recipe: str,
    inputs: Path = typer.Option(None, "--inputs", help="Inputs JSON file, or - for stdin."),
    as_json: bool = JsonOpt,
) -> None:
    """Validate inputs and render a recipe's steps for the host agent."""
    svc = _service()
    _run(as_json, lambda: svc.workflow_render(ref, recipe, _json_file(inputs)))


@workflow_app.command("start")
def workflow_start_cmd(
    package: str = typer.Option(..., "--package", help="Installed package record ID."),
    recipe: str = typer.Option(..., "--recipe"),
    inputs: Path = typer.Option(None, "--inputs"),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Create a run record for a recipe of an installed package."""
    svc = _service()

    def action() -> Any:
        return svc.workflow_start(
            svc.resolve_project_id(project),
            package_id=package,
            recipe_id=recipe,
            inputs=_json_file(inputs),
            request_id=request_id or new_request_id(),
        )

    _run(as_json, action)


def _parse_artifact(text: str) -> dict[str, Any]:
    record_id, _, rev = text.rpartition(":")
    if not record_id or not rev.isdigit():
        raise PrometheosError("validation_error", "artifacts are RECORD_ID:REV", {})
    return {"id": record_id, "rev": int(rev)}


def _parse_check(text: str) -> dict[str, Any]:
    check_id, _, result = text.partition("=")
    return {"id": check_id, "result": result, "evidence": []}


@workflow_app.command("step")
def workflow_step_cmd(
    run_id: str,
    step: str = typer.Option(..., "--step"),
    status: str = typer.Option(..., "--status", help="pending|in_progress|blocked|done|skipped"),
    base_rev: int = typer.Option(..., "--base-rev"),
    artifact: list[str] = typer.Option([], "--artifact", help="RECORD_ID:REV (repeatable)."),
    check: list[str] = typer.Option([], "--check", help="STEP.N=pass|fail|unknown."),
    reason: str = typer.Option(None, "--reason"),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record host-reported progress for one step (validated against the recipe)."""
    svc = _service()

    def action() -> Any:
        return svc.workflow_step(
            svc.resolve_project_id(project),
            run_id=run_id,
            base_rev=base_rev,
            step_id=step,
            status=status,
            artifact_refs=[_parse_artifact(a) for a in artifact],
            check_results=[_parse_check(c) for c in check],
            reason=reason,
            request_id=request_id or new_request_id(),
        )

    _run(as_json, action)


def _finish(run_id: str, base_rev: int, outcome: str, outputs: Any, request_id: str | None,
            project: str | None, as_json: bool) -> None:  # fmt: skip
    svc = _service()

    def action() -> Any:
        return svc.workflow_finish(
            svc.resolve_project_id(project),
            run_id=run_id,
            base_rev=base_rev,
            outcome=outcome,
            outputs=outputs,
            request_id=request_id or new_request_id(),
        )

    _run(as_json, action)


@workflow_app.command("complete")
def workflow_complete_cmd(
    run_id: str,
    base_rev: int = typer.Option(..., "--base-rev"),
    outputs: Path = typer.Option(..., "--outputs", help="Outputs JSON file, or - for stdin."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Complete a run: every step resolved, outputs matching the recipe schema."""
    _finish(run_id, base_rev, "completed", _json_file(outputs), request_id, project, as_json)


@workflow_app.command("cancel")
def workflow_cancel_cmd(
    run_id: str,
    base_rev: int = typer.Option(..., "--base-rev"),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Cancel a run."""
    _finish(run_id, base_rev, "cancelled", None, request_id, project, as_json)


@workflow_app.command("status")
def workflow_status_cmd(run_id: str, project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Show a run's steps and which steps are ready (agent visibility)."""
    svc = _service()
    agent = Caller(actor="cli", scope="agent")
    _run(as_json, lambda: svc.workflow_status(svc.resolve_project_id(project), run_id, agent))


@workflow_app.command("export")
def workflow_export_cmd(
    run_id: str,
    out: Path = typer.Option(..., "--out"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Write a run (agent visibility) and its recipe to a JSON file; never overwrites."""
    svc = _service()

    def action() -> Any:
        agent = Caller(actor="cli", scope="agent")
        status = svc.workflow_status(svc.resolve_project_id(project), run_id, agent)
        document = {k: status[k] for k in ("run", "recipe", "ready_steps")}
        try:
            with out.open("x", encoding="utf-8", newline="\n") as handle:  # never overwrites
                handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        except FileExistsError as exc:
            raise PrometheosError("revision_conflict", "output file already exists", {}) from exc
        return {"exported": str(out)}

    _run(as_json, action)


# ----- CP5: researcher hub ---------------------------------------------------------------

hub_app = typer.Typer(help="Loopback researcher hub (browser).", no_args_is_help=True)
app.add_typer(hub_app, name="hub")


@hub_app.command("serve")
def hub_serve(
    port: int = typer.Option(0, "--port", help="Loopback port (0 picks a free one)."),
    open_browser: bool = typer.Option(False, "--open", help="Open the hub in the browser."),
    agent_token_file: Path = typer.Option(
        None,
        "--agent-token-file",
        help=(
            "Write a bearer token for a local coding agent to this new file (never "
            "overwritten). It reaches only agent-scoped API routes, at agent scope."
        ),
    ),
    maintenance: bool = typer.Option(
        False,
        "--maintenance",
        help="Serve the recovery screen and the maintenance routes only, opening no project "
        "database (also chosen automatically when the project store cannot be opened).",
    ),
    sidecar: bool = typer.Option(
        False,
        "--sidecar",
        help="Run under the desktop app (CP19.2): one JSON line per event on stdout (ready with "
        "the URL and a pairing code, pairing), a line 'pair' on stdin for a new code, and a "
        "graceful stop when stdin closes. The code goes only to the parent process's pipe.",
    ),
    launched: bool = typer.Option(
        False,
        "--launched",
        hidden=True,
        help="Started by `prometheos start` (A07, CP21): no terminal pairing code; the browser "
        "pairs with a launch code issued over the owner channel.",
    ),
    project: str = ProjectOpt,
) -> None:
    """Serve the hub on 127.0.0.1. The pairing token is shown only in this terminal (or, with
    --sidecar, only to the desktop app that started it)."""
    import socket
    import threading
    import time
    import webbrowser

    import uvicorn

    from prometheos import launcher
    from prometheos.hub.app import create_app
    from prometheos.hub.security import PAIRING_TTL, Gate

    svc = _service()
    home = Path(svc.home)
    # A07 (CP21): one hub process per home. The lock is held for this process's lifetime and
    # released by the operating system if it dies; MCP and CLI writers do not take it.
    hub_lock = launcher.HubLock.acquire(home, wait=2.0)
    if hub_lock is None:
        _fail(
            PrometheosError(
                "busy",
                "a hub is already running for this home; `prometheos start` opens it",
                {"lock": "hub"},
            ),
            False,
        )
    try:
        pid = svc.resolve_project_id(project)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", port))
    except PrometheosError as exc:
        hub_lock.release()
        _fail(exc, False)
    except OSError as exc:
        hub_lock.release()
        _fail(PrometheosError("io_error", f"cannot bind 127.0.0.1:{port}: {exc}", {}), False)
    bound = sock.getsockname()[1]
    # A07, CP21.2: the home's own host name, where a remembered device credential can live.
    from prometheos import devices

    # Only on the home's own fixed port (spec/08: "the home's own host name and port"; review
    # cp21-a F2): a terminal hub on another port serves 127.0.0.1 and localhost only.
    identity = launcher.home_identity(home)
    home_host = (
        devices.host_label(identity["home_id"]) + ".localhost"
        if bound == int(identity["port"])
        else None
    )
    gate = Gate(port=bound, home_host=home_host)
    if agent_token_file is not None:
        try:
            _write_agent_token(agent_token_file, gate)
        except PrometheosError as exc:
            sock.close()
            _fail(exc, False)
        except OSError as exc:
            sock.close()
            _fail(
                PrometheosError("io_error", f"cannot create the agent token file: {exc}", {}), False
            )
    try:
        hub = create_app(svc, pid, gate, maintenance=True if maintenance else None)
    except PrometheosError as exc:
        sock.close()
        _fail(exc, False)
    if hub.state.maintenance:
        sys.stderr.write(
            "\nMaintenance mode: the project store is not opened. The hub shows its recovery "
            "screen (inspect, verify a backup, restore into a new store, switch).\n"
        )
    else:
        svc.run_recover(pid)  # runs a stopped server left behind read truthfully (decision 0017)
        svc.events_detect_at_start(pid)  # one detector pass at start, never on a timer (0035)
    url = f"http://127.0.0.1:{bound}/"

    def emit(document: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(document) + "\n")
        sys.stdout.flush()

    def announce() -> None:
        token = gate.new_pairing_token()
        if sidecar:
            emit({"event": "pairing", "url": url, "project_id": pid, "pairing_code": token})
            return
        sys.stderr.write(
            f"\nPrometheOS hub for {pid}\n  {url}\n"
            f"  Pairing code (one use, {int(PAIRING_TTL // 60)} minutes): {token}\n"
            "  Paste it into the hub page. Press Enter here for a new code; Ctrl+C stops.\n"
        )
        sys.stderr.flush()

    # The owner channel (A07, CP21): launch codes go only to a caller that can read this
    # user-only secret, on a request no browser can make (launcher.owner_channel_ok).
    hub.state.owner_secret = launcher.write_owner_secret(home)
    if not sidecar and not launched:
        announce()  # under the desktop app, only once the server listens (below)
    if not sidecar and not launched and sys.stdin is not None and sys.stdin.isatty():

        def renew() -> None:
            for _ in sys.stdin:
                announce()

        threading.Thread(target=renew, daemon=True).start()
    if open_browser:
        webbrowser.open(url)  # the pairing code is never part of the URL
    config = uvicorn.Config(
        hub,
        log_level="warning",
        access_log=False,
        lifespan="off",
        # Under the desktop app or the launcher, a graceful stop must not wait forever on an
        # open stream (a paired browser's changes long-poll; found in the A1a demo, 0088).
        timeout_graceful_shutdown=5 if sidecar or launched else None,
    )
    server = uvicorn.Server(config)
    if sidecar:
        # Decision 0054 (CP19.2): the desktop app holds this pipe. "pair" asks for a new
        # one-use code (an expired session pairs again); end of input is the app's graceful
        # stop, so open requests finish and the store closes instead of a hard terminate.
        def commands() -> None:
            # The first line goes out once the server listens: the URL it names must answer.
            while not server.started and not server.should_exit:
                time.sleep(0.05)
            if server.started:
                announce()
            for line in sys.stdin:
                if line.strip() == "pair":
                    announce()
            server.should_exit = True

        threading.Thread(target=commands, daemon=True).start()

    def request_stop() -> None:
        server.should_exit = True

    hub.state.request_stop = request_stop
    mode = "maintenance" if hub.state.maintenance else "project"

    def advertise() -> None:
        # Advisory metadata for `status`, written once the server listens; the lock decides.
        while not server.started and not server.should_exit:
            time.sleep(0.05)
        if server.started:
            launcher.write_metadata(home, port=bound, project_id=pid, mode=mode)

    threading.Thread(target=advertise, daemon=True).start()
    try:
        server.run(sockets=[sock])
    finally:
        launcher.clear_hub_files(home)
        hub_lock.release()
    if sidecar:
        emit({"event": "stopped", "project_id": pid})


# ----- A07, CP21: the web-first launcher (decision 0088) -------------------------------------


@app.command("start")
def start_cmd(
    no_open: bool = typer.Option(
        False, "--no-open", help="Start the hub without opening a paired browser."
    ),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Start this home's hub in the background on its fixed port and open a paired browser.
    If the hub already runs, open a paired browser on it (no second hub). The one-use launch
    code goes only to the browser, in the URL fragment; it is never printed."""
    from prometheos import launcher

    def action() -> Any:
        opened = launcher.start(project=project or None, open_browser=not no_open)
        if not as_json:
            state = "already running" if opened.already_running else "started"
            browser = "opened a paired browser" if opened.browser_opened else "no browser opened"
            sys.stderr.write(f"PrometheOS hub {state} at {opened.url} ({browser}).\n")
        return {
            "url": opened.url,
            "already_running": opened.already_running,
            "pid": opened.pid,
            "browser_opened": opened.browser_opened,
        }

    _run(as_json, action)


@app.command("stop")
def stop_cmd(as_json: bool = JsonOpt) -> None:
    """Stop this home's hub (asked over the owner channel; open requests finish first)."""
    from prometheos import launcher

    _run(as_json, launcher.stop)


device_app = typer.Typer(
    help="Remembered browsers of this home (A07, CP21.2): list and revoke.", no_args_is_help=True
)
app.add_typer(device_app, name="device")


@device_app.command("list")
def device_list_cmd(as_json: bool = JsonOpt) -> None:
    """The browsers this home remembers: label, state, last use and expiry (never a secret)."""
    _run(as_json, lambda: _service().device_list())


@device_app.command("revoke")
def device_revoke_cmd(
    device_id: str = typer.Argument(..., help="The device id from `device list`."),
    as_json: bool = JsonOpt,
) -> None:
    """Revoke a remembered browser: its credential stops at once and the hub ends its
    sessions within seconds; it pairs again with `prometheos start`."""
    _run(as_json, lambda: _service().device_revoke(device_id))


@app.command("status")
def status_cmd(as_json: bool = JsonOpt) -> None:
    """Whether this home's hub runs, decided by its lock; process, port and start time are
    advisory metadata."""
    from prometheos import launcher

    _run(as_json, launcher.status)


# ----- CP7: papers and recurring jobs ------------------------------------------------------

paper_app = typer.Typer(
    help="Paper readiness (pure), local bundles and handoff plans.", no_args_is_help=True
)
handoff_app = typer.Typer(
    help="Copy a bundle into a local folder you choose (no Git, no cloud).", no_args_is_help=True
)
job_app = typer.Typer(help="Fixed-handler recurring jobs.", no_args_is_help=True)
jobs_app = typer.Typer(help="Run due job slots and read the job inbox.", no_args_is_help=True)
paper_app.add_typer(handoff_app, name="handoff")
for sub, name in ((paper_app, "paper"), (job_app, "job"), (jobs_app, "jobs")):
    app.add_typer(sub, name=name)
VersionOpt = typer.Option(
    None, "--manuscript-version", help="Manuscript version ID (default: latest)."
)


def _rid(request_id: str | None) -> str:
    rid = request_id or new_request_id()
    if not is_request_id(rid):
        raise PrometheosError("validation_error", "request id must be a UUID4", {})
    return rid


@paper_app.command("check")
def paper_check_cmd(
    paper_id: str,
    save: bool = typer.Option(False, "--save", help="Also record the findings as an entry."),
    request_id: str = typer.Option(None, "--request-id", help="UUID4 for a retry-safe --save."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Readiness findings for one paper. Pure unless --save; never a score or verdict."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        if save:
            if scope != "agent":
                raise PrometheosError("validation_error", "--save records the agent view", {})
            return svc.paper_check_save(pid, paper_id, _rid(request_id))
        return svc.paper_check(pid, _caller(svc, pid, scope, capability_file), paper_id)

    _run(as_json, action)


@paper_app.command("compass")
def paper_compass_cmd(
    paper: str = typer.Option(None, "--paper", help="Only this paper's directions."),
    limit: int = typer.Option(12, "--limit", min=1, max=50),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Where to look next: ranked directions from the record, each with why, what to do and
    when it is done. Pure; never a verdict."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.research_compass(pid, caller, paper_id=paper, limit=limit)

    _run(as_json, action)


@paper_app.command("numbers")
def paper_numbers_cmd(
    paper_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Where every number in the manuscript comes from: macros traced to their file, run and
    claims, claims' bound quantities, and numbers typed by hand. Pure; never a verdict."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.paper_numbers(pid, _caller(svc, pid, scope, capability_file), paper_id)

    _run(as_json, action)


@paper_app.command("get")
def paper_get_cmd(
    paper_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Paper overview: the record, venue requirements, anchors, versions and findings."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.paper_overview(pid, _caller(svc, pid, scope, capability_file), paper_id)

    _run(as_json, action)


@paper_app.command("export")
def paper_export_cmd(
    paper_id: str,
    out: Path = typer.Option(..., "--out", help="New .zip file to write (never overwritten)."),
    version: str = VersionOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Write a local bundle bound to exact manuscript/source versions. Sends nothing."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.paper_export(pid, caller, paper_id, out, version)

    _run(as_json, action)


@paper_app.command("requirements-draft")
def paper_requirements_draft_cmd(
    version_id: str, project: str = ProjectOpt, as_json: bool = JsonOpt
) -> None:
    """Draft unknown requirement rows from list items of a retained venue source version."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.requirements_draft(svc.resolve_project_id(project), Caller("cli"), version_id),
    )


@handoff_app.command("plan")
def paper_handoff_plan_cmd(
    paper_id: str,
    into: Path = typer.Option(..., "--into", help="Existing local folder (e.g. your Git clone)."),
    version: str = VersionOpt,
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Compare the bundle with a folder: new, identical, conflict, folder-only (pure)."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.paper_handoff_plan(
            svc.resolve_project_id(project), Caller("cli"), paper_id, into, version
        ),
    )


@handoff_app.command("apply")
def paper_handoff_apply_cmd(
    paper_id: str,
    into: Path = typer.Option(..., "--into"),
    digest: str = typer.Option(..., "--digest", help="Digest of the reviewed plan."),
    overwrite: list[str] = typer.Option(
        None, "--overwrite", help="A conflicting file to overwrite (repeat for each)."
    ),
    version: str = VersionOpt,
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Write the reviewed plan's new files; never deletes, never runs Git."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.paper_handoff_apply(
            svc.resolve_project_id(project),
            Caller("cli"),
            paper_id,
            into,
            digest,
            list(overwrite or []),
            version,
        ),
    )


def _job_body(
    file: Path | None,
    handler: str | None,
    paper: str | None,
    sources: list[str] | None,
    every: int | None,
    start: str | None,
    weekly: str | None,
    zone: str | None,
    max_inputs: int,
    max_output_chars: int,
) -> Any:
    if file is not None:
        return _json_file(file)
    if handler is None:
        raise PrometheosError("validation_error", "give --file or --handler", {})
    if (every is None) == (weekly is None):
        raise PrometheosError("validation_error", "give exactly one of --every or --weekly", {})
    if every is not None:
        from prometheos.domain.models import utc_now

        schedule: dict[str, Any] = {
            "type": "interval",
            "minutes": every,
            "start_utc": start or utc_now()[:19] + "Z",
        }
    else:
        day, sep, at = str(weekly).partition("@")
        if not sep or zone is None:
            raise PrometheosError(
                "validation_error", "--weekly takes DAY@HH:MM and needs --zone (IANA)", {}
            )
        schedule = {"type": "weekly", "day": day.lower(), "time": at, "zone": zone}
    parameters: dict[str, Any] = {}
    if handler == "paper_check":
        parameters = {"paper_id": paper}
    elif handler == "source_refresh":
        parameters = {"source_ids": list(sources or [])}
    return {
        "handler": handler,
        "parameters": parameters,
        "schedule": schedule,
        "enabled": True,
        "budget": {
            "max_inputs": max_inputs,
            "max_output_chars": max_output_chars,
            "max_provider_calls": 0,
            "max_concurrent_runs": 1,
            "spend_cap_estimate_usd": None,
        },
        "last_slot": None,
    }


JobFileOpt = typer.Option(None, "--file", help="Complete job body JSON (instead of flags).")
HandlerOpt = typer.Option(None, "--handler", help="source_refresh | paper_check | digest")
PaperOpt = typer.Option(None, "--paper", help="Paper ID (paper_check).")
SourceOpt = typer.Option(None, "--source", help="Source ID (source_refresh; repeat).")
EveryOpt = typer.Option(None, "--every", help="Interval in whole minutes (>= 15).")
StartOpt = typer.Option(None, "--start", help="Interval start, UTC RFC3339 (default now).")
WeeklyOpt = typer.Option(None, "--weekly", help="DAY@HH:MM local time, e.g. monday@09:00.")
ZoneOpt = typer.Option(None, "--zone", help="IANA zone for --weekly, e.g. Europe/London.")
MaxInputsOpt = typer.Option(50, "--max-inputs")
MaxOutputOpt = typer.Option(4000, "--max-output-chars")


@job_app.command("plan")
def job_plan_cmd(
    file: Path = JobFileOpt,
    handler: str = HandlerOpt,
    paper: str = PaperOpt,
    source: list[str] = SourceOpt,
    every: int = EveryOpt,
    start: str = StartOpt,
    weekly: str = WeeklyOpt,
    zone: str = ZoneOpt,
    max_inputs: int = MaxInputsOpt,
    max_output_chars: int = MaxOutputOpt,
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Preview a job: next due slots (UTC and local), DST behaviour, side effects, budget."""
    svc = _service()

    def action() -> Any:
        body = _job_body(
            file, handler, paper, source, every, start, weekly, zone, max_inputs, max_output_chars
        )
        return svc.job_plan(svc.resolve_project_id(project), Caller("cli"), body)

    _run(as_json, action)


@job_app.command("add")
def job_add_cmd(
    file: Path = JobFileOpt,
    handler: str = HandlerOpt,
    paper: str = PaperOpt,
    source: list[str] = SourceOpt,
    every: int = EveryOpt,
    start: str = StartOpt,
    weekly: str = WeeklyOpt,
    zone: str = ZoneOpt,
    max_inputs: int = MaxInputsOpt,
    max_output_chars: int = MaxOutputOpt,
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record a job. It runs only when `prometheos jobs tick` runs."""
    svc = _service()

    def action() -> Any:
        body = _job_body(
            file, handler, paper, source, every, start, weekly, zone, max_inputs, max_output_chars
        )
        return svc.job_add(svc.resolve_project_id(project), Caller("cli"), body, _rid(request_id))

    _run(as_json, action)


@job_app.command("list")
def job_list_cmd(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Jobs with recent slots and the next due times."""
    svc = _service()
    _run(as_json, lambda: svc.job_list(svc.resolve_project_id(project), Caller("cli")))


@job_app.command("slots")
def job_slots_cmd(job_id: str, project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Every recorded slot of one job (state, attempts, reason, result)."""
    svc = _service()
    _run(as_json, lambda: svc.job_slots(svc.resolve_project_id(project), Caller("cli"), job_id))


def _set_enabled(job_id: str, base_rev: int, enabled: bool, project: str, as_json: bool) -> None:
    svc = _service()
    _run(
        as_json,
        lambda: svc.job_set_enabled(
            svc.resolve_project_id(project), Caller("cli"), job_id, base_rev, enabled, _rid(None)
        ),
    )


@job_app.command("enable")
def job_enable_cmd(
    job_id: str,
    base_rev: int = typer.Option(..., "--base-rev"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Enable a job; slots missed while disabled are not run."""
    _set_enabled(job_id, base_rev, True, project, as_json)


@job_app.command("disable")
def job_disable_cmd(
    job_id: str,
    base_rev: int = typer.Option(..., "--base-rev"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Disable a job; its pending slots are skipped."""
    _set_enabled(job_id, base_rev, False, project, as_json)


@job_app.command("cancel")
def job_cancel_cmd(
    job_id: str,
    slot: str = typer.Option(..., "--slot", help="Due time (UTC) of the slot."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Cancel one slot (pending: skipped; running: cancellation requested)."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.job_cancel(svc.resolve_project_id(project), Caller("cli"), job_id, slot),
    )


@job_app.command("resolve")
def job_resolve_cmd(
    job_id: str,
    slot: str = typer.Option(..., "--slot", help="Due time (UTC) of the unknown slot."),
    outcome: str = typer.Option(..., "--as", help="succeeded, failed or abandoned."),
    reason: str = typer.Option(..., "--reason", help="Why; kept with the slot."),
    project: str = ProjectOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record the owner's decision about a slot whose outcome is unknown (never automatic).

    A write, so it needs an exact-plan owner decision: in an interactive terminal you confirm
    this exact resolution with a fresh code; with --capability-file you present a capability
    bound to its digest; otherwise it becomes an approval request (approval_required) that the
    owner grants in the hub or `decision review`, and `approval resume` records it. Nothing is
    repeated; the slot keeps its external-request evidence."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        secret = _read_secret(capability_file)
        if secret is None and not _interactive():
            params = {"job_id": job_id, "due_utc": slot, "resolution": outcome, "reason": reason}
            _request_approval(svc, pid, Caller("cli"), "job_resolve", params)
        if secret is None and _interactive():
            digest = svc.job_resolve_digest(pid, job_id, slot, outcome, reason)
            _confirm_owner(
                f"Resolve unknown slot {slot} of {job_id} as {outcome}.\n"
                f"Reason: {reason}\n"
                f"Digest: {digest}",
                purpose="To record exactly this resolution",
            )
            secret = svc.grant(
                pid,
                operation="exact_request",
                digest=digest,
                ttl_seconds=600,
                max_uses=1,
                label="cli job resolve",
            ).secret
        return svc.job_resolve_slot(pid, Caller("cli"), job_id, slot, outcome, reason, secret)

    _run(as_json, action)


@jobs_app.command("tick")
def jobs_tick_cmd(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Run due slots and the event detectors once (bounded). Nothing repeats on its own; to
    repeat it, the owner may schedule this command."""
    svc = _service()
    _run(as_json, lambda: svc.jobs_tick(svc.resolve_project_id(project)))


@jobs_app.command("inbox")
def jobs_inbox_cmd(
    include_dismissed: bool = typer.Option(False, "--all", help="Include dismissed items."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Meaningful job findings and failures needing a decision."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.inbox(svc.resolve_project_id(project), Caller("cli"), include_dismissed),
    )


@jobs_app.command("dismiss")
def jobs_dismiss_cmd(
    notification_id: int, project: str = ProjectOpt, as_json: bool = JsonOpt
) -> None:
    """Dismiss an inbox item (inbox state only)."""
    svc = _service()
    _run(
        as_json,
        lambda: svc.inbox_dismiss(svc.resolve_project_id(project), Caller("cli"), notification_id),
    )


def _register_cp9() -> None:
    """LaTeX workspace, engine and Overleaf commands live in their own module (CP9)."""
    from prometheos import cli_latex

    cli_latex.register(paper_app)


_register_cp9()


# ----- CP6: audio, transcription and recording -------------------------------------------

audio_app = typer.Typer(
    help="Optional audio: import, local transcription, recording, models.", no_args_is_help=True
)
transcript_app = typer.Typer(help="Read and correct transcripts.", no_args_is_help=True)
model_app = typer.Typer(help="Local speech models (explicit install only).", no_args_is_help=True)
record_app_audio = typer.Typer(
    help="Live recording with recoverable segments.", no_args_is_help=True
)
audio_app.add_typer(transcript_app, name="transcript")
audio_app.add_typer(model_app, name="model")
audio_app.add_typer(record_app_audio, name="record")
app.add_typer(audio_app, name="audio")
VersionArg = typer.Argument(..., help="Version ID.")


def _progress(label: str) -> Callable[[int, int], None]:
    def report(done: int, total: int) -> None:
        sys.stderr.write(f"{label}: chunk {done}/{total}\n")

    return report


@audio_app.command("devices")
def audio_devices(
    backend: str = typer.Option(None, "--backend", help="auto, pyaudiowpatch or synthetic."),
    as_json: bool = JsonOpt,
) -> None:
    """List capture devices (microphones and WASAPI system-audio loopback). Records nothing."""
    from prometheos.audio import capture

    svc = _service()
    _run(as_json, lambda: svc.capture_devices(capture.get_backend(backend)))


@audio_app.command("import")
def audio_import_cmd(
    path: str = typer.Argument(..., help="Audio file under the research root."),
    root_alias: str = typer.Option(..., "--root", help="Approved research root alias."),
    title: str = typer.Option(None, "--title"),
    sensitivity: str = typer.Option(
        "restricted", "--sensitivity", help="Recordings default to restricted (hidden from agents)."
    ),
    retention: str = typer.Option("copy", "--retention", help="copy (needed to transcribe)."),
    no_wait: bool = NoWaitOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Import one audio file: the bytes are retained, hashed and re-verified at commit (an
    operation run)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        svc.worker_args = _worker_args(scope, capability_file)
        return svc.audio_import(
            pid,
            _caller(svc, pid, scope, capability_file),
            root_alias=root_alias,
            path=path,
            sensitivity=sensitivity,
            title=title,
            retention=retention,
            mode=_mode(no_wait),
        )

    _run(as_json, action)


def _run_config(
    engine: str,
    model: str | None,
    language: str | None,
    chunk_seconds: int,
    overlap_seconds: int,
    script: Path | None,
) -> Any:
    from prometheos.audio.transcribe import RunConfig

    lines: tuple[str, ...] = ()
    if script is not None:
        loaded = loads_strict(script.read_bytes())
        if not isinstance(loaded, list) or not all(isinstance(x, str) for x in loaded):
            raise PrometheosError("validation_error", "--script is a JSON list of strings", {})
        lines = tuple(loaded)
    return RunConfig(engine, model, language, chunk_seconds * 1000, overlap_seconds * 1000, lines)


@audio_app.command("transcribe")
def audio_transcribe(
    version_id: str = VersionArg,
    engine: str = typer.Option("faster-whisper", "--engine"),
    model: str = typer.Option(None, "--model", help="Installed model (default: base)."),
    language: str = typer.Option(None, "--language", help="2-3 letter code; default detects."),
    chunk_seconds: int = typer.Option(60, "--chunk-seconds"),
    overlap_seconds: int = typer.Option(2, "--overlap-seconds"),
    script: Path = typer.Option(
        None, "--script", help="Synthetic engine only: JSON list of lines."
    ),
    start_only: bool = typer.Option(False, "--start-only", help="Create the run without running."),
    resume: str = typer.Option(None, "--resume", help="Run an existing run ID to completion."),
    budget_seconds: int = typer.Option(
        None, "--budget-seconds", help="Pause the run before it exceeds this much audio."
    ),
    no_wait: bool = NoWaitOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Transcribe locally (resumable, cancellable). Needs the model installed explicitly."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        if resume:
            return svc.transcribe_run(pid, caller, resume, progress=_progress("transcribing"))
        config = _run_config(engine, model, language, chunk_seconds, overlap_seconds, script)
        run = svc.transcribe_start(pid, caller, version_id, config, budget_seconds=budget_seconds)
        _announce_run(run["run_id"], "audio_transcribe")  # named at once (round 2, F11)
        if start_only:
            return run
        if no_wait:
            if run["state"] == "queued":
                svc.worker_args = _worker_args(scope, capability_file)
                svc.run_dispatch(pid, run["run_id"], "detached")
            return run
        return svc.transcribe_run(pid, caller, run["run_id"], progress=_progress("transcribing"))

    _run(as_json, action)


@audio_app.command("runs")
def audio_runs(
    version: str = typer.Option(None, "--version", help="Only runs for this audio version."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Transcription runs (state, progress, errors)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.transcription_list(pid, _caller(svc, pid, scope, capability_file), version)

    _run(as_json, action)


@audio_app.command("run-status")
def audio_run_status(
    run_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One transcription run."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.transcription_status(pid, _caller(svc, pid, scope, capability_file), run_id)

    _run(as_json, action)


@audio_app.command("run-cancel")
def audio_run_cancel(
    run_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Cancel a run (an active one stops before its next chunk)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.transcription_cancel(pid, _caller(svc, pid, scope, capability_file), run_id)

    _run(as_json, action)


@transcript_app.command("show")
def transcript_show(
    version_id: str = VersionArg,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Segments, provenance, the recording it came from and later corrections."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.transcript_get(pid, _caller(svc, pid, scope, capability_file), version_id)

    _run(as_json, action)


@transcript_app.command("correct")
def transcript_correct_cmd(
    version_id: str = VersionArg,
    edits_file: Path = typer.Option(
        None, "--edits", help='JSON list: [{"segment_id":"seg-00001","text":"..."}].'
    ),
    segment: str = typer.Option(None, "--segment", help="One segment ID."),
    text: str = typer.Option(None, "--text", help="Corrected text for --segment."),
    speaker: str = typer.Option(None, "--speaker", help="Speaker label for --segment."),
    request_id: str = typer.Option(None, "--request-id", help="UUID4 for a retry-safe call."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record a correction as a new version of the transcript (nothing is overwritten)."""
    from prometheos.audio.transcribe import Edit

    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        edits: list[Edit] = []
        if edits_file is not None:
            loaded = loads_strict(edits_file.read_bytes())
            if not isinstance(loaded, list):
                raise PrometheosError("validation_error", "--edits is a JSON list", {})
            for entry in loaded:
                if not isinstance(entry, dict) or not isinstance(entry.get("segment_id"), str):
                    raise PrometheosError("validation_error", "each edit names a segment_id", {})
                edits.append(
                    Edit(
                        entry["segment_id"],
                        entry.get("text"),
                        entry.get("speaker_label"),
                        entry.get("uncertain"),
                    )
                )
        if segment is not None:
            edits.append(Edit(segment, text, speaker))
        return svc.transcript_correct(
            pid, _caller(svc, pid, scope, capability_file), version_id, edits, _rid(request_id)
        )

    _run(as_json, action)


@audio_app.command("span")
def audio_span_cmd(
    version_id: str = typer.Argument(..., help="Transcript version ID."),
    start_ms: int = typer.Option(..., "--start-ms"),
    end_ms: int = typer.Option(..., "--end-ms"),
    clip: Path = typer.Option(None, "--clip", help="Also write the audio of this span (new WAV)."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """The transcript text a locator cites, and optionally its audio, for checking by ear."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.audio_span(
            pid, _caller(svc, pid, scope, capability_file), version_id, start_ms, end_ms, clip
        )

    _run(as_json, action)


@model_app.command("plan")
def model_plan(
    model: str = typer.Option("faster-whisper-base", "--model"), as_json: bool = JsonOpt
) -> None:
    """Preview a model download: size, licence, revision, location. Downloads nothing."""
    svc = _service()
    _run(as_json, lambda: svc.audio_models_plan(model))


@model_app.command("install")
def model_install(
    plan_sha256: str = typer.Option(..., "--plan-sha256", help="Digest from `audio model plan`."),
    model: str = typer.Option("faster-whisper-base", "--model"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Download the model after you reviewed the plan; every file is verified. An owner
    decision: confirm it in an interactive terminal; otherwise it becomes an approval request
    in the project (approval_required)."""
    svc = _service()

    def report(name: str, got: int, total: int) -> None:
        if got == total or got % (16 * 1024 * 1024) < 1024 * 1024:
            sys.stderr.write(
                f"downloading {name}: {got // 1024 // 1024} MiB of {total // 1024 // 1024} MiB\n"
            )

    def action() -> Any:
        if not _interactive():
            pid = svc.resolve_project_id(project)
            params = {"model": model, "plan_sha256": plan_sha256}
            _request_approval(svc, pid, Caller("cli"), "audio_models_install", params)
        shown = svc.audio_models_plan(model)
        if shown["plan_sha256"] != plan_sha256:
            raise PrometheosError(
                "revision_conflict", "the model plan changed; review it again", {"plan": shown}
            )
        _confirm_owner(
            f"Download {model} ({shown['total_mib']} MiB, {shown['license']}) from "
            f"{shown['repository']} into {shown['location']}",
            purpose="To download exactly this model",
        )
        return svc.audio_models_install(
            model, plan_sha256, report, authority=_owner_act("cli-owner-model"),
            # The owner's install is recorded at owner rank: an agent cannot resume it
            # (decision 0017 round 2, F3).
            project_id=_maybe_project(svc, project), caller=Caller("cli", "owner"),
        )  # fmt: skip

    _run(as_json, action)


@model_app.command("status")
def model_status(
    verify: bool = typer.Option(False, "--verify", help="Re-hash installed files."),
    as_json: bool = JsonOpt,
) -> None:
    """Installed models and where they live."""
    svc = _service()
    _run(as_json, lambda: svc.audio_models_status(verify))


@model_app.command("remove")
def model_remove(
    model: str = typer.Option("faster-whisper-base", "--model"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Delete an installed model (owner decision; non-interactive: approval_required)."""
    svc = _service()

    def action() -> Any:
        if not _interactive():
            pid = svc.resolve_project_id(project)
            _request_approval(svc, pid, Caller("cli"), "audio_models_remove", {"model": model})
        _confirm_owner(f"Delete the installed speech model {model}")
        return svc.audio_models_remove(model, authority=_owner_act("cli-owner-model"))

    _run(as_json, action)


@record_app_audio.command("start")
def audio_record_start(
    devices: list[str] = typer.Option(..., "--device", help="Device ID; up to a mic + loopback."),
    title: str = typer.Option(..., "--title"),
    sensitivity: str = typer.Option("restricted", "--sensitivity"),
    segment_seconds: int = typer.Option(30, "--segment-seconds"),
    max_seconds: float = typer.Option(None, "--max-seconds", help="Stop automatically."),
    permission: bool = typer.Option(
        False,
        "--i-have-permission",
        help="Confirm every participant knows and agrees, under the rules that apply to you.",
    ),
    backend: str = typer.Option(None, "--backend"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Record until Ctrl-C (or another terminal runs `record stop`). Capture is visible here.
    A restricted recording is controlled from other terminals only with owner scope.
    Consent is an owner act at this computer: in an interactive terminal you confirm it with
    a fresh code; otherwise the start becomes an approval request (approval_required) that the
    owner grants in the hub, and `approval resume` starts it."""
    from prometheos.audio import capture, recorder

    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        if not _interactive():
            params = {
                "title": title,
                "device_ids": list(devices),
                "sensitivity": sensitivity,
                "segment_seconds": segment_seconds,
            }
            _request_approval(svc, pid, caller, "recording_start", params)
        if not permission:
            raise PrometheosError(
                "policy_denied",
                "confirm that every participant knows and agrees (--i-have-permission)",
                {},
            )
        _confirm_owner(
            f"Start recording '{title}' from {', '.join(devices)} ({sensitivity}).\n"
            f"{recorder.CONSENT_REMINDER}",
            purpose="To start this recording",
        )
        chosen = capture.get_backend(backend)
        session = svc.recording_start(
            pid,
            caller,
            recorder.SessionRequest(
                title, tuple(devices), sensitivity, segment_seconds, permission
            ),
            chosen,
            authority=_owner_act("cli-owner-recording"),
        )
        sys.stderr.write(
            f"RECORDING {session['session_id']} - "
            f"{', '.join(s['name'] for s in session['sources'])}\n"
            f"{recorder.CONSENT_REMINDER}\nCtrl-C stops and saves.\n"
        )

        def tick(state: dict[str, Any]) -> None:
            seconds = state["elapsed_ms"] // 1000
            sys.stderr.write(
                f"  {state['state']} {seconds // 60:02d}:{seconds % 60:02d} "
                f"level {state['level_dbfs']} dBFS\n"
            )

        return svc.recording_run(
            pid, session["session_id"], chosen, max_seconds=max_seconds, on_tick=tick
        )

    _run(as_json, action)


@record_app_audio.command("list")
def audio_record_list(
    all_sessions: bool = typer.Option(False, "--all"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Recording sessions that are running or recoverable (restricted ones need owner scope)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.recording_list(pid, _caller(svc, pid, scope, capability_file), all_sessions)

    _run(as_json, action)


@record_app_audio.command("status")
def audio_record_status(
    session_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One session: state, elapsed time, level, devices, reason."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.recording_status(pid, _caller(svc, pid, scope, capability_file), session_id)

    _run(as_json, action)


def _record_control(
    action_name: str,
    session_id: str,
    project: str | None,
    scope: str,
    capability_file: Path | None,
    as_json: bool,
    confirm: bool = False,
) -> None:
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        if action_name == "recover":
            return svc.recording_recover(pid, caller, session_id)
        if action_name == "discard":
            return svc.recording_discard(pid, caller, session_id, confirm)
        return svc.recording_control(pid, caller, session_id, action_name)

    _run(as_json, action)


@record_app_audio.command("pause")
def audio_record_pause(
    session_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Pause a running recording (audio while paused is not kept)."""
    _record_control("pause", session_id, project, scope, capability_file, as_json)


@record_app_audio.command("resume")
def audio_record_resume(
    session_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Resume a paused recording."""
    _record_control("resume", session_id, project, scope, capability_file, as_json)


@record_app_audio.command("stop")
def audio_record_stop(
    session_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Stop and save: the running recorder assembles and registers the audio."""
    _record_control("stop", session_id, project, scope, capability_file, as_json)


@record_app_audio.command("cancel")
def audio_record_cancel(
    session_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Stop without saving; segments stay recoverable until discarded."""
    _record_control("cancel", session_id, project, scope, capability_file, as_json)


@record_app_audio.command("recover")
def audio_record_recover(
    session_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Register what an interrupted, cancelled or abandoned session recorded."""
    _record_control("recover", session_id, project, scope, capability_file, as_json)


@record_app_audio.command("discard")
def audio_record_discard(
    session_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    confirm: bool = typer.Option(
        False, "--confirm", help="Required: the segments are deleted for good."
    ),
    as_json: bool = JsonOpt,
) -> None:
    """Delete an unregistered session's segments (cannot be undone; needs --confirm and the
    rank the recording was started at)."""
    _record_control("discard", session_id, project, scope, capability_file, as_json, confirm)


@meeting_app.command("from-audio")
def meeting_from_audio_cmd(
    transcript: str = typer.Option(..., "--transcript", help="Transcript version ID."),
    audio: str = typer.Option(None, "--audio", help="The recording's version ID."),
    title: str = typer.Option(..., "--title"),
    held_at: str = typer.Option(None, "--held-at", help="When it happened (UTC RFC3339)."),
    request_id: str = typer.Option(None, "--request-id", help="Reuse to retry safely."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Create a meeting whose original material is a recording and its transcript."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.capture_meeting_from_transcript(
            pid,
            _caller(svc, pid, scope, capability_file),
            transcript_version_id=transcript,
            audio_version_id=audio,
            title=title,
            held_at=held_at,
            request_id=_rid(request_id) if request_id else None,
        )

    _run(as_json, action)


# ----- CP6.3: providers (capability interface, routing policy, egress, budgets) -------------

provider_app = typer.Typer(
    help="Optional AI provider: routing policy, egress preview, budgets. Never required.",
    no_args_is_help=True,
)
secret_app = typer.Typer(help="Provider credentials (OS credential store).", no_args_is_help=True)
provider_app.add_typer(secret_app, name="secret")
app.add_typer(provider_app, name="provider")
EgressCapabilityOpt = typer.Option(
    None, "--egress-capability-file", help="Capability for sending restricted/private inputs."
)


@provider_app.command("status")
def provider_status_cmd(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Policy, route, capabilities, credential reference and health, usage. No secret values."""
    svc = _service()
    _run(as_json, lambda: svc.provider_status(svc.resolve_project_id(project), Caller("cli")))


@provider_app.command("calls")
def provider_calls_cmd(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """The call ledger: routes, states, costs (estimates) and digests; never message text."""
    svc = _service()
    _run(as_json, lambda: svc.provider_calls(svc.resolve_project_id(project), Caller("cli")))


@provider_app.command("preview")
def provider_preview_cmd(
    purpose: str = typer.Option(..., "--purpose", help="meeting_extract | notebook_draft | ..."),
    meeting: str = typer.Option(None, "--meeting"),
    budget: int = typer.Option(12000, "--budget", help="Context budget in characters."),
    include_restricted: bool = typer.Option(
        False, "--include-restricted", help="Owner scope only: also send restricted inputs."
    ),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Pure egress preview: what would leave this computer, by which route, and why not."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.provider_preview(
            pid,
            _caller(svc, pid, scope, capability_file),
            purpose=purpose,
            meeting_id=meeting,
            context_budget=budget,
            include_restricted=include_restricted,
        )

    _run(as_json, action)


def _owner_grant(svc: Any, pid: str, operation: str, digest: str, summary: str) -> str:
    """Interactive owner confirmation for exactly this digest; returns a single-use secret."""
    _confirm_owner(summary, purpose=f"To approve exactly this ({operation})")
    secret: str = svc.grant(
        pid,
        operation=operation,
        digest=digest,
        ttl_seconds=300,
        max_uses=1,
        label=f"cli {operation}",
    ).secret
    return secret


# ----- the intelligence layer (A07, CP22.4; decision 0094) ------------------------------------

ai_app = typer.Typer(
    help="The intelligence layer: the standing policy, task runs, results and evaluations.",
    no_args_is_help=True,
)
ai_policy_app = typer.Typer(help="The owner's standing policy.", no_args_is_help=True)
app.add_typer(ai_app, name="ai")
ai_app.add_typer(ai_policy_app, name="policy")


@ai_policy_app.command("show")
def ai_policy_show_cmd(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """The policy in force (or why none is), the key's health and this month's spend."""
    svc = _service()
    _run(as_json, lambda: svc.ai_policy_show(svc.resolve_project_id(project)))


@ai_policy_app.command("grant")
def ai_policy_grant_cmd(
    file: Path = typer.Option(..., "--file", help="The standing policy (JSON)."),
    preview_only: bool = typer.Option(False, "--preview", help="Show what a grant binds."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: grant (or replace) the standing policy. Without an interactive terminal
    it becomes an approval request (approval_required)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        document = loads_strict(file.read_bytes())
        shown = svc.ai_policy_preview(pid, document)
        if preview_only:
            return shown
        if not _interactive():
            params = {"policy": document, "binding_sha256": shown["binding_sha256"]}
            _request_approval(svc, pid, Caller("cli"), "ai_policy_grant", params, request_id)
        _confirm_owner("\n".join(["Grant the standing policy:", *shown["summary"],
                                   f"Binding: {shown['binding_sha256']}"]))  # fmt: skip
        return svc.ai_policy_grant(
            pid, document, shown["binding_sha256"], authority=_owner_act("cli-owner-ai-policy")
        )

    _run(as_json, action)


@ai_policy_app.command("revoke")
def ai_policy_revoke_cmd(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Owner decision: stop the layer's remote calls at once."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        _confirm_owner("Revoke the standing policy: no new remote task call until a new grant")
        return svc.ai_policy_revoke(pid, authority=_owner_act("cli-owner-ai-policy"))

    _run(as_json, action)


@ai_app.command("run")
def ai_run_cmd(
    task: str = typer.Option(..., "--task", help="A registered task kind."),
    scan_gen: int = typer.Option(..., "--scan", help="The adoption scan generation."),
    paths: list[str] = typer.Option(..., "--path", help="An inventory path (repeat)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show calls and cost; send nothing."),
    cassettes: Path = typer.Option(
        None, "--cassettes", help="Replay recorded replies from this folder (sends nothing)."
    ),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Run a task over adoption files inside the standing policy (agent scope)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.ai_run(pid, Caller("cli"), task_kind=task, scan_gen=scan_gen, paths=paths,
                          dry_run=dry_run, cassettes=cassettes)  # fmt: skip

    _run(as_json, action)


@ai_app.command("map-suggest")
def ai_map_suggest_cmd(
    task: str = typer.Option(
        "map.link_claim_question",
        "--task",
        help="map.link_claim_question, results.draft_interpretation or cards.word.",
    ),
    claims: list[str] = typer.Option(None, "--claim", help="A claim id (repeat); default: all."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show calls and cost; send nothing."),
    cassettes: Path = typer.Option(
        None, "--cassettes", help="Replay recorded replies from this folder (sends nothing)."
    ),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Run a record task: which question an unplaced claim answers (0109), the line of a
    rerun's output that holds a bound quantity, or a card's plain wording (0110). Agent scope."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.ai_map_suggest(pid, Caller("cli"), task_kind=task, claim_ids=claims or None,
                                  dry_run=dry_run, cassettes=cassettes)  # fmt: skip

    _run(as_json, action)


@ai_app.command("accept")
def ai_accept_cmd(
    task: str = typer.Option(..., "--task", help="A registered task kind."),
    scan_gen: int = typer.Option(..., "--scan", help="The adoption scan generation."),
    path: str = typer.Option(..., "--path", help="The inventory path of the file."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner decision: accept the layer's proposal for one file as proposed evidence."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        shown = svc.ai_accept_preview(pid, task, scan_gen, path)
        if not _interactive():
            params = {"task_kind": task, "scan_gen": scan_gen, "subject": path}
            _request_approval(svc, pid, Caller("cli"), "ai_accept", params, request_id)
        sources = ", ".join(f"{s['executor']} {s['model']}" for s in shown["samples"])
        _confirm_owner(f"Accept the {task} proposal for {path} ({sources}) as proposed evidence")
        keys = [x["result_key"] for x in shown["samples"]]  # exactly what was shown (R3-F3)
        return svc.ai_accept(pid, task, scan_gen, path, authority=_owner_act("cli-owner-ai"),
                             result_keys=keys)  # fmt: skip

    _run(as_json, action)


@ai_app.command("results")
def ai_results_cmd(
    task: str = typer.Option(None, "--task"),
    limit: int = typer.Option(200, "--limit"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Stored samples: subject, outcome, reasons, executor and served model."""
    svc = _service()
    _run(as_json, lambda: svc.ai_results(svc.resolve_project_id(project), task, limit))


@ai_app.command("eval")
def ai_eval_cmd(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Measurements per task and model, and the trust they confer."""
    svc = _service()
    _run(as_json, lambda: svc.ai_evaluations(svc.resolve_project_id(project)))


adopt_sensitivity_app = typer.Typer(
    help="The sensitivity of a research root (restricted until the owner sets it).",
    no_args_is_help=True,
)
adopt_app.add_typer(adopt_sensitivity_app, name="sensitivity")


@adopt_sensitivity_app.command("show")
def adopt_sensitivity_show_cmd(project: str = ProjectOpt, as_json: bool = JsonOpt) -> None:
    """Each research root's sensitivity."""
    svc = _service()
    _run(as_json, lambda: svc.ai_root_sensitivity_show(svc.resolve_project_id(project)))


@adopt_sensitivity_app.command("set")
def adopt_sensitivity_set_cmd(
    alias: str = typer.Argument(..., help="The research root alias."),
    sensitivity: str = typer.Argument(..., help="normal, restricted or private."),
    request_id: str = typer.Option(None, "--request-id"),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Raise freely; lowering is an owner decision (approval_required without a terminal)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        roots = svc.ai_root_sensitivity_show(pid)["roots"]
        current = next((r for r in roots if r["root_alias"] == alias), None)
        order = ("normal", "restricted", "private")
        lowering = (
            current is not None
            and sensitivity in order
            and order.index(sensitivity) < order.index(current["sensitivity"])
        )
        if not lowering:
            return svc.ai_root_sensitivity_set(pid, alias, sensitivity)
        params = {"root_alias": alias, "sensitivity": sensitivity}
        if not _interactive():
            _request_approval(svc, pid, Caller("cli"), "root_sensitivity_set", params, request_id)
        _confirm_owner(
            f"Lower research root {alias} from {current['sensitivity'] if current else '?'} to "
            f"{sensitivity}: its files may then be sent to the intelligence layer's provider"
        )
        return svc.ai_root_sensitivity_set(
            pid, alias, sensitivity, authority=_owner_act("cli-owner-sensitivity")
        )

    _run(as_json, action)


@provider_app.command("configure")
def provider_configure_cmd(
    file: Path = typer.Option(..., "--file", help="Provider configuration JSON."),
    preview_only: bool = typer.Option(False, "--preview", help="Show the change; apply nothing."),
    project: str = ProjectOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Set the routing policy (no-ai | local-only | remote-allowed), endpoint, model, prices and
    budgets. An owner decision: interactive confirmation or a capability bound to this digest."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        document = loads_strict(file.read_bytes())
        base = svc.provider_status(pid, Caller("cli"))["revision"]
        shown = svc.provider_configure_preview(pid, document, base)
        if preview_only:
            return shown
        secret = _read_secret(capability_file)
        if secret is None and not _interactive():
            params = {"config": document, "revision": base}
            _request_approval(svc, pid, Caller("cli"), "provider_configure", params)
        if secret is None:
            secret = _owner_grant(
                svc,
                pid,
                "provider_configure",
                shown["digest"],
                f"Provider configuration: policy {shown['policy']}, route {shown['route']}, "
                f"changes {', '.join(shown['changes']) or 'none'}\nDigest: {shown['digest']}",
            )
        return svc.provider_configure(pid, document, base, secret)

    _run(as_json, action)


@provider_app.command("approve")
def provider_approve_cmd(
    operation: str = typer.Option(..., "--operation", help="provider_run | provider_egress"),
    digest: str = typer.Option(..., "--digest", help="Digest shown by `provider preview`."),
    project: str = ProjectOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Owner approval of one previewed request: prints a single-use capability (stderr)."""
    svc = _service()

    def action() -> Any:
        if operation not in ("provider_run", "provider_egress", "provider_configure"):
            raise PrometheosError("validation_error", "unknown provider operation", {})
        pid = svc.resolve_project_id(project)
        secret = _owner_grant(svc, pid, operation, digest, f"Approve {operation}\nDigest: {digest}")
        sys.stderr.write("Capability secret (shown once, valid 5 minutes):\n" + secret + "\n")
        return {"operation": operation, "digest": digest}

    _run(as_json, action)


@provider_app.command("run")
def provider_run_cmd(
    purpose: str = typer.Option(..., "--purpose"),
    meeting: str = typer.Option(None, "--meeting"),
    budget: int = typer.Option(12000, "--budget"),
    include_restricted: bool = typer.Option(False, "--include-restricted"),
    digest: str = typer.Option(None, "--digest", help="Reviewed digest (from `provider preview`)."),
    acknowledge_unknown: str = typer.Option(
        None, "--acknowledge-unknown", help="Call ID of an earlier unknown outcome."
    ),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    scope_capability_file: Path = typer.Option(
        None, "--scope-capability-file", help="read_sensitive capability (with --scope owner)."
    ),
    capability_file: Path = CapabilityOpt,
    egress_capability_file: Path = EgressCapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One bounded generation. Needs the owner's capability for the exact preview digest; any
    failure leaves the task for an external agent and changes nothing else."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        reader = _caller(svc, pid, scope, scope_capability_file)
        shown = svc.provider_preview(
            pid, reader, purpose=purpose, meeting_id=meeting, context_budget=budget,
            include_restricted=include_restricted,
        )  # fmt: skip
        approved = digest or shown["digest"]
        secret = _read_secret(capability_file)
        egress = _read_secret(egress_capability_file, "PROMETHEOS_EGRESS_CAPABILITY")
        if secret is None and not _interactive():
            if include_restricted:
                raise PrometheosError(
                    "policy_denied",
                    "sending restricted records needs the owner at an interactive terminal",
                    {},
                )
            params = {
                "purpose": purpose,
                "meeting_id": meeting,
                "context_budget": budget,
                "digest": approved,
            }
            _request_approval(svc, pid, reader, "provider_run", params)
        if secret is None:
            if digest is not None:
                raise PrometheosError(
                    "policy_denied", "a reviewed digest needs its capability file", {}
                )
            secret = _owner_grant(
                svc, pid, "provider_run", shown["digest"],
                f"Send to {shown['endpoint_host']} ({shown['route']}) model {shown['model']}: "
                f"{shown['transmits']['characters']} characters, estimate "
                f"{shown['cost']['estimate_upper_bound_usd']} USD\nDigest: {shown['digest']}",
            )  # fmt: skip
            if shown["needs_egress_grant"] and egress is None:
                egress = _owner_grant(
                    svc, pid, "provider_egress", shown["transmits"]["payload_sha256"],
                    "This request includes RESTRICTED records\nPayload: "
                    + shown["transmits"]["payload_sha256"],
                )  # fmt: skip
        return svc.provider_run(
            pid,
            purpose=purpose,
            meeting_id=meeting,
            context_budget=budget,
            include_restricted=include_restricted,
            approved_digest=approved,
            capability=secret,
            egress_capability=egress,
            acknowledge_unknown=acknowledge_unknown,
            # Restricted inputs make the run (and its result) the owner's; otherwise the run is
            # visible at the reader's scope.
            caller=Caller("cli", "owner") if include_restricted else reader,
        )

    _run(as_json, action)


# ----- A02, CP10.3: operation runs (decision 0017) ---------------------------------------------

run_app = typer.Typer(
    help="Durable operation runs: list, status, cancel, resume, acknowledge.",
    no_args_is_help=True,
)
app.add_typer(run_app, name="run")


@run_app.command("list")
def run_list_cmd(
    state: str = typer.Option(None, "--state", help="queued, running, ... unknown_external."),
    operation: str = typer.Option(None, "--operation", help="Registry operation id."),
    limit: int = typer.Option(20, "--limit"),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Operation runs visible at your scope, newest first."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        return svc.run_list(pid, caller, state=state, operation=operation, limit=limit)

    _run(as_json, action)


@run_app.command("get")
def run_get_cmd(
    run_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One run: state, stage, progress, bounded logs, artifacts, limitations and result."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.run_get(pid, _caller(svc, pid, scope, capability_file), run_id)

    _run(as_json, action)


@run_app.command("cancel")
def run_cancel_cmd(
    run_id: str,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Cancel a run: at once when nothing executes it, otherwise a recorded request that the
    worker confirms at its next stage boundary (outcome cancel_requested)."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.run_cancel(pid, _caller(svc, pid, scope, capability_file), run_id)

    _run(as_json, action)


@run_app.command("resume")
def run_resume_cmd(
    run_id: str,
    budget: str = typer.Option(
        None, "--budget", help="Raise the run's own budget reservation (same unit)."
    ),
    no_wait: bool = NoWaitOpt,
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Continue a queued, failed or budget-paused run after its last completed stage, only
    with unchanged inputs, parameters, configuration, model and workflow version (otherwise
    stale_task: start a new run). Runs in this process unless --no-wait."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        caller = _caller(svc, pid, scope, capability_file)
        svc.worker_args = _worker_args(scope, capability_file)
        if not no_wait:
            sys.stderr.write(f"resuming {run_id}\n")
        view = svc.run_resume(
            pid, caller, run_id, budget_amount=budget,
            mode="detached" if no_wait else "foreground",
        )  # fmt: skip
        outcome["view"] = view
        return view

    outcome: dict[str, Any] = {}
    _run(as_json, action)
    raise typer.Exit(_run_exit_code(outcome.get("view")))


def _run_exit_code(view: dict[str, Any] | None) -> int:
    """A run that did not end well exits non-zero (decision 0017 round 2, F11): its error's
    exit code when it failed, 2 (like budget_exceeded) when paused at its budget, 5 when an
    external call's outcome is unknown, 3 when cancelled. Queued, running (--no-wait) and
    completed runs exit 0."""
    if not view:
        return 0
    state = view.get("state")
    if state == "failed":
        error = view.get("error") or {}
        return EXIT_CODES.get(str(error.get("code")), 5)
    return {"paused_budget": 2, "unknown_external": 5, "cancelled": 3}.get(str(state), 0)


@run_app.command("acknowledge")
def run_acknowledge_cmd(
    run_id: str,
    note: str = typer.Option(None, "--note", help="What you know about the external call."),
    project: str = ProjectOpt,
    scope: str = ScopeOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Close a run whose external call has an unknown outcome (unknown_external): it becomes
    failed with the uncertainty recorded. Nothing is sent again automatically."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        return svc.run_acknowledge(pid, _caller(svc, pid, scope, capability_file), run_id, note)

    _run(as_json, action)


def _secret_value(from_env: str | None) -> str:
    if from_env:
        value = os.environ.get(from_env)
        if not value:
            raise PrometheosError(
                "validation_error", f"environment variable {from_env} is empty", {}
            )
        return value.strip()
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("Credential (not shown): ").strip()
    return sys.stdin.readline().strip()


@secret_app.command("set")
def provider_secret_set_cmd(
    reference: str = typer.Option(..., "--reference"),
    from_env: str = typer.Option(None, "--from-env", help="Read the value from this variable."),
    project: str = ProjectOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Store a credential in the OS credential store (value from a hidden prompt or an
    environment variable; never from an argument). Prints health, never the value."""
    svc = _service()

    def action() -> Any:
        if not _interactive():
            raise PrometheosError(
                "policy_denied", "credential entry requires an interactive owner terminal", {}
            )
        pid = svc.resolve_project_id(project)
        secret = _read_secret(capability_file)
        digest = svc.provider_secret_digest("set", reference)
        if secret is None:
            secret = _owner_grant(
                svc, pid, "provider_configure", digest, f"Store a credential for {reference}"
            )
        return svc.provider_secret_set(pid, reference, _secret_value(from_env), secret)

    _run(as_json, action)


@secret_app.command("delete")
def provider_secret_delete_cmd(
    reference: str = typer.Option(..., "--reference"),
    project: str = ProjectOpt,
    capability_file: Path = CapabilityOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Delete a stored credential."""
    svc = _service()

    def action() -> Any:
        pid = svc.resolve_project_id(project)
        secret = _read_secret(capability_file)
        digest = svc.provider_secret_digest("delete", reference)
        if secret is None:
            secret = _owner_grant(
                svc, pid, "provider_configure", digest, f"Delete the credential {reference}"
            )
        return svc.provider_secret_delete(pid, reference, secret)

    _run(as_json, action)


def main() -> None:
    # Output that goes to a pipe or file is UTF-8, whatever the console code page: a JSON
    # document read by another program must decode as UTF-8 (dogfood P10). A terminal keeps
    # its own writer.
    for stream in (sys.stdout, sys.stderr):
        try:  # pythonw and detached starts have no stream, or a closed one (review F3)
            redirected = stream is not None and not stream.isatty()
        except ValueError:
            redirected = False
        if redirected and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    app()
