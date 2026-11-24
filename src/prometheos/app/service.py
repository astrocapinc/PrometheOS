"""Application service: the single mutation/query boundary for every transport."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from prometheos import __version__, packages
from prometheos.adoption import assist as adoption_assist
from prometheos.adoption import drift as adoption_drift
from prometheos.adoption import outline as adoption_outline
from prometheos.adoption import plan as adoption_plan
from prometheos.adoption import tables as adoption_tables
from prometheos.adoption import transfer as adoption_transfer
from prometheos.adoption import views as adoption_views
from prometheos.adoption.contracts import ArchiveLimits, ScanConfig
from prometheos.app import apply as apply_mod
from prometheos.app import (
    approval_kinds,
    approvals,
    brief,
    continuity,
    events,
    gitsync,
    jobs,
    maintenance,
    meetings,
    papers,
    purge,
    reads,
    receipts,
    run_handlers,
    runs,
    sources,
    tasks,
    texcompile,
    texengine,
    views,
    workflows,
    workspace,
)
from prometheos.app import context as context_mod
from prometheos.app import intelligence as app_intelligence
from prometheos.app.apply import apply_request, validate_body
from prometheos.app.authority import require_owner
from prometheos.audio import capture, importing, models, recorder, transcribe, wav
from prometheos.domain.bodies import ProjectSettings, ResearchRoot, default_settings
from prometheos.domain.canonical import (
    canonical_sha256,
    dumps_canonical,
    loads_internal,
    loads_strict,
)
from prometheos.domain.ids import (
    derived_request_id,
    is_record_id,
    is_request_id,
    new_id,
    new_request_id,
)
from prometheos.domain.models import MAX_REQUEST_BYTES, PrometheosError, utc_now
from prometheos.faults import fault_point
from prometheos.library import views as library_views
from prometheos.providers import adapter as provider_adapter
from prometheos.providers import config as provider_config
from prometheos.providers import generate as provider_generate
from prometheos.providers import ledger as provider_ledger
from prometheos.providers import secrets as provider_secrets
from prometheos.providers import transport as provider_transport
from prometheos.store import admission, capabilities, db, operational, pending
from prometheos.store import backup as backup_mod
from prometheos.store.capabilities import Secret as CapabilitySecret
from prometheos.store.home import Registry, app_home, check_store_location
from prometheos.store.lock import maintenance_lock
from prometheos.store.migrations import migrate, schema_version
from prometheos.store.project import ProjectStore, Txn
from prometheos.store.schema import MIGRATIONS

LINK_FILE = ".prometheos-link.json"
RUN_APPROVALS = frozenset({"tex_engine_install", "audio_models_install", "provider_run"})


def _approval_run_params(row: operational.ApprovalRequest) -> dict[str, Any] | None:
    """The run parameters of a protected long operation's approval plan (decision 0017)."""
    plan = row.plan
    if row.operation == "tex_engine_install":
        return {"plan_digest": plan["plan_digest"], "archive": None}
    if row.operation == "audio_models_install":
        return {"model": plan["model"], "plan_sha256": plan["plan_sha256"]}
    if row.operation == "provider_run":
        return {
            "purpose": plan["purpose"],
            "meeting_id": plan["meeting_id"],
            "context_budget": plan["context_budget"],
            "include_restricted": False,
            "approved_digest": plan["approved_digest"],
            "acknowledge_unknown": None,
        }
    return None


ORPHAN_GRACE_SECONDS = 24 * 3600


def _map_counts_and_gaps(
    conn: sqlite3.Connection, store_dir: Path, caller: reads.Caller
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """The map counts and, for the brief (decision 0109), the claims it raises, from one read
    of the derived statuses."""
    from prometheos.app import claim_status

    found = claim_status.statuses(conn, store_dir, caller)
    claims = {c["id"]: c for c in claim_status._rows(conn, "claim", caller)}
    return _map_counts(conn, store_dir, caller, found), brief.gaps(found, claims)


def _map_counts(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: reads.Caller,
    found: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """A06.1 (decision 0107): what ``now_get`` says about the research map, as counts: claims not
    to quote, decision cards waiting, and planned checks (open, and eligible to start now)."""
    from prometheos.app import cards, claim_status, verification

    if found is None:
        found = claim_status.statuses(conn, store_dir, caller)
    plan = verification.plan(conn, store_dir, caller)["checks"]
    return {
        "claims": len(found),
        "not_usable": sum(1 for s in found if not s["usable"]),
        "stale": sum(1 for s in found if s["status"] == "stale"),
        "agent_reported": sum(1 for s in found if s["basis"] == "agent_reported"),
        "cards_waiting": len(cards.pending(conn, caller.rank)),
        "checks_open": len(plan),
        "checks_eligible": sum(1 for c in plan if c["eligible"]),
    }


class Service:
    def __init__(self, home: Path | None = None, actor: str = "cli") -> None:
        self.home = home or app_home()
        self.registry = Registry(self.home)
        self.actor = actor
        # Injection points for the provider layer (tests use stand-ins; production leaves them
        # unset and gets the OS credential store and the real HTTP transport).
        self.secret_store: provider_secrets.SecretStore | None = None
        self.provider_transport: provider_transport.Transport | None = None
        # CP10.3 (decision 0017): a CLI announces each run it starts on stderr, and passes its
        # scope options to a detached worker.
        self.on_run_created: Callable[[str, str], None] | None = None
        self.worker_args: list[str] = []

    # ----- projects --------------------------------------------------------------

    def init_project(
        self,
        *,
        title: str,
        objective: str,
        timezone: str | None = None,
        roots: dict[str, str] | None = None,
        store: Path | None = None,
        declare_local: bool = False,
    ) -> dict[str, Any]:
        project_id = new_id("project")
        directory = store or (self.home / "projects" / project_id)
        report = check_store_location(directory, declare_local)
        if not report.accepted:
            raise PrometheosError("policy_denied", report.reason, {"store": str(directory)})
        settings = default_settings(title, objective, timezone)
        if roots:
            settings = settings.model_copy(
                update={
                    "allowed_roots": [
                        ResearchRoot(alias=alias, path=str(Path(path).resolve()))
                        for alias, path in roots.items()
                    ]
                }
            )
        model = validate_body("project", settings.model_dump(mode="json"))
        project = ProjectStore.create(report.path, project_id)
        request_id = new_request_id()
        body = model.model_dump(mode="json")

        def handler(txn: Txn) -> None:
            body_id = txn.insert_body(body)
            txn.write_revision(
                project_id,
                "project",
                1,
                body_id,
                "normal",
                "active",
                None,
                "project_init",
                body=body,
            )
            txn.result(project_id, 1)

        response = project.commit(
            request_id=request_id,
            request_sha256=canonical_sha256({"op": "project_init", "settings": body}),
            expected_seq=0,
            actor=self.actor,
            handler=handler,
        )
        self.registry.register(project_id, report.path, title, report.reason)
        return {
            "project_id": project_id,
            "store": str(report.path),
            "location": report.reason,
            "commit": response,
        }

    def resolve_project_id(self, explicit: str | None, cwd: Path | None = None) -> str:
        if explicit:
            return explicit
        env = os.environ.get("PROMETHEOS_PROJECT")
        if env:
            return env
        here = (cwd or Path.cwd()).resolve()
        for folder in (here, *here.parents):
            link = folder / LINK_FILE
            if link.is_file():
                data = loads_strict(link.read_bytes())
                if isinstance(data, dict) and is_record_id(str(data.get("project_id")), "project"):
                    return str(data["project_id"])
                raise PrometheosError("validation_error", f"malformed {LINK_FILE}", {})
        entries = self.registry.entries()
        if len(entries) == 1:
            return str(entries[0]["project_id"])
        raise PrometheosError(
            "validation_error",
            "choose a project with --project, PROMETHEOS_PROJECT or a link file",
            {"registered": len(entries)},
        )

    def open(self, project_id: str) -> ProjectStore:
        """The registered store, opened read-only once to check its identity. A store that
        does not open (missing, corrupt or unreadable database, a schema to migrate) answers
        with a recovery pointer to the maintenance service instead of a crash (CP10.5)."""
        if not is_record_id(project_id, "project"):
            raise PrometheosError("validation_error", "malformed project id", {})
        store = ProjectStore(self.registry.lookup(project_id))
        # Round 2 (F3, F5): the file, a hot journal and the schema are checked from bytes, and
        # the identity is read through an immutable connection: no side file in a damaged store.
        try:
            maintenance.check_openable(store.directory, project_id)
            found = store.identity()
        except (PrometheosError, sqlite3.DatabaseError, OSError) as exc:
            raise maintenance.open_failure(project_id, exc) from exc
        if found != project_id:
            raise maintenance.recovery_error(
                project_id, "wrong_project", "the registered store holds a different project"
            )
        # What a purge (or a worker that died since) left listed for deletion, and derived audio
        # of a recording the store no longer retains, goes here once nothing holds it any more
        # (decision 0031): no later transcription is needed for that.
        pending.sweep(store.directory, store)
        return store

    def list_projects(self) -> list[dict[str, Any]]:
        """Registered projects; a store the maintenance service replaced stays listed with its
        quarantine records (decision 0021)."""
        out = []
        for entry in self.registry.entries():
            records = self.registry.quarantine_records(str(entry["project_id"]))
            out.append(
                dict(entry)
                | {
                    "quarantined_stores": [
                        {
                            "quarantine_id": r.id,
                            "damaged_store_path": r.damaged_store_path,
                            "damaged_integrity": r.damaged_integrity,
                            "switched_at": r.switched_at,
                        }
                        for r in records
                    ]
                }
            )
        return out

    def project_info(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            with db.read_snapshot(conn):
                record = reads.redact_hidden(
                    conn, caller, reads.get_record(conn, store.directory, caller, project_id)
                )
                visible = (
                    ("normal",) if caller.scope != "owner" else ("normal", "restricted", "private")
                )
                marks = ",".join("?" for _ in visible)
                counts = {
                    r["kind"]: r["n"]
                    for r in conn.execute(
                        f"SELECT kind, COUNT(*) AS n FROM records WHERE sensitivity IN ({marks}) "  # noqa: S608
                        "AND lifecycle != 'deleted' GROUP BY kind",
                        visible,
                    )
                }
                seq = reads.project_seq(conn)
        finally:
            conn.close()
        return {
            "project_id": project_id,
            "project_seq": seq,
            "settings_rev": record["rev"],
            "settings": record["body"],
            "record_counts": counts,
            "store": str(store.directory),
        }

    # ----- records ---------------------------------------------------------------

    def apply(
        self,
        project_id: str,
        raw: bytes,
        caller: reads.Caller | None = None,
        capability: str | None = None,
    ) -> dict[str, Any]:
        """``capability``: owner decision secret (transport metadata, never stored). Without
        one, a request with protected effects becomes an approval request and the answer is
        ``approval_required`` (A02, CP10.2); nothing commits."""
        caller = caller or reads.Caller(actor=self.actor)
        rank = caller.rank
        return apply_request(
            self.open(project_id),
            raw,
            self.actor,
            max_rank=rank,
            capability=capability,
            request_approval=capability is None,
            approval_actor=caller.actor,
        )

    def get(
        self, project_id: str, caller: reads.Caller, record_id: str, at_seq: int | None
    ) -> dict[str, Any]:
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            with db.read_snapshot(conn):
                record = reads.get_record(conn, store.directory, caller, record_id, at_seq)
                redacted: dict[str, Any] = reads.redact_hidden(conn, caller, record)
                return redacted
        finally:
            conn.close()

    def list_records(self, project_id: str, caller: reads.Caller, **kwargs: Any) -> dict[str, Any]:
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            with db.read_snapshot(conn):
                listed = reads.list_records(conn, store.directory, caller, **kwargs)
                redacted_list: dict[str, Any] = reads.redact_hidden(conn, caller, listed)
                return redacted_list
        finally:
            conn.close()

    # ----- sources ---------------------------------------------------------------

    def _long(
        self,
        project_id: str,
        caller: reads.Caller,
        operation: str,
        params: dict[str, Any],
        *,
        mode: str,
        wait_seconds: float = 0,
        request_id: str | None = None,
        **create: Any,
    ) -> dict[str, Any]:
        """A long operation as an operation run: ``mode="wait"`` executes it here and returns
        its result with ``run_id``; any other mode answers the run view at once."""
        if mode == "wait":
            return self.run_execute_sync(
                project_id, caller, operation, params, request_id=request_id, **create
            )
        create.pop("options", None)
        create.pop("approval_request_id", None)
        return self.run_start(
            project_id, caller, operation, params, request_id=request_id, mode=mode,
            wait_seconds=wait_seconds, **create,
        )  # fmt: skip

    def import_plan(
        self,
        project_id: str,
        caller: reads.Caller | None = None,
        *,
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
        root_alias: str,
        paths: list[str],
        retention: str | None = None,
        sensitivity: str = "normal",
        titles: dict[str, str] | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Agent scope unless an owner caller is given (decision 0014 item 1). An operation
        run (decision 0017): the plan is persisted under an ID derived from the run."""
        caller = caller or reads.Caller(actor=self.actor)
        params = {
            "root_alias": root_alias,
            "paths": list(paths),
            "retention": retention,
            "sensitivity": sensitivity,
            "titles": titles,
            "description": description,
        }
        return self._long(
            project_id, caller, "source_import_plan", params, mode=mode,
            wait_seconds=wait_seconds, request_id=request_id,
        )  # fmt: skip

    def import_commit(
        self,
        project_id: str,
        plan_id: str,
        caller: reads.Caller | None = None,
        *,
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Commit or replay a plan at the caller's rank; agent scope unless an owner caller
        is given. A plan made at a higher rank reads as absent to a lower one. The run's input
        digests are the planned bytes; a resume rehashes the files and refuses changed ones."""
        caller = caller or reads.Caller(actor=self.actor)
        env = self._run_env(project_id)
        handler = run_handlers.ImportCommitHandler()
        fingerprint = handler.from_plan(handler.planned(env, plan_id, caller.rank))
        return self._long(
            project_id, caller, "source_import_commit", {"plan_id": plan_id}, mode=mode,
            wait_seconds=wait_seconds, request_id=request_id, fingerprint=fingerprint,
        )  # fmt: skip

    def receipt_record(
        self,
        project_id: str,
        *,
        raw: bytes,
        title: str,
        description: str,
        request_id: str,
        caller: reads.Caller | None = None,
    ) -> dict[str, Any]:
        """Record an externally reported execution receipt (decision 0009). Agent scope
        unless an owner caller is given: hidden records read as absent (review 0009 H1)."""
        caller = caller or reads.Caller(actor=self.actor)
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            settings, _ = sources._settings(conn, project_id)
        finally:
            conn.close()
        return receipts.record(
            store,
            raw=raw,
            title=title,
            description=description,
            request_id=request_id,
            actor=self.actor,
            allowed_roots={root.alias for root in settings.allowed_roots},
            max_rank=caller.rank,
        )

    def receipt_from_paths(
        self,
        project_id: str,
        *,
        run: dict[str, Any],
        inputs: list[dict[str, str]],
        outputs: list[dict[str, str]],
        title: str,
        description: str,
        request_id: str,
        caller: reads.Caller | None = None,
    ) -> dict[str, Any]:
        """Record what ran, naming its inputs and outputs by research-root path (decision 0112):
        each path is pinned to its newest recorded version, and only while the file on disk
        still has that version's digest. A file not recorded yet, or changed since, is refused
        with the list to import first, so a receipt never names bytes that were not recorded."""
        caller = caller or reads.Caller(actor=self.actor)
        store = self.open(project_id)
        found_in, missing_in = sources.recorded_artifacts(store, inputs, caller.rank)
        found_out, missing_out = sources.recorded_artifacts(store, outputs, caller.rank)
        if missing_in or missing_out:
            raise PrometheosError(
                "validation_error",
                "import these files first (source_import_plan, then source_import_commit), "
                "then record the run again",
                {"needs_import": missing_in + missing_out},
            )

        def pinned(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
            keys = ("source_id", "version_id", "sha256", "role")
            return [{k: a[k] for k in keys} for a in found]

        receipt = {
            **run,
            "schema_version": 1,
            "observation": "reported",
            "inputs": pinned(found_in),
            "outputs": pinned(found_out),
        }
        result = self.receipt_record(
            project_id,
            raw=json.dumps(receipt, ensure_ascii=False).encode("utf-8"),
            title=title,
            description=description,
            request_id=request_id,
            caller=caller,
        )
        return {"commit": result, "inputs": found_in, "outputs": found_out}

    def source_check(
        self,
        project_id: str,
        ids: list[str],
        save: bool,
        caller: reads.Caller | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return sources.check_sources(
            self.open(project_id),
            ids,
            save=save,
            actor=self.actor,
            caller=caller or reads.Caller(actor=self.actor),
            request_id=request_id,
        )

    # ----- backup and restore ------------------------------------------------------

    def backup(
        self, project_id: str, out: Path | None, caller: reads.Caller | None = None
    ) -> dict[str, Any]:
        """Whole-store snapshot: owner full-data scope only (spec/04)."""
        if caller is None or caller.scope != "owner" or not caller.full_data:
            raise PrometheosError(
                "policy_denied",
                "backups need the owner's full-data scope (prometheos decision grant "
                "--operation full_data_export)",
                {},
            )
        store = self.open(project_id)
        # CP10.5: a suffix keeps two backups made within one second apart (an owner's and a
        # granted agent request's, for instance).
        target = out or store.directory / "exports" / (
            f"backup-{int(time.time())}-{uuid.uuid4().hex[:6]}.zip"
        )
        made = backup_mod.create_backup(store, target)
        # A02, CP10.4 (decision 0018): artifact coverage counts only verified backups, so the
        # archive just written is verified into a scratch folder and inventoried (metadata
        # only; the inventory never holds backup bytes and does not advance the sequence).
        self.verify_backup(Path(made["backup"]))
        self._inventory_backup(store, made, purpose="manual")
        return made

    def verify_backup(self, path: Path) -> dict[str, Any]:
        scratch = self.home / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        return backup_mod.verify_backup(path, scratch)

    def restore(
        self,
        path: Path,
        into: Path,
        *,
        rebind: dict[str, str] | None = None,
        declare_local: bool = False,
        caller: reads.Caller | None = None,
    ) -> dict[str, Any]:
        """Verify, optionally rebind roots on the staged copy, then rename and register.

        Nothing becomes visible (target directory, registry entry) unless the archive
        verifies and every requested rebinding is valid; the previous store is kept.
        """
        if caller is None or caller.scope != "owner" or not caller.full_data:
            raise PrometheosError("policy_denied", "restore needs the owner's full-data scope", {})
        report = check_store_location(into, declare_local)
        if not report.accepted:
            raise PrometheosError("policy_denied", report.reason, {"store": str(into)})
        rebind = dict(rebind or {})
        for alias, target in rebind.items():
            if not Path(target).is_dir():
                raise PrometheosError(
                    "validation_error",
                    "rebind target is not an existing directory",
                    {"alias": alias},
                )

        upgraded: dict[str, Any] = {}

        def prepare(staging: Path) -> dict[str, Any]:
            # Decision 0052: an older backup's staged copy is upgraded before anything reads
            # it at the running schema; a failure discards staging (restore_backup).
            upgraded.update(self._upgrade_restored_copy(path, staging, report.path) or {})
            store = ProjectStore(staging)
            conn = store.connect(readonly=True)
            try:
                project_id = store.project_id_in(conn)
                settings_row = reads.current_record(conn, project_id)
            finally:
                conn.close()
            assert settings_row is not None
            settings = ProjectSettings.model_validate(settings_row["body"])
            unknown = set(rebind) - {r.alias for r in settings.allowed_roots}
            if unknown:
                raise PrometheosError(
                    "validation_error",
                    "rebind names unknown root aliases",
                    {"aliases": sorted(unknown)},
                )
            rebound = None
            if rebind:
                roots = [
                    ResearchRoot(
                        alias=root.alias,
                        path=str(Path(rebind[root.alias]).resolve())
                        if root.alias in rebind
                        else root.path,
                    )
                    for root in settings.allowed_roots
                ]
                new_settings = settings.model_copy(update={"allowed_roots": roots})
                request = {
                    "schema_version": 1,
                    "project_id": project_id,
                    "request_id": new_request_id(),
                    "expected_seq": reads.project_seq_of(store),
                    "operations": [
                        {
                            "op": "project_update",
                            "base_rev": settings_row["rev"],
                            "settings": new_settings.model_dump(mode="json"),
                        }
                    ],
                }
                rebound = apply_request(
                    store,
                    dumps_canonical(request),
                    self.actor,
                    max_rank=2,
                    owner_local="restore --rebind",
                )
                settings = new_settings
            return {"project_id": project_id, "settings": settings, "rebound": rebound}

        backup_sha = backup_mod._sha_file(path)
        maint = maintenance.Maintenance(self)
        # A04 (R2-L2): the registered store this restore would quarantine is checked before
        # anything is written, so a refusal about it leaves no target or staging folder.
        maint.check_owner_restore_source(backup_mod.read_manifest(path)["project_id"], report.path)
        manifest, prepared = backup_mod.restore_backup(path, report.path, prepare)
        settings = prepared["settings"]
        # CP10.5 (decision 0021): restoring over a registered project is an owner switch; the
        # previous store is quarantined and stays listed, never replaced silently.
        try:
            quarantined = maint.record_owner_restore(
                prepared["project_id"],
                report.path,
                backup=path,
                backup_sha256=backup_sha,
                manifest=manifest,
                switched_by=caller.actor,
            )
        except PrometheosError as exc:
            # The previous store changed after the early check: the verified restored store
            # is kept, unregistered, and the refusal names it (nothing is deleted).
            details = dict(exc.details)
            details["restored_store"] = str(report.path)
            raise PrometheosError(
                exc.code,
                f"{exc.message}; the verified restored store was kept but not registered",
                details,
            ) from exc
        if quarantined is None:
            previous = self.registry.register(
                prepared["project_id"], report.path, settings.title, report.reason, replace=True
            )
        else:
            previous = str(quarantined["damaged_store_path"])
        roots_report = [
            {"alias": r.alias, "path": r.path, "available": Path(r.path).is_dir()}
            for r in settings.allowed_roots
        ]
        return {
            "project_id": prepared["project_id"],
            "store": str(report.path),
            "previous_store": previous,
            "project_seq": manifest["project_seq"],
            "roots": roots_report,
            "reference_only_not_included": manifest["reference_only_not_included"],
            "rebind_commit": prepared["rebound"],
            "quarantine": quarantined,
            "migrated": upgraded or None,
        }

    def _upgrade_restored_copy(
        self, backup: Path, staging: Path, final: Path
    ) -> dict[str, Any] | None:
        """Restore, then upgrade (decision 0052; owner answer of 2026-10-16, decision 0049).

        ``staging`` is the restore's private, verified, unregistered copy, so it is migrated
        directly, outside the admission gate. Like ``migrate``, a verified backup of the old
        schema is kept in the store's ``exports/`` first: here it is the restored archive
        itself, copied and checked against its digest, and it enters the backup inventory as
        ``pre_migration``. The staged copy must then pass SQLite's integrity and foreign-key
        checks at the running schema. Any failure raises, and ``restore_backup`` discards the
        staging folder: no target, no registry change, the live store and the archive as they
        were. Returns None for a backup already at the running schema."""
        staged = ProjectStore(staging, _scratch_copy=True)
        conn = db.connect(staged.db_path)
        try:
            old = schema_version(conn)
            latest = len(MIGRATIONS)
            if old >= latest:
                return None
            name = f"pre-migrate-v{old}-to-v{latest}-{int(time.time())}.zip"
            copy = staging / "exports" / name
            try:
                # The kept archive is bound to what was verified: its manifest's database digest
                # must be the staged database's, before any migration (review s12-restore F1).
                shutil.copyfile(backup, copy)
                backup_sha = backup_mod._sha_file(copy)
                manifest = backup_mod.read_manifest(copy)
                if manifest["database"]["sha256"] != backup_mod._sha_file(staged.db_path):
                    raise PrometheosError(
                        "validation_error", "the backup changed while it was restored", {}
                    )
                applied = migrate(conn, MIGRATIONS)
                fault_point("restore_after_upgrade")
                check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            except (PrometheosError, sqlite3.Error, OSError) as exc:  # review F3: OSError too
                code = exc.code if isinstance(exc, PrometheosError) else "unsupported"
                raise PrometheosError(
                    code,
                    f"the restored copy could not be upgraded from schema {old} to {latest} "
                    f"({exc}); nothing was registered and the current store is unchanged",
                    {"store_schema_version": old, "supported_schema_version": latest},
                ) from exc
            if check != "ok" or violations:
                raise PrometheosError(
                    "validation_error",
                    "the upgraded copy failed its integrity check; nothing was registered",
                    {"integrity": check, "foreign_key_violations": len(violations)},
                )
            new = schema_version(conn)
        finally:
            conn.close()
        made = {
            "backup": str(final / "exports" / name),
            "sha256": backup_sha,
            "manifest": manifest,
        }
        if new >= 7:  # the inventory table exists from schema 7 (as in ``migrate``)
            self._inventory_backup(staged, made, purpose="pre_migration")
        return {
            "from_schema": old,
            "to_schema": new,
            "applied": applied,
            "pre_migration_backup": {"path": made["backup"], "sha256": backup_sha},
        }

    # ----- maintenance -------------------------------------------------------------

    def migrate(
        self,
        project_id: str,
        *,
        authority: object,
        _authorize_source: Callable[[Path], None] | None = None,
    ) -> dict[str, Any]:
        """Upgrade a store's schema after creating and verifying a backup of the old one.

        The backup is written inside the store's own ``exports/`` folder (no data leaves
        the store directory). A failed backup or verification leaves the old schema
        untouched; a failed migration rolls back that migration's transaction.

        An owner decision (A02, CP10.2): ``authority`` must be an :class:`OwnerAct` (the
        interactive CLI confirmation). A store that needs migration cannot hold an approval
        request (its database does not open at the current schema), so an agent's request
        for it belongs to the maintenance approvals of CP10.5 (decision 0016).
        """
        require_owner(authority, "migrating a project store")
        if not is_record_id(project_id, "project"):
            raise PrometheosError("validation_error", "malformed project id", {})
        # Round 2 (F6): a damaged store, a hot journal or a newer schema is never migrated (a
        # migration opens the store read-write).
        self._maintenance().require_migratable(project_id)
        store = ProjectStore(self.registry.lookup(project_id))
        report: dict[str, Any] = {"backup": None}

        def backup_first(old: int, new: int) -> None:
            out = (
                store.directory / "exports" / f"pre-migrate-v{old}-to-v{new}-{int(time.time())}.zip"
            )
            made = backup_mod.create_backup(store, out, allow_pending=True, already_locked=True)
            scratch = self.home / "scratch"
            scratch.mkdir(parents=True, exist_ok=True)
            backup_mod.verify_backup(out, scratch)
            report["backup"] = {"path": made["backup"], "sha256": made["sha256"], "verified": True}
            report["_made"] = made

        with maintenance_lock(store.directory), admission.operation(store.db_path):
            self._maintenance().require_migratable(project_id)
            # Internal maintenance grant validation runs at this exact admission boundary.
            # Keep the same gate from authorization through upgrade/actual connection close.
            if _authorize_source is not None:
                _authorize_source(store.directory)
            result = store.upgrade(backup_first)
            made = report.pop("_made", None)
            if made is not None and result["to_schema"] >= 7:
                # A02 (CP10): the verified pre-migration backup enters the store's backup
                # inventory once the inventory table exists (metadata only).
                self._inventory_backup(store, made, purpose="pre_migration")
        return {**result, **report}

    @staticmethod
    def _inventory_backup(store: ProjectStore, made: dict[str, Any], *, purpose: str) -> None:
        now = utc_now()
        entry, media = operational.entry_from_manifest(
            made["manifest"],
            backup_sha256=made["sha256"],
            location=str(made["backup"]),
            label=Path(made["backup"]).name,
            purpose=purpose,
            verified_at=now,
            recorded_at=now,
        )
        conn = store.connect()
        try:
            with db.write_transaction(conn):
                operational.record_backup(conn, entry, media)
        finally:
            conn.close()

    # ----- CP10.5: maintenance service (no project database; decision 0021) ------------------

    def _maintenance(self) -> maintenance.Maintenance:
        return maintenance.Maintenance(self)

    def maintenance_inspect(
        self,
        project_id: str,
        caller: reads.Caller,
        quarantine_id: str | None = None,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Integrity, schema version and lock state of the store (or of one quarantined
        store), its backups, maintenance requests and quarantine records. Opens no project
        database: the database is read from a scratch copy."""
        return self._maintenance().inspect(project_id, caller.rank, quarantine_id, refresh=refresh)

    def maintenance_verify(self, project_id: str, backup: str) -> dict[str, Any]:
        """Verify a backup of this project in a scratch staging folder."""
        return self._maintenance().verify(project_id, backup)

    def maintenance_restore(
        self,
        project_id: str,
        backup: str,
        into: Path | str | None = None,
        *,
        declare_local: bool = False,
        owner: object = None,
    ) -> dict[str, Any]:
        """Verify and restore a backup into a new store; the registry is not changed. Without
        an owner act it lands in a new folder PrometheOS creates under the home (round 2, F1)."""
        return self._maintenance().restore(
            project_id, backup, into, declare_local=declare_local, owner=owner
        )

    def maintenance_journal_recover(self, project_id: str) -> dict[str, Any]:
        """Recover a hot rollback journal into a new store (via a verified backup in the
        managed area); never in place (round 2, F3)."""
        return self._maintenance().journal_recover(project_id)

    def maintenance_switch(
        self,
        project_id: str,
        caller: reads.Caller,
        backup: str,
        target: str,
        *,
        request_id: str | None = None,
        owner: object = None,
    ) -> dict[str, Any]:
        """Switch the registry to a verified restored store. With an owner act (paired hub,
        interactive terminal) it switches at once; any other caller gets ``approval_required``
        with a maintenance request held in the registry."""
        if owner is not None:
            return self._maintenance().switch_direct(project_id, backup, target, owner=owner)
        raise self._maintenance().request_switch(
            project_id, caller.rank, caller.actor, backup, target, request_id
        )

    def maintenance_migrate_request(
        self, project_id: str, caller: reads.Caller, request_id: str | None = None
    ) -> dict[str, Any]:
        """An agent's request to migrate a store's schema: always ``approval_required``."""
        raise self._maintenance().request_migrate(project_id, caller.rank, caller.actor, request_id)

    def maintenance_approval_get(
        self, project_id: str, caller: reads.Caller, approval_id: str
    ) -> dict[str, Any]:
        return self._maintenance().get(project_id, approval_id, caller.rank)

    def maintenance_queue(self, project_id: str) -> dict[str, Any]:
        """Every maintenance request of the project (the owner's view)."""
        return self._maintenance().queue(project_id)

    def maintenance_resume(
        self, project_id: str, caller: reads.Caller, approval_id: str
    ) -> dict[str, Any]:
        return self._maintenance().resume(project_id, approval_id, caller.rank, caller.actor)

    def maintenance_withdraw(
        self, project_id: str, caller: reads.Caller, approval_id: str
    ) -> dict[str, Any]:
        return self._maintenance().withdraw(project_id, approval_id, caller.rank)

    def maintenance_decide(
        self, project_id: str, approval_id: str, *, grant: bool, plan_sha256: str, owner: object
    ) -> dict[str, Any]:
        """Owner channels only: grant or deny exactly the maintenance plan that was shown."""
        return self._maintenance().decide(
            project_id, approval_id, grant=grant, plan_sha256=plan_sha256, owner=owner
        )

    def orphans(
        self, project_id: str, collect: bool, grace_seconds: int = ORPHAN_GRACE_SECONDS
    ) -> dict[str, Any]:
        store = self.open(project_id)
        now = time.time()
        with maintenance_lock(store.directory):
            conn = store.connect(readonly=True)
            try:
                referenced = {
                    r["storage_key"] for r in conn.execute("SELECT storage_key FROM media")
                }
                protected = recorder.protected_directories(conn)
            finally:
                conn.close()
            found = []
            for path in sorted((store.directory / "media").rglob("*")):
                if path.is_file():
                    key = path.relative_to(store.directory / "media").as_posix()
                    if key not in referenced:
                        found.append(("media", path))
            for path in sorted((store.directory / "staging").glob("*")):
                if path.name in protected:
                    continue  # an unfinished or recoverable recording session owns it
                found.append(("staging", path))
            report = []
            for area, path in found:
                age = now - path.stat().st_mtime
                collectable = age >= grace_seconds
                removed = False
                if collect and collectable:
                    if path.is_dir():
                        import shutil

                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
                    removed = True
                report.append(
                    {
                        "area": area,
                        "name": path.name,
                        "age_seconds": int(age),
                        "collectable": collectable,
                        "removed": removed,
                    }
                )
        return {"orphans": report, "grace_seconds": grace_seconds}

    # ----- A07, CP21.2: remembered devices (decision 0089) -----------------------------------

    def device_list(self) -> dict[str, Any]:
        """The home's remembered browsers (owner only: CLI and the paired hub; never MCP)."""
        from prometheos import devices

        return {"devices": devices.list_devices(Path(self.home))}

    def device_revoke(self, device_id: str) -> dict[str, Any]:
        """Revoke a remembered browser: its credential and its grace value stop at once."""
        from prometheos import devices

        return devices.revoke(Path(self.home), device_id)

    def doctor(self, project_id: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "prometheos": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "home": str(self.home),
            "supported_schema_version": len(MIGRATIONS),
            "projects_registered": len(self.registry.entries()),
        }
        if project_id:
            try:
                store = self.open(project_id)
                conn = store.connect(readonly=True)
            except PrometheosError as exc:
                if exc.code not in ("io_error", "unsupported"):
                    raise
                # CP10.5: a store that does not open is reported with its recovery pointer
                # (the maintenance service inspects it without opening it).
                result["project"] = {
                    "project_id": project_id,
                    "opens": False,
                    "error": exc.message,
                    "recovery": maintenance.recovery_pointer(project_id),
                }
                return result
            try:
                result["project"] = {
                    "project_id": project_id,
                    "store": str(store.directory),
                    "schema_version": schema_version(conn),
                    "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                    "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "project_seq": reads.project_seq(conn),
                    "location": check_store_location(store.directory).reason,
                }
            finally:
                conn.close()
        return result

    # ----- CP2: views ------------------------------------------------------------------

    def _read[T](self, project_id: str, fn: Callable[[sqlite3.Connection, Path], T]) -> T:
        return self._read_as(project_id, None, fn)

    def _read_as[T](
        self,
        project_id: str,
        caller: reads.Caller | None,
        fn: Callable[[sqlite3.Connection, Path], T],
    ) -> T:
        """Pure read in one snapshot; agent-scope results get hidden IDs redacted."""
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            with db.read_snapshot(conn):
                result = fn(conn, store.directory)
                if caller is not None:
                    result = reads.redact_hidden(conn, caller, result)
                return result
        finally:
            conn.close()

    def context(self, project_id: str, caller: reads.Caller, **kwargs: Any) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: context_mod.assemble(c, d, caller, **kwargs)
        )

    def next_steps(
        self, project_id: str, caller: reads.Caller, at_seq: int | None = None
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: views.next_steps(c, d, caller, at_seq)
        )

    def agenda(self, project_id: str, caller: reads.Caller, meeting_id: str) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: views.agenda(c, d, caller, meeting_id)
        )

    def notebook(self, project_id: str, caller: reads.Caller, **kwargs: Any) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: views.notebook(c, d, caller, **kwargs)
        )

    def search(
        self, project_id: str, caller: reads.Caller, query: str, limit: int = 20
    ) -> dict[str, Any]:
        return self._read_as(project_id, caller, lambda c, d: views.search(c, caller, query, limit))

    def claim_status(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        claim_ids: list[str] | None = None,
        paper_id: str | None = None,
    ) -> dict[str, Any]:
        """Derived claim status (A06.1, CP20.1; decision 0082): a pure, rebuildable read that
        never changes a record and is never support."""
        from prometheos.app import claim_status

        return self._read_as(
            project_id,
            caller,
            lambda c, d: claim_status.view(c, d, caller, claim_ids=claim_ids, paper_id=paper_id),
        )

    def research_map(
        self, project_id: str, caller: reads.Caller, *, paper_id: str | None = None
    ) -> dict[str, Any]:
        """The research map (A06.1, CP20.2; decision 0083): objective, deliverables with derived
        stages, questions, claims and tracks. A pure read that changes nothing."""
        from prometheos.app import research_map

        return self._read_as(
            project_id, caller, lambda c, d: research_map.view(c, d, caller, paper_id=paper_id)
        )

    # ----- A06.1, CP20.3: planned checks, the verification budget, decision cards (0084) -----

    def verification_plan(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        """The open checks the caller may see, cheapest first, with whether an agent may start
        each within the owner's budget now. A pure read."""
        from prometheos.app import verification

        return self._read_as(project_id, caller, lambda c, d: verification.plan(c, d, caller))

    def verification_budget_request(self, project_id: str, budget: dict[str, Any]) -> bytes:
        """The owner's budget change as the one ``project_update`` request the CLI's ``verify
        budget`` and the hub's budget control both preview and apply (review cp20-3-a F4): the
        current settings with ``verification_budget`` replaced. The settings are read here, with
        the owner's visibility, so a client never resends the research roots' local paths."""
        from prometheos.domain.bodies import VerificationBudget

        try:
            checked = VerificationBudget.model_validate(budget).model_dump(mode="json")
        except ValidationError as exc:
            raise PrometheosError(
                "validation_error",
                "this budget cannot be applied",
                {"errors": [e["msg"] for e in exc.errors()][:5]},
            ) from exc
        owner = reads.Caller(actor="owner-budget", scope="owner")
        current = self.get(project_id, owner, project_id, None)
        request = {
            "schema_version": 1,
            "project_id": project_id,
            "request_id": new_request_id(),
            "expected_seq": self.project_info(project_id, owner)["project_seq"],
            "operations": [
                {
                    "op": "project_update",
                    "base_rev": current["rev"],
                    "settings": dict(current["body"]) | {"verification_budget": checked},
                }
            ],
        }
        return dumps_canonical(request)

    def verification_propose(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        """Run PrometheOS's check detector: one proposed verification action per uncertain
        claim the caller may see and cause, deduplicated. The content is PrometheOS's; the
        caller only asks for the pass. Starts nothing."""
        from prometheos.app import verification

        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            with db.read_snapshot(conn):
                ops = verification.proposals(conn, store.directory, caller)
                seq = int(conn.execute("SELECT project_seq FROM project_state").fetchone()[0])
        finally:
            conn.close()
        if not ops:
            return {"project_seq": seq, "created": []}
        request = {
            "schema_version": 1,
            "project_id": project_id,
            "request_id": new_request_id(),
            "expected_seq": seq,
            "operations": ops,
        }
        commit = apply_request(
            store, dumps_canonical(request), verification.DETECTOR_ACTOR, max_rank=caller.rank
        )
        return {"project_seq": commit["project_seq"], "created": [op["id"] for op in ops]}

    def decision_cards(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        """Pending approval requests with a decision card, as the caller may see them."""
        from prometheos.app import cards

        return self._read_as(project_id, caller, lambda c, d: cards.view(c, caller))

    def recommended_preview(
        self, project_id: str, caller: reads.Caller, approval_ids: list[str], owner: object
    ) -> dict[str, Any]:
        """The owner's preview of "apply all recommended": the listed plans as one request,
        its protected effects and the digest the owner approves. Owner only (review cp20-3-a
        F2): the preview runs with owner visibility."""
        from prometheos.app import cards

        require_owner(owner, "previewing the recommended set")

        built = self._read_as(
            project_id, caller, lambda c, d: cards.bundle(c, project_id, caller.rank, approval_ids)
        )
        preview = self.review_request(project_id, built["raw"], caller)
        return {
            "approval_request_ids": built["approval_request_ids"],
            "plan_sha256s": built["plan_sha256s"],
            "operations": len(built["request"]["operations"]),
            "preview": preview,
        }

    def recommended_apply(
        self,
        project_id: str,
        caller: reads.Caller,
        approval_ids: list[str],
        digest: str,
        owner: object,
    ) -> dict[str, Any]:
        """Apply the listed plans in one transaction, only if the digest the owner approved
        still matches; then close the folded requests. Nothing applies if any base changed.
        Owner only (review cp20-3-a F2)."""
        from prometheos.app import approvals, cards

        act = require_owner(owner, "applying the recommended set")

        built = self._read_as(
            project_id, caller, lambda c, d: cards.bundle(c, project_id, caller.rank, approval_ids)
        )
        preview = self.review_request(project_id, built["raw"], caller)
        if not preview["would_apply"]:
            raise PrometheosError(
                "revision_conflict",
                "a listed plan no longer applies; nothing was changed",
                {"preview": preview},
            )
        if digest != preview["digest"]:
            raise PrometheosError(
                "revision_conflict",
                "the recommended set changed since it was previewed; review it again",
                {"digest": preview["digest"]},
            )
        secret = None
        if preview["protected_effects"]:
            secret = self.grant(
                project_id,
                operation="exact_request",
                digest=preview["digest"],
                ttl_seconds=60,
                max_uses=1,
                label="apply all recommended",
            ).secret
        result = self.apply(project_id, built["raw"], caller, secret)
        # The close is its own write after the commit. If it fails, each folded plan still
        # names base revisions that moved, so none of them can run again (decision 0084).
        approvals.close_applied_together(
            self.open(project_id),
            built["approval_request_ids"],
            int(result["project_seq"]),
            decided_by=act.actor,
        )
        return {"result": result, "closed": built["approval_request_ids"]}

    def links(self, project_id: str, caller: reads.Caller, record_id: str) -> dict[str, Any]:
        return self._read_as(project_id, caller, lambda c, d: views.links(c, caller, record_id))

    def reconcile(self, project_id: str, caller: reads.Caller, meeting_id: str) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: meetings.reconcile(c, d, caller, meeting_id)
        )

    # ----- CP10.4: research continuity (A02; decision 0018) --------------------------------

    def execution_register(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        title: str,
        host_label: str,
        reporter: str,
        command_summary: str | None = None,
        working_root_alias: str | None = None,
        external_ref: str | None = None,
        expected_outputs: list[str] | None = None,
        sensitivity: str = "normal",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Observe-only registration of a run the agent owns. Nothing is started."""
        return continuity.register(
            self.open(project_id),
            title=title,
            host_label=host_label,
            reporter=reporter,
            command_summary=command_summary,
            working_root_alias=working_root_alias,
            external_ref=external_ref,
            expected_outputs=expected_outputs or [],
            sensitivity=sensitivity,
            request_id=request_id or new_request_id(),
            actor=self.actor,
            max_rank=caller.rank,
        )

    def execution_report(
        self,
        project_id: str,
        caller: reads.Caller,
        execution_id: str,
        *,
        state: str,
        reported_at: str | None = None,
        receipt_version_ids: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a reported status and any receipts. Reporting never acts on the run."""
        return continuity.report(
            self.open(project_id),
            execution_id=execution_id,
            state=state,
            reported_at=reported_at,
            receipt_version_ids=receipt_version_ids or [],
            request_id=request_id or new_request_id(),
            actor=self.actor,
            max_rank=caller.rank,
        )

    def execution_list(
        self, project_id: str, caller: reads.Caller, **kwargs: Any
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: continuity.execution_views(c, caller, **kwargs)
        )

    def awaiting_list(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        return self._read_as(project_id, caller, lambda c, d: continuity.awaiting_view(c, caller))

    def awaiting_resolve_request(
        self,
        project_id: str,
        caller: reads.Caller,
        target_id: str,
        resolution: str,
        text: str,
        request_id: str,
    ) -> bytes:
        """The canonical transaction that resolves one queue item (a reviewed entry citing it)."""
        if not is_request_id(request_id):
            raise PrometheosError("validation_error", "request_id must be a canonical UUID4", {})
        return self._read(
            project_id,
            lambda c, d: continuity.resolution_request(
                c,
                caller,
                project_id,
                target_id=target_id,
                resolution=resolution,
                text=text,
                request_id=request_id,
            ),
        )

    def awaiting_resolve(
        self,
        project_id: str,
        caller: reads.Caller,
        target_id: str,
        resolution: str,
        text: str,
        request_id: str,
        capability: str | None = None,
    ) -> dict[str, Any]:
        """Take an item out of the queue with an interpretation or a dismissal reason. The
        reviewed entry is a protected effect: without an owner decision the answer is
        ``approval_required`` and nothing commits."""
        replay = self.awaiting_resolve_replay(project_id, caller, request_id)
        if replay is not None:
            return replay
        raw = self.awaiting_resolve_request(
            project_id, caller, target_id, resolution, text, request_id
        )
        return self.apply(project_id, raw, caller, capability)

    def awaiting_resolve_replay(
        self, project_id: str, caller: reads.Caller, request_id: str
    ) -> dict[str, Any] | None:
        """The stored response of a resolution that already committed under ``request_id``."""
        if not is_request_id(request_id):
            raise PrometheosError("validation_error", "request_id must be a canonical UUID4", {})
        return continuity.resolution_replay(self.open(project_id), caller, request_id)

    def artifact_coverage(
        self, project_id: str, caller: reads.Caller, **kwargs: Any
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: continuity.artifact_coverage(c, d, caller, **kwargs),
        )

    def now(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        since_seq: int | None = None,
        due_within_days: int = continuity.DUE_WITHIN_DAYS,
        silence_after_hours: int = continuity.SILENCE_AFTER_HOURS,
        limit: int = continuity.NOW_LIMIT,
    ) -> dict[str, Any]:
        """Changed, due, running, awaiting-interpretation and approval items, each with a
        reason. Read-only (the approval listing marks expired requests, as elsewhere)."""
        view = self._read_as(
            project_id,
            caller,
            lambda c, d: continuity.now_view(
                c,
                d,
                caller,
                since_seq=since_seq,
                due_within_days=due_within_days,
                silence_after_hours=silence_after_hours,
                limit=limit,
            ),
        )
        rows = approvals.listing(self.open(project_id), caller.rank, "pending", 200)
        view["approvals"] = continuity.approvals_section(list(reversed(rows)), limit)
        counts, claim_gaps = self._read_as(
            project_id, caller, lambda c, d: _map_counts_and_gaps(c, d, caller)
        )
        view["research_map"] = counts
        view["brief"] = brief.build(view, claim_gaps)
        return view

    # ----- CP2: decisions ------------------------------------------------------------------

    def grant(
        self,
        project_id: str,
        *,
        operation: str,
        digest: str | None,
        ttl_seconds: int = 600,
        max_uses: int = 1,
        label: str,
        synthetic: bool = False,
    ) -> capabilities.Grant:
        store = self.open(project_id)
        conn = store.connect()
        try:
            with db.write_transaction(conn):
                return capabilities.issue(
                    conn,
                    project_id=project_id,
                    operation=operation,
                    digest=digest,
                    ttl_seconds=ttl_seconds,
                    max_uses=max_uses,
                    label=label,
                    synthetic=synthetic,
                )
        finally:
            conn.close()

    def revoke(self, project_id: str, capability_id: str) -> bool:
        store = self.open(project_id)
        conn = store.connect()
        try:
            with db.write_transaction(conn):
                return capabilities.revoke(conn, capability_id)
        finally:
            conn.close()

    def owner_caller(
        self, project_id: str, secret: str | None, operation: str = "read_sensitive"
    ) -> reads.Caller:
        """Exchange an owner capability for owner scope (counts one use)."""
        store = self.open(project_id)
        conn = store.connect()
        try:
            with db.write_transaction(conn):
                capabilities.consume(
                    conn, secret=secret, project_id=project_id, operation=operation, digest=None
                )
        finally:
            conn.close()
        return reads.Caller(
            actor=self.actor, scope="owner", full_data=operation == "full_data_export"
        )

    def review_request(
        self, project_id: str, raw: bytes, caller: reads.Caller | None = None
    ) -> dict[str, Any]:
        """Pure preview: protected effects and the exact digest a capability must bind.

        The owner's interactive review runs with owner visibility so it can approve
        changes that touch restricted records; agents preview with agent visibility."""
        rank = 2 if caller is not None and caller.scope == "owner" else 0
        return apply_mod.preview(self.open(project_id), raw, max_rank=rank)

    # ----- CP10.2: pending approvals (A02; decision 0016) ----------------------------------

    def approval_request(
        self,
        project_id: str,
        caller: reads.Caller,
        operation: str,
        params: Any,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Store the exact plan of a protected operation the caller cannot authorise and
        raise ``approval_required`` (always; nothing else commits). ``params`` are the
        operation's parameters; the plan also binds the current base (a plan digest, a
        revision, a slot state) so a changed base needs a new request."""
        kind = approval_kinds.KINDS.get(operation)
        if kind is None or kind.params is None:
            raise PrometheosError(
                "validation_error", "this operation is not requested this way", {}
            )
        if request_id is not None and not is_request_id(request_id):
            raise PrometheosError("validation_error", "request_id must be a canonical UUID4", {})
        parsed = approval_kinds.parse_params(kind, params)
        store = self.open(project_id)
        built = kind.build(self, project_id, store, caller, parsed)
        row = approvals.create(
            store,
            operation=operation,
            caller_rank=caller.rank,
            actor=caller.actor,
            request_id=request_id,
            plan=built.plan,
            required_scope=kind.scope,
            summary=built.summary,
        )
        if operation in RUN_APPROVALS:
            self._waiting_run_for_approval(project_id, caller, row)
        raise approvals.answer(row)

    def approval_list(
        self, project_id: str, caller: reads.Caller, state: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """Approval requests the caller may see (made at or below its rank); expired ones are
        marked expired first. Read-only apart from that bookkeeping."""
        if not 1 <= limit <= 200:
            raise PrometheosError("validation_error", "limit must be 1..200", {})
        rows = approvals.listing(self.open(project_id), caller.rank, state, limit)
        return {"approvals": [approvals.view(r) for r in rows]}

    def approval_get(
        self, project_id: str, caller: reads.Caller, approval_id: str
    ) -> dict[str, Any]:
        return approvals.view(approvals.get(self.open(project_id), approval_id, caller.rank))

    def approval_withdraw(
        self, project_id: str, caller: reads.Caller, approval_id: str
    ) -> dict[str, Any]:
        """The requesting scope ends its own request (a granted one loses its grant)."""
        return approvals.view(approvals.withdraw(self.open(project_id), approval_id, caller.rank))

    def approval_resume(
        self,
        project_id: str,
        caller: reads.Caller,
        approval_id: str,
        *,
        backend: capture.Backend | None = None,
        start_capture: bool = True,
        capture_owner: object = None,
    ) -> dict[str, Any]:
        """Execute a granted request's stored plan once, with the server-held grant consumed
        in the operation's own transaction. A used request answers its stored result
        (``replayed``); pending is ``approval_required``; denied, expired, withdrawn and
        revoked are ``policy_denied``. Only the rank that made the request may resume it."""
        store = self.open(project_id)
        row = approvals.for_resume(store, approval_id, caller.rank)
        return self._execute_approval(
            project_id, store, row, caller, backend=backend, capture_owner=capture_owner,
            start_capture=start_capture,
        )  # fmt: skip

    def _execute_approval(
        self,
        project_id: str,
        store: ProjectStore,
        row: operational.ApprovalRequest,
        caller: reads.Caller,
        *,
        backend: capture.Backend | None,
        capture_owner: object,
        start_capture: bool,
    ) -> dict[str, Any]:
        """Run one request's plan as the rank that made it (never above it)."""
        if row.state == "used":
            return {
                "approval_request_id": row.id,
                "operation": row.operation,
                "state": "used",
                "replayed": True,
                "result": row.result,
            }
        kind = approval_kinds.KINDS.get(row.operation)
        if kind is None or row.capability_id is None:
            raise PrometheosError("unsupported", "this approval request cannot be resumed", {})
        grant = capabilities.HeldGrant(row.capability_id, row.id)
        runner = approval_kinds.caller_for(row, f"{caller.actor}:approval")
        try:
            raw_result = kind.execute(
                self, project_id, store, row, grant, runner,
                {"backend": backend, "capture_owner": capture_owner},
            )  # fmt: skip
        except PrometheosError as exc:
            after = approvals.state_of(store, row.id)
            # The grant was spent before the effect failed: keep why. A policy_denied here is a
            # concurrent resume that lost the race for the grant; it never records anything.
            if after == "used" and exc.code != "policy_denied":
                approvals.record_result(
                    store, row.id, exc.to_response().model_dump(mode="json"), failure=True
                )
            raise
        result: dict[str, Any] = loads_internal(dumps_canonical(raw_result))
        approvals.record_result(store, row.id, result)
        if row.operation == "recording_start" and start_capture:
            self._background_capture(
                project_id, result["session_id"], backend, int(result["max_seconds"])
            )
        return {
            "approval_request_id": row.id,
            "operation": row.operation,
            "state": "used",
            "replayed": False,
            "result": result,
        }

    def _background_capture(
        self,
        project_id: str,
        session_id: str,
        backend: capture.Backend | None,
        max_seconds: int,
    ) -> None:
        import threading

        def body() -> None:
            try:
                self.recording_run(project_id, session_id, backend, max_seconds=max_seconds)
            except PrometheosError:
                pass  # the session row carries the state and reason

        threading.Thread(target=body, daemon=True).start()

    # ----- CP10.3: durable operation runs (A02; decision 0017) -------------------------------

    def _run_env(self, project_id: str) -> runs.Env:
        return runs.Env(service=self, project_id=project_id, store=self.open(project_id))

    def _run_view(self, env: runs.Env, caller: reads.Caller, run_id: str) -> dict[str, Any]:
        conn = env.store.connect(readonly=True)
        try:
            with db.read_snapshot(conn):
                run = runs.visible(conn, run_id, caller.rank)
                return runs.redact_view(conn, caller, runs.view(conn, run))
        finally:
            conn.close()

    def _link_or_create(
        self,
        env: runs.Env,
        caller: reads.Caller,
        operation: str,
        params: dict[str, Any],
        *,
        request_id: str | None,
        budget: dict[str, str] | None,
        job: tuple[str, str] | None,
        authorize: Callable[[sqlite3.Connection], None] | None,
        fingerprint: runs.Fingerprint | None,
        approval_request_id: str | None,
    ) -> tuple[operational.OperationRun, bool]:
        """Create the run, or (for a resumed approval request) take over the run that has been
        waiting on that request: the same run ID from the request to the result."""
        if approval_request_id is not None:
            taken = self._take_waiting_run(env, approval_request_id, params, authorize)
            if taken is not None:
                return taken, True
        return runs.create(
            env, caller, operation, params, request_id=request_id, budget=budget, job=job,
            authorize=authorize, fingerprint=fingerprint,
        )  # fmt: skip

    def _take_waiting_run(
        self,
        env: runs.Env,
        approval_request_id: str,
        params: dict[str, Any],
        authorize: Callable[[sqlite3.Connection], None] | None,
    ) -> operational.OperationRun | None:
        canonical = loads_internal(dumps_canonical(params))
        conn = env.store.connect()
        try:
            with db.write_transaction(conn):
                rows = conn.execute(
                    "SELECT * FROM operation_runs WHERE state = 'waiting_for_input' AND "
                    "json_extract(waiting_on, '$.approval_request_id') = ? ORDER BY created_at",
                    (approval_request_id,),
                ).fetchall()
                waiting = [operational.from_row(operational.OperationRun, r) for r in rows]
                run = next((w for w in waiting if w.params == canonical), None)
                if run is None:
                    return None  # recovery closes a waiting run whose approval was used (F13)
                if authorize is not None:
                    authorize(conn)
                queued = runs.with_update(
                    run, {"state": "queued", "waiting_on": None, "updated_at": runs.stamp()}
                )
                runs._save(conn, queued)
                runs._log(conn, run.id, "info", f"approval {approval_request_id} granted: queued")
                return queued
        finally:
            conn.close()

    def _waiting_run_for_approval(
        self, project_id: str, caller: reads.Caller, row: operational.ApprovalRequest
    ) -> None:
        """A protected long operation requested by an agent sits in ``waiting_for_input`` on its
        approval request (``waiting_on``) from the moment it is requested (decision 0017)."""
        params = _approval_run_params(row)
        if params is None:
            return
        if row.state != "pending":
            return  # granted or ended: the run (if any) has moved on
        env = self._run_env(project_id)
        conn = env.store.connect(readonly=True)
        try:
            found = conn.execute(
                "SELECT 1 FROM operation_runs WHERE "
                "json_extract(waiting_on, '$.approval_request_id') = ?",
                (row.id,),
            ).fetchone()
        finally:
            conn.close()
        if found is not None:
            return
        runs.create(
            env, caller, row.operation, params,
            waiting_on={"kind": "approval", "approval_request_id": row.id},
            log=f"waiting on approval {row.id}",
        )  # fmt: skip

    def run_execute_sync(
        self,
        project_id: str,
        caller: reads.Caller,
        operation: str,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
        budget: dict[str, str] | None = None,
        job: tuple[str, str] | None = None,
        authorize: Callable[[sqlite3.Connection], None] | None = None,
        fingerprint: runs.Fingerprint | None = None,
        options: dict[str, Any] | None = None,
        approval_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a run and execute it in this thread (callers that wait: the CLI default, the
        approval resume, the in-process API). Returns the operation's result with its
        ``run_id``; a failure is raised with the operation's own error after it was recorded."""
        env = self._run_env(project_id)
        run, created = self._link_or_create(
            env, caller, operation, params, request_id=request_id, budget=budget, job=job,
            authorize=authorize, fingerprint=fingerprint,
            approval_request_id=approval_request_id,
        )  # fmt: skip
        if self.on_run_created is not None:
            self.on_run_created(run.id, operation)
        if created or run.state == "queued":
            run = runs.execute(env, run.id, actor=self.actor, options=options, raise_errors=True)
        return self._sync_result(env, run)

    def _sync_result(self, env: runs.Env, run: operational.OperationRun) -> dict[str, Any]:
        conn = env.store.connect(readonly=True)
        try:
            result = runs.stage_output(conn, run.id, "result")
        finally:
            conn.close()
        if run.state == "completed" and result is not None:
            return result | {"run_id": run.id}
        if run.error is not None:
            error = run.error
            details = dict(error.details)
            if error.code not in ("approval_required", "capability_unavailable"):
                details["run_id"] = run.id
            raise PrometheosError(error.code, error.message, details)
        raise PrometheosError(
            "stale_task",
            f"the run ended as {run.state} before it completed",
            {"run_id": run.id, "state": run.state},
        )

    def run_start(
        self,
        project_id: str,
        caller: reads.Caller,
        operation: str,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
        budget: dict[str, str] | None = None,
        job: tuple[str, str] | None = None,
        authorize: Callable[[sqlite3.Connection], None] | None = None,
        fingerprint: runs.Fingerprint | None = None,
        mode: str = "background",
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        """Create a run and answer with its view at once (``oprun_`` ID, state). ``mode``:
        ``background`` executes it in a thread of this long-lived process (hub, MCP server)
        and waits at most ``wait_seconds`` (0-20) for it to finish; ``detached`` starts a
        ``prometheos run resume`` worker process (CLI ``--no-wait``); ``none`` only queues it."""
        if not 0 <= wait_seconds <= runs.MAX_WAIT_SECONDS:
            raise PrometheosError("validation_error", "wait_seconds must be 0..20", {})
        env = self._run_env(project_id)
        run, created = runs.create(
            env, caller, operation, params, request_id=request_id, budget=budget, job=job,
            authorize=authorize, fingerprint=fingerprint,
        )  # fmt: skip
        if created or run.state == "queued":
            self._dispatch(env, run.id, mode, wait_seconds)
        return self._run_view(env, caller, run.id)

    def _dispatch(self, env: runs.Env, run_id: str, mode: str, wait_seconds: float) -> None:
        if mode == "background":
            runs.execute_in_background(env, run_id, actor=self.actor)
            if wait_seconds:
                runs.wait(env, run_id, wait_seconds)
        elif mode == "detached":
            runs.spawn_worker(self.home, env.project_id, run_id, self.worker_args)
        elif mode != "none":
            raise PrometheosError("validation_error", "unknown run mode", {"mode": mode})

    def run_dispatch(self, project_id: str, run_id: str, mode: str) -> None:
        """Execute an existing queued run in the background or a detached worker."""
        self._dispatch(self._run_env(project_id), run_id, mode, 0)

    def run_get(self, project_id: str, caller: reads.Caller, run_id: str) -> dict[str, Any]:
        """One run as every transport shows it (status, progress, bounded logs, artifacts,
        limitations, result). Interrupted runs are made truthful first (recovery)."""
        env = self._run_env(project_id)
        runs.recover(env)
        return self._run_view(env, caller, run_id)

    def run_list(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        state: str | None = None,
        operation: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        env = self._run_env(project_id)
        runs.recover(env)
        conn = env.store.connect(readonly=True)
        try:
            with db.read_snapshot(conn):
                rows = runs.listing(
                    conn, caller.rank, state=state, operation=operation, limit=limit
                )
                return {"runs": [runs.redact_view(conn, caller, runs.view(conn, r)) for r in rows]}
        finally:
            conn.close()

    def run_cancel(self, project_id: str, caller: reads.Caller, run_id: str) -> dict[str, Any]:
        """``cancelled`` when nothing executes the run; ``cancel_requested`` (a request, not a
        confirmed stop) while a worker does, until it confirms at its next stage boundary."""
        env = self._run_env(project_id)
        done = runs.cancel(env, caller, run_id)
        return done | {"run": self._run_view(env, caller, done["run_id"])}

    def run_resume(
        self,
        project_id: str,
        caller: reads.Caller,
        run_id: str,
        *,
        budget_amount: str | None = None,
        mode: str = "background",
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        """Resume a queued, failed or budget-paused run when its inputs, parameters,
        configuration, model and workflow version are unchanged (else ``stale_task``: start a
        new run). A run waiting on an approval resumes through that approval (the same
        semantics as ``approval resume``). ``mode="foreground"`` executes it in this thread."""
        if not 0 <= wait_seconds <= runs.MAX_WAIT_SECONDS:
            raise PrometheosError("validation_error", "wait_seconds must be 0..20", {})
        env = self._run_env(project_id)
        runs.recover(env)
        conn = env.store.connect(readonly=True)
        try:
            run = runs.visible(conn, run_id, caller.rank)
        finally:
            conn.close()
        if run.state == "waiting_for_input" and run.waiting_on is not None:
            if run.waiting_on.kind == "approval":
                self.approval_resume(project_id, caller, run.waiting_on.approval_request_id)
                return self._run_view(env, caller, run.id)
        queued = runs.resume(env, caller, run.id, budget_amount=budget_amount)
        if mode == "foreground":
            runs.execute(env, queued.id, actor=self.actor)
        else:
            self._dispatch(env, queued.id, mode, wait_seconds)
        return self._run_view(env, caller, queued.id)

    def run_acknowledge(
        self, project_id: str, caller: reads.Caller, run_id: str, note: str | None = None
    ) -> dict[str, Any]:
        """Close a run whose external call has an unknown outcome (``unknown_external``): it
        becomes ``failed`` with the uncertainty recorded; nothing is sent again."""
        env = self._run_env(project_id)
        done = runs.acknowledge(env, caller, run_id, note)
        return self._run_view(env, caller, done.id)

    def run_recover(self, project_id: str) -> list[str]:
        """Make interrupted runs truthful (server start); runs nothing."""
        return runs.recover(self._run_env(project_id))

    # Owner channels only (paired hub session, interactive CLI): never MCP or an agent route.

    def approval_queue(self, project_id: str, state: str | None = None) -> dict[str, Any]:
        """The owner's review queue. Without ``state``: every open request and the 50 most
        recent ended ones; with ``state``: the newest 200 in that state. No previews (they are
        computed per request by ``approval_owner_view``, review F12)."""
        store = self.open(project_id)
        rows = approvals.queue(store) if state is None else approvals.listing(store, 2, state, 200)
        return {"approvals": [approvals.view(r) for r in rows]}

    def approval_owner_view(self, project_id: str, approval_id: str) -> dict[str, Any]:
        """One request as the owner reviews it, with its full effect (review F2)."""
        store = self.open(project_id)
        return self._owner_rendering(project_id, store, approvals.get(store, approval_id, 2))

    def _owner_rendering(
        self, project_id: str, store: ProjectStore, row: operational.ApprovalRequest
    ) -> dict[str, Any]:
        """The request as the owner reviews it. An ``apply`` plan gets the same server
        preview as ``decision review`` (protected effects, digest, whether it still applies),
        computed with owner visibility. A push or purge gets the owner's full view (every
        file, restricted ones named; the closure) and whether it still matches the digest the
        plan binds. Other kinds carry their full effect in the plan itself."""
        shown = approvals.view(row)
        preview: dict[str, Any] | None = None
        if row.state == "pending":
            if row.operation == "apply" and row.request_id:
                raw = dumps_canonical({**row.plan, "request_id": row.request_id})
                preview = apply_mod.preview(store, raw, max_rank=2)
            elif row.operation in approval_kinds.OWNER_REVIEWS:
                try:
                    preview = approval_kinds.OWNER_REVIEWS[row.operation](self, project_id, row)
                except PrometheosError as exc:
                    preview = {"kind": row.operation, "error": exc.message, "matches_plan": False}
        rendered: dict[str, Any] = loads_internal(dumps_canonical(shown | {"preview": preview}))
        return rendered

    def approval_grant_receipt(self, project_id: str, approval_id: str) -> dict[str, Any]:
        """Owner channels only: what the grant of a request issued (the ``capability_grant_
        receipt`` contract; never the secret, which nobody holds)."""
        from prometheos.domain.models import CapabilityGrantReceipt

        store = self.open(project_id)
        row = approvals.get(store, approval_id, 2)
        if row.capability_id is None:
            raise PrometheosError("validation_error", "this request was not granted", {})
        conn = store.connect(readonly=True)
        try:
            cap = conn.execute(
                "SELECT * FROM capabilities WHERE id = ?", (row.capability_id,)
            ).fetchone()
        finally:
            conn.close()
        # Review R2-7: a server-held grant is named by its request, never by capability ID.
        receipt = CapabilityGrantReceipt(
            capability_id=None, operation=cap["operation"], digest=cap["digest"],
            expires_at=cap["expires_at"], max_uses=cap["max_uses"], server_held=True,
            approval_request_id=row.id,
        )  # fmt: skip
        return receipt.model_dump(mode="json")

    def approval_start_recording(
        self,
        project_id: str,
        approval_id: str,
        *,
        owner: object,
        plan_sha256: str,
        backend: capture.Backend | None = None,
        start_capture: bool = True,
    ) -> dict[str, Any]:
        """Owner channels only (review F1): start a granted recording where the owner sees it
        (the paired hub, whose recording banner shows it with a stop control). It runs as the
        requesting rank, within the plan's start window and for at most its duration."""
        act = require_owner(owner, "starting a granted recording")
        store = self.open(project_id)
        row = approvals.get(store, approval_id, 2)
        if row.operation != "recording_start":
            raise PrometheosError("validation_error", "this request is not a recording", {})
        if plan_sha256 != row.plan_sha256:
            raise PrometheosError("revision_conflict", "the plan differs from the one shown", {})
        if row.state not in ("granted", "used"):
            raise approvals.answer(row)
        runner = approval_kinds.caller_for(row, f"{self.actor}:approval")
        return self._execute_approval(
            project_id, store, row, runner, backend=backend, capture_owner=act,
            start_capture=start_capture,
        )  # fmt: skip

    def approval_stop(
        self, project_id: str, caller: reads.Caller, approval_id: str, cancel: bool = False
    ) -> dict[str, Any]:
        """Stop (or cancel) the recording an approved request started. The requesting scope
        may always stop what it caused (decision 0013's creator rule), even a restricted
        session it cannot read; the owner may always stop."""
        store = self.open(project_id)
        row = approvals.get(store, approval_id, caller.rank)
        # ``approvals.get`` already binds it: an agent sees only requests made at its rank, the
        # owner sees every request (and may always stop a recording).
        session_id = (row.result or {}).get("session_id")
        if row.operation != "recording_start" or row.state != "used" or not session_id:
            raise PrometheosError(
                "validation_error", "this request started no recording", {"state": row.state}
            )
        controller = reads.Caller(actor=f"{caller.actor}:approval-stop", scope="owner")
        done = self.recording_control(
            project_id, controller, str(session_id), "cancel" if cancel else "stop"
        )
        return {"approval_request_id": row.id, "session_id": session_id, "state": done.get("state")}

    def approval_decide(
        self,
        project_id: str,
        approval_id: str,
        *,
        grant: bool,
        plan_sha256: str,
        owner: object,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Grant or deny exactly the plan the owner saw. A grant creates a single-use
        exact-plan capability held by the server; its secret is never returned."""
        act = require_owner(owner, "deciding an approval request")
        store = self.open(project_id)
        row = approvals.get(store, approval_id, 2)
        kind = approval_kinds.KINDS.get(row.operation)
        if kind is None:
            raise PrometheosError("unsupported", "unknown approval operation", {})
        review = approval_kinds.OWNER_REVIEWS.get(row.operation)
        if grant and review is not None and row.state == "pending":
            # Review R2-4: a push or purge whose owner view changed since the request cannot be
            # granted at all (the hub disables Grant; the server refuses it too).
            if not review(self, project_id, row).get("matches_plan"):
                raise PrometheosError(
                    "revision_conflict",
                    "what this request would do changed since it was made; deny it (the agent "
                    "can request again)",
                    {"approval_request_id": row.id},
                )
        decided = approvals.decide(
            store,
            approval_id,
            grant=grant,
            plan_sha256=plan_sha256,
            grant_digest=kind.digest(row.plan) if grant else None,
            decided_by=act.actor,
            note=note,
        )
        return approvals.view(decided)

    def approval_revoke(
        self, project_id: str, approval_id: str, *, owner: object
    ) -> dict[str, Any]:
        """The owner revokes a grant before it is used (its capability is revoked)."""
        require_owner(owner, "revoking an approval grant")
        return approvals.view(approvals.revoke(self.open(project_id), approval_id))

    # ----- CP2: purge and capture ---------------------------------------------------------

    def purge_plan(
        self, project_id: str, targets: list[str], caller: reads.Caller | None = None
    ) -> dict[str, Any]:
        """Owner only: the closure names restricted records and reveals existence."""
        if caller is None or caller.scope != "owner":
            raise PrometheosError("policy_denied", "purge planning needs owner scope", {})
        return purge.plan(self.open(project_id), targets)

    def history(self, project_id: str, caller: reads.Caller, record_id: str) -> dict[str, Any]:
        return self._read_as(project_id, caller, lambda c, d: reads.history(c, caller, record_id))

    def purge_plan_get(
        self, project_id: str, plan_id: str, caller: reads.Caller | None = None
    ) -> dict[str, Any]:
        if caller is None or caller.scope != "owner":
            raise PrometheosError("policy_denied", "purge plans are owner-only", {})
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT plan FROM purge_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise PrometheosError("not_found", "purge plan does not exist", {"plan_id": plan_id})
        plan: dict[str, Any] = loads_internal(row["plan"])
        return plan

    def purge_commit(
        self, project_id: str, plan_id: str, secret: CapabilitySecret
    ) -> dict[str, Any]:
        store = self.open(project_id)
        env = runs.Env(self, project_id, store)
        owner = reads.Caller(actor=self.actor, scope="owner")

        def stop_transcriptions(closure: list[str]) -> None:
            # A recording being purged must not go on being transcribed: ask its runs to stop
            # (the worker ends at its next stage boundary) before anything is deleted.
            transcribe.cancel_active(env, owner, closure)

        return purge.commit(store, plan_id, secret, self.actor, before_commit=stop_transcriptions)

    def capture_meeting(
        self, project_id: str, caller: reads.Caller | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Agent scope unless an owner caller is given (decision 0014 item 1)."""
        caller = caller or reads.Caller(actor=self.actor)
        return meetings.capture(
            self.open(project_id), actor=self.actor, max_rank=caller.rank, **kwargs
        )

    # ----- CP3: external-agent tasks ------------------------------------------------------

    def task_prepare(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        return tasks.prepare(self.open(project_id), actor=self.actor, **kwargs)

    def task_get(
        self, project_id: str, task_id: str, caller: reads.Caller | None = None
    ) -> dict[str, Any]:
        result = tasks.get(self.open(project_id), task_id, caller)
        return self._read_as(project_id, caller or reads.Caller(actor="task"), lambda c, d: result)

    def task_submit(
        self, project_id: str, task_id: str, result: bytes, request_id: str
    ) -> dict[str, Any]:
        return tasks.submit(self.open(project_id), task_id, result, request_id, self.actor)

    def task_cancel(self, project_id: str, task_id: str, request_id: str) -> dict[str, Any]:
        return tasks.cancel(self.open(project_id), task_id, request_id, self.actor)

    # ----- CP4: skill packages and recipe runs ---------------------------------------------

    @property
    def catalog(self) -> packages.Catalog:
        return packages.Catalog(self.home)

    def skill_inspect(self, source: Path) -> dict[str, Any]:
        entries, origin = packages.read_source(source)
        return packages.inspect(entries, self.catalog) | {"source": origin}

    def skill_import_plan(self, source: Path) -> dict[str, Any]:
        return packages.import_plan(self.catalog, source)

    def skill_import(self, source: Path, plan_sha256: str | None) -> dict[str, Any]:
        return packages.import_commit(self.catalog, source, plan_sha256)

    def skill_catalog(self) -> dict[str, Any]:
        return packages.list_catalog(self.catalog)

    def skill_verify(self, ref: str) -> dict[str, Any]:
        return packages.verify(self.catalog, ref)

    def skill_revoke(self, ref: str, target: Path | None = None) -> dict[str, Any]:
        """Revoke; with a project folder, also offer reviewed removal plans for its installs
        (nothing is removed until a plan is applied with ``skill remove``)."""
        name = self.catalog.get(ref)["name"]
        plans = None
        if target is not None:  # computed first: a bad lock fails before anything changes
            installs = packages.load_lock(target.resolve())["installs"]
            plans = [
                packages.remove_plan(target, name, i["host"]) for i in installs if i["name"] == name
            ]
        result = packages.set_status(self.catalog, ref, revoke=True, reviewer=self.actor)
        if plans is not None:
            result["removal_plans"] = plans
        return result

    def skill_trust(self, ref: str, *, authority: object) -> dict[str, Any]:
        """Owner decision (A02, CP10.2): ``authority`` is an :class:`OwnerAct`; an agent
        requests it (``approval_request``) and resumes after the owner's grant."""
        act = require_owner(authority, "restoring trust in a skill package")
        return packages.set_status(self.catalog, ref, revoke=False, reviewer=act.actor)

    def skill_install(
        self,
        ref: str,
        host: str,
        target: Path,
        plan_sha256: str | None,
        resolve: str | None = None,
        project_id: str | None = None,
        *,
        authority: object = None,
    ) -> dict[str, Any]:
        if plan_sha256 is None:
            return {"plan": packages.install_plan(self.catalog, ref, host, target, resolve)}
        require_owner(authority, "installing or updating host skill instructions")
        result = packages.install_commit(self.catalog, ref, host, target, plan_sha256, resolve)
        if project_id is not None:
            entry = self.catalog.get(result["installed"])
            result["record"] = workflows.record_package(
                self.open(project_id),
                entry,
                target.resolve() / packages.LOCK_NAME,
                "installed",
                new_request_id(),
                self.actor,
            )
        return result

    def skill_remove(
        self,
        name: str,
        host: str,
        target: Path,
        plan_sha256: str | None,
        project_id: str | None = None,
        *,
        authority: object = None,
    ) -> dict[str, Any]:
        if plan_sha256 is None:
            return {"plan": packages.remove_plan(target, name, host)}
        require_owner(authority, "removing host skill instructions")
        installed = packages.load_lock(target.resolve())
        current = next(
            (i for i in installed["installs"] if i["name"] == name and i["host"] == host), None
        )
        result = packages.remove_commit(target, name, host, plan_sha256)
        if project_id is not None and current is not None:
            # One record per package and project folder: it stays installed while another
            # host projection of the same package remains in the lock.
            remaining = [
                i for i in packages.load_lock(target.resolve())["installs"] if i["name"] == name
            ]
            source = remaining[-1] if remaining else current
            entry = self.catalog.find_digest(source["package_digest"])
            if entry is not None:
                result["record"] = workflows.record_package(
                    self.open(project_id),
                    entry,
                    target.resolve() / packages.LOCK_NAME,
                    "installed" if remaining else "removed",
                    new_request_id(),
                    self.actor,
                )
        return result

    def skill_export(self, ref: str, out: Path, draft: bool = False) -> dict[str, Any]:
        return packages.export(self.catalog, ref, out, draft)

    def skill_adapt(self, ref: str, new_name: str, out: Path) -> dict[str, Any]:
        return packages.adapt(self.catalog, ref, new_name, out)

    def skill_create(
        self, name: str, description: str, license_expression: str | None, out: Path
    ) -> dict[str, Any]:
        return packages.create(name, description, license_expression, out)

    def skill_refresh(self, folder: Path) -> dict[str, Any]:
        return packages.refresh_sidecar(folder, self.catalog)

    def workflow_render(self, ref: str, recipe_id: str, inputs: Any) -> dict[str, Any]:
        return packages.render_recipe(self.catalog, ref, recipe_id, inputs)

    def workflow_start(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        return workflows.start(self.open(project_id), self.catalog, actor=self.actor, **kwargs)

    def workflow_step(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        return workflows.step(self.open(project_id), self.catalog, actor=self.actor, **kwargs)

    def workflow_finish(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        return workflows.finish(self.open(project_id), self.catalog, actor=self.actor, **kwargs)

    def workflow_status(
        self, project_id: str, run_id: str, caller: reads.Caller | None = None
    ) -> dict[str, Any]:
        who = caller or reads.Caller(actor=self.actor)
        return self._read_as(
            project_id,
            who,
            lambda c, d: workflows.status(c, d, self.catalog, run_id, who),
        )

    # ----- CP5: hub support -----------------------------------------------------------------

    def changes(
        self, project_id: str, caller: reads.Caller, after_seq: int, limit: int = 500
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: reads.changes(c, caller, after_seq, limit)
        )

    def latest_seq(self, project_id: str) -> int:
        """The committed project sequence alone: the hub change feed's shared probe."""
        return self._read(project_id, lambda c, d: reads.project_seq(c))

    def version_text(
        self,
        project_id: str,
        caller: reads.Caller,
        version_id: str,
        offset: int = 0,
        limit: int = 200_000,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: reads.version_text(c, d, caller, version_id, limit, offset),
        )

    def research_root(self, project_id: str, alias: str) -> Path:
        """The folder of an approved research root of the project (agent transports resolve
        every file operand inside one; decision 0022)."""
        conn = self.open(project_id).connect(readonly=True)
        try:
            settings, _ = sources._settings(conn, project_id)
        finally:
            conn.close()
        return sources.root_path(settings, alias)

    def request_file(self, project_id: str, alias: str, relative: str) -> bytes:
        """The bytes of a request file inside an approved research root (decision 0079, P22):
        resolved without links or short names like every agent file operand, refused when it is
        not a regular file or exceeds the request limit. A missing file says which one and that a
        retry does not help (P28)."""
        import os

        from prometheos.sources.paths import open_verified, resolve_in_root

        resolved = resolve_in_root(self.research_root(project_id, alias), relative)
        where = {"root_alias": alias, "path": resolved.relative}
        if not os.path.lexists(resolved.path):
            raise PrometheosError("validation_error", "request file not found", where)
        if not resolved.path.is_file():
            raise PrometheosError("validation_error", "request file is not a regular file", where)
        # Opened like every agent file operand (identity and reparse checks at open), and read
        # capped, so a file that grows or is swapped after the checks is still bounded (F3).
        with open_verified(resolved) as handle:
            raw = handle.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise PrometheosError(
                "validation_error", "request file exceeds the request size limit", where
            )
        return raw

    # ----- CP7: papers -----------------------------------------------------------------------

    def paper_check(self, project_id: str, caller: reads.Caller, paper_id: str) -> dict[str, Any]:
        """Pure readiness analysis (no write, no audit row, no sequence change)."""
        return self._read_as(project_id, caller, lambda c, d: papers.check(c, d, caller, paper_id))

    def research_compass(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        paper_id: str | None = None,
        limit: int = 12,
    ) -> dict[str, Any]:
        """Where to look next (decision 0113): ranked directions from the record for the coding
        agent, each with why, what to do and when it is done. A pure, deterministic read."""
        from prometheos.app import compass

        return self._read_as(
            project_id,
            caller,
            lambda c, d: compass.view(c, d, caller, paper_id=paper_id, limit=limit),
        )

    def paper_numbers(self, project_id: str, caller: reads.Caller, paper_id: str) -> dict[str, Any]:
        """Where every number in the manuscript comes from (decision 0111): a pure read that
        traces macros to their file, run and claims and lists numbers typed by hand."""
        from prometheos.app import paper_numbers

        return self._read_as(
            project_id, caller, lambda c, d: paper_numbers.audit(c, d, caller, paper_id)
        )

    def paper_check_save(self, project_id: str, paper_id: str, request_id: str) -> dict[str, Any]:
        """Explicit save: one generated notebook entry naming the agent-scope findings."""
        agent = reads.Caller(actor=self.actor)
        report = self.paper_check(project_id, agent, paper_id)
        if not is_request_id(request_id):
            raise PrometheosError("validation_error", "request_id must be a canonical UUID4", {})
        # The request content depends on the snapshot, so a retry is recognised by a key
        # derived from this operation and the caller's ID, never by content.
        key = derived_request_id("paper_check_save", project_id, paper_id, request_id)
        # The stored key is this operation's identity, not the snapshot-dependent body digest,
        # so a replay is checked against both the operation and the (agent) rank.
        identity = canonical_sha256(
            {"op": "paper_check_save", "project_id": project_id, "paper_id": paper_id, "key": key}
        )
        prior = self.open(project_id).prior_response(
            key, request_sha256=identity, max_rank=agent.rank
        )
        if prior is not None:
            return {"report": report, "saved": prior}
        body: dict[str, Any] = {
            "title": f"Paper readiness check: {report['title']}"[:300],
            "text": papers.report_entry_text(report),
            "category": "note",
            "evidence": [],
            "claim_refs": [],
            "author_ref": None,
            "origin": "generated",
            "quantities": [],
            "review": "unreviewed",
        }
        request = {
            "schema_version": 1,
            "project_id": project_id,
            "request_id": key,
            "expected_seq": report["project_seq"],
            "operations": [
                {
                    "op": "create",
                    "kind": "entry",
                    "id": new_id("entry"),
                    "base_rev": 0,
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": body,
                }
            ],
        }
        commit = apply_request(
            self.open(project_id),
            dumps_canonical(request),
            self.actor,
            max_rank=agent.rank,
            request_key_sha256=identity,
        )
        return {"report": report, "saved": commit}

    def paper_export(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        out: Path,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: papers.export(c, d, caller, paper_id, out, version_id),
        )

    def paper_handoff_plan(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        into: Path,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: papers.handoff_plan(c, d, caller, paper_id, into, version_id),
        )

    def paper_handoff_apply(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        into: Path,
        digest: str,
        overwrite: list[str],
        version_id: str | None = None,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: papers.handoff_apply(
                c, d, caller, paper_id, into, digest, overwrite, version_id
            ),
        )

    def requirements_draft(
        self, project_id: str, caller: reads.Caller, version_id: str
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: papers.requirements_draft(c, d, caller, version_id)
        )

    # ----- CP7: recurring jobs ----------------------------------------------------------------

    def job_plan(self, project_id: str, caller: reads.Caller, body: Any) -> dict[str, Any]:
        return jobs.plan(self.open(project_id), caller, body)

    def job_add(
        self, project_id: str, caller: reads.Caller, body: Any, request_id: str
    ) -> dict[str, Any]:
        return jobs.add(self.open(project_id), caller, body, request_id, self.actor)

    def job_set_enabled(
        self,
        project_id: str,
        caller: reads.Caller,
        job_id: str,
        base_rev: int,
        enabled: bool,
        request_id: str,
    ) -> dict[str, Any]:
        return jobs.set_enabled(
            self.open(project_id), caller, job_id, base_rev, enabled, request_id, self.actor
        )

    def job_cancel(
        self, project_id: str, caller: reads.Caller, job_id: str, due_utc: str
    ) -> dict[str, Any]:
        return jobs.cancel(self.open(project_id), caller, job_id, due_utc)

    def job_resolve_slot(
        self,
        project_id: str,
        caller: reads.Caller,
        job_id: str,
        due_utc: str,
        resolution: str,
        reason: str,
        capability: CapabilitySecret = None,
    ) -> dict[str, Any]:
        """Record the outcome of an ``unknown`` slot (never automatic). Needs an exact-plan
        owner decision capability bound to :meth:`job_resolve_digest`."""
        return jobs.resolve_slot(
            self.open(project_id), caller, job_id, due_utc, resolution, reason, capability
        )

    def job_resolve_digest(
        self, project_id: str, job_id: str, due_utc: str, resolution: str, reason: str
    ) -> str:
        """Pure: the digest a capability must bind to resolve this slot this way."""
        return jobs.resolve_digest(project_id, job_id, due_utc, resolution, reason)

    def job_list(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        return self._read_as(project_id, caller, lambda c, d: jobs.listing(c, caller))

    def job_slots(self, project_id: str, caller: reads.Caller, job_id: str) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: {"job_id": job_id, "slots": jobs.slots(c, caller, job_id)},
        )

    def jobs_tick(self, project_id: str, now: Any = None) -> dict[str, Any]:
        """One bounded scheduler pass, then one event detector pass; ``now`` (an aware
        datetime) is for tests only."""
        result = jobs.tick(self.open(project_id), self.actor, now=now)
        result["events"] = self.events_detect(project_id, now=now)
        return result

    # ----- CP18.2-CP18.3: research events and the agent-visible queue (decision 0035) --------

    def events_detect(self, project_id: str, now: Any = None) -> dict[str, Any]:
        """One detector pass (writes only events and detector state). It runs when a server
        starts and in every jobs tick, never inside a read and never on a timer."""
        return events.detect(self.open(project_id), now)

    def events_detect_at_start(self, project_id: str) -> dict[str, Any] | None:
        """The detector pass a starting server runs once. It never stops the server from
        starting: any failure is one line on stderr and the pass is left to the next start or
        tick (``None``), never retried in the background (review s2-a F4)."""
        try:
            return self.events_detect(project_id)
        except Exception as exc:  # a detector problem must never block a server from starting
            code = exc.code if isinstance(exc, PrometheosError) else type(exc).__name__
            sys.stderr.write(f"PrometheOS: the event detector pass did not run ({code}).\n")
            return None

    def work_queue(
        self,
        project_id: str,
        caller: reads.Caller,
        limit: int = events.QUEUE_LIMIT,
        proposed: str = "fold",
    ) -> dict[str, Any]:
        """Open work items and unacknowledged events with reasons. Read-only: it runs no
        detector, expires nothing and claims nothing. Suggested items are folded into groups
        unless ``proposed="list"`` (decision 0078)."""
        return self._read_as(
            project_id, caller, lambda c, d: events.queue(c, caller, limit=limit, proposed=proposed)
        )

    def event_list(self, project_id: str, caller: reads.Caller, **kwargs: Any) -> dict[str, Any]:
        return self._read_as(project_id, caller, lambda c, d: events.listing(c, caller, **kwargs))

    def event_acknowledge(
        self,
        project_id: str,
        event_ids: list[str],
        note: str | None = None,
        *,
        authority: object,
    ) -> dict[str, Any]:
        """The owner acknowledges events (an owner act: the paired hub or an interactive
        terminal). An agent requests it instead (``approval_request``, kind
        ``event_acknowledge``)."""
        act = require_owner(authority, "acknowledging an event")
        return events.acknowledge(self.open(project_id), event_ids, note, act.actor)

    # ----- adoption of an existing folder (A05, CP17; decision 0039) ------------------------

    # ----- A02, CP11 S11.1: the literature library (decision 0060) ------------------------------

    def library_import_plan(
        self,
        project_id: str,
        caller: reads.Caller | None = None,
        *,
        root_alias: str,
        path: str | None = None,
        sensitivity: str = "normal",
        csv_columns: dict[str, str] | None = None,
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Plan the import of the PDFs and bibliography files (.bib, .ris, .csv; S11.2) under an
        approved research root (an operation run). Agent scope unless an owner caller is given;
        nothing is written into the folder."""
        caller = caller or reads.Caller(actor=self.actor)
        params: dict[str, Any] = {
            "root_alias": root_alias,
            "path": path,
            "sensitivity": sensitivity,
        }
        if csv_columns:
            params["csv_columns"] = dict(csv_columns)
        return self._long(
            project_id, caller, "library_import_plan", params, mode=mode,
            wait_seconds=wait_seconds, request_id=request_id,
        )  # fmt: skip

    def library_import_commit(
        self,
        project_id: str,
        plan_id: str,
        caller: reads.Caller | None = None,
        *,
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Commit or replay a library plan at the caller's rank, one file per transaction (an
        operation run whose input digests are the planned bytes)."""
        from prometheos.library.importer import LibraryCommitHandler

        caller = caller or reads.Caller(actor=self.actor)
        env = self._run_env(project_id)
        handler = LibraryCommitHandler()
        fingerprint = handler.from_plan(handler.planned(env, plan_id, caller.rank))
        return self._long(
            project_id, caller, "library_import_commit", {"plan_id": plan_id}, mode=mode,
            wait_seconds=wait_seconds, request_id=request_id, fingerprint=fingerprint,
        )  # fmt: skip

    # ----- A02, CP11 S11.5: the read-only Zotero adapter (decisions 0069, 0070) ---------------

    def zotero_status(
        self, project_id: str, caller: reads.Caller, *, collections: bool = False
    ) -> dict[str, Any]:
        """The capability probe of Zotero's local API (loopback, GET only). It never raises:
        ``state`` is available, not_running, local_api_disabled or unexpected. With
        ``collections`` and an available API, the library's collections are listed too."""
        from prometheos.library import zotero

        self.open(project_id)  # the project exists
        out = zotero.status(self.home)
        if collections and out["state"] == "available":
            try:
                out["collections"] = zotero.list_collections(zotero.endpoint(self.home))
            except PrometheosError as exc:
                out["collections_error"] = exc.message
        return out

    def zotero_import_plan(
        self,
        project_id: str,
        caller: reads.Caller | None = None,
        *,
        collection: str | None = None,
        item_keys: list[str] | None = None,
        sensitivity: str = "normal",
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Plan importing items from the local Zotero (the library, one collection or named
        item keys; an operation run). Reads Zotero by GET; writes nothing into it."""
        from prometheos.library import zotero

        caller = caller or reads.Caller(actor=self.actor)
        params: dict[str, Any] = {
            "scope": zotero.scope_of(collection, item_keys),
            "sensitivity": sensitivity,
        }
        return self._long(
            project_id, caller, "zotero_import_plan", params, mode=mode,
            wait_seconds=wait_seconds, request_id=request_id,
        )  # fmt: skip

    def zotero_import_commit(
        self,
        project_id: str,
        plan_id: str,
        caller: reads.Caller | None = None,
        *,
        authority: object,
        item_keys: list[str] | None = None,
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
        approval_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Commit a Zotero plan (all its importable items, or the named keys), one item per
        transaction, re-reading each item first (an operation run). An owner act: reading the
        owner's whole reference library into the project is of the same kind as adding a
        research folder (decision 0070 section 7). ``authority`` is an :class:`OwnerAct`; an
        agent asks with ``approval_request`` (kind ``zotero_import_commit``)."""
        from prometheos.library.zotero import ZoteroCommitHandler

        require_owner(authority, "importing from the owner's Zotero library")
        caller = caller or reads.Caller(actor=self.actor, scope="owner")
        env = self._run_env(project_id)
        handler = ZoteroCommitHandler()
        keys = sorted(dict.fromkeys(item_keys)) if item_keys else None
        fingerprint = handler.from_plan(handler.planned(env, plan_id, caller.rank), keys)
        params: dict[str, Any] = {"plan_id": plan_id}
        if keys:
            params["item_keys"] = keys
        return self._long(
            project_id, caller, "zotero_import_commit", params, mode=mode,
            wait_seconds=wait_seconds, request_id=request_id, fingerprint=fingerprint,
            approval_request_id=approval_request_id,
        )  # fmt: skip

    def library_works(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        offset: int = 0,
        limit: int = 50,
        include_merged: bool = False,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: library_views.works(
                c, caller, offset=offset, limit=limit, include_merged=include_merged
            ),
        )

    # ----- CP11 S11.2: dedup, export, annotations (decision 0062) -------------------------------

    def library_dedup(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        """Similarity candidates (proposals), the decisions already made and the live
        version links (an import's link can be undone like a decision)."""
        from prometheos.library import dedup

        def read(c: Any, d: Any) -> dict[str, Any]:
            out = dedup.candidates(c, caller)
            out["decisions"] = dedup.decisions(c, caller)
            out["links"] = dedup.links(c, caller)
            return out

        return self._read_as(project_id, caller, read)

    def library_dedup_resolve(
        self,
        project_id: str,
        work_a: str,
        work_b: str,
        choice: str,
        note: str | None = None,
        *,
        authority: object,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """The owner's choice for a pair: merge, distinct or versions. An agent requests it
        instead (``approval_request``, kind ``library_dedup_resolve``)."""
        from prometheos.library import dedup

        act = require_owner(authority, "resolving a duplicate candidate")
        owner = reads.Caller(actor=act.actor, scope="owner")
        plan = self._read(project_id, lambda c, d: dedup.plan_of(c, owner, work_a, work_b, choice))
        return dedup.execute(
            self.open(project_id), plan, note, act.actor, request_id=request_id or new_request_id()
        )

    def library_dedup_undo(
        self,
        project_id: str,
        work_a: str,
        work_b: str,
        note: str | None = None,
        *,
        authority: object,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """The owner undoes a pair's decision (unmerge, unrelate, forget distinct) or an
        import's version link. An agent requests it instead (kind ``library_dedup_undo``)."""
        from prometheos.library import dedup

        act = require_owner(authority, "undoing a duplicate or version decision")
        owner = reads.Caller(actor=act.actor, scope="owner")
        plan = self._read(project_id, lambda c, d: dedup.plan_undo(c, owner, work_a, work_b))
        return dedup.execute_undo(
            self.open(project_id), plan, note, act.actor, request_id=request_id or new_request_id()
        )

    def library_export(
        self, project_id: str, caller: reads.Caller, fmt: str, work_ids: list[str] | None = None
    ) -> dict[str, Any]:
        from prometheos.library import export

        return self._read_as(
            project_id, caller, lambda c, d: export.export(c, caller, fmt, work_ids)
        )

    def library_annotations(
        self, project_id: str, caller: reads.Caller, work_id: str
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: library_views.annotations_of(c, caller, work_id)
        )

    def library_annotate(
        self,
        project_id: str,
        caller: reads.Caller,
        work_id: str,
        *,
        page: int,
        start: int,
        end: int,
        text: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """An entry citing the work's PDF with a verified locator and quote digest, plus the
        annotation text (agent scope: an agent's annotation is marked origin agent)."""
        import json as json_mod

        if not 1 <= len(text.strip()) <= 20_000:
            raise PrometheosError("validation_error", "an annotation is 1..20000 characters", {})
        cited = self._read_as(
            project_id,
            caller,
            lambda c, d: library_views.annotation_evidence(c, caller, work_id, page, start, end),
        )
        entry_id = new_id("entry")
        seq = int(self.project_info(project_id, caller)["project_seq"])
        body = {
            "title": f"Annotation on {cited['title']}, page {page}"[:300],
            "text": text,
            "category": "note",
            "evidence": [cited["evidence"]],
            "claim_refs": [],
            "author_ref": None,
            "origin": "typed" if caller.scope == "owner" else "agent",
            "quantities": [],
            "review": "unreviewed",
        }
        raw = json_mod.dumps(
            {
                "schema_version": 1,
                "project_id": project_id,
                "request_id": request_id or new_request_id(),
                "expected_seq": seq,
                "operations": [
                    {
                        "op": "create",
                        "kind": "entry",
                        "id": entry_id,
                        "base_rev": 0,
                        "sensitivity": cited["sensitivity"],
                        "occurred_at": None,
                        "body": body,
                    }
                ],
            }
        ).encode("utf-8")
        commit = self.apply(project_id, raw, caller)
        created = next((r["id"] for r in commit.get("results", []) if r.get("id")), entry_id)
        return {
            "entry_id": created,
            "work_id": work_id,
            "evidence": cited["evidence"],
            "quote": cited["quote"],
            "commit": commit,
            "content_trust": "untrusted_data",
        }

    def library_work(self, project_id: str, caller: reads.Caller, work_id: str) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: library_views.work(c, caller, work_id)
        )

    def library_search(
        self,
        project_id: str,
        caller: reads.Caller,
        query: str,
        *,
        limit: int = 20,
        work_id: str | None = None,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: library_views.search(c, caller, query, limit=limit, work_id=work_id),
        )

    def library_read(
        self, project_id: str, caller: reads.Caller, work_id: str, **where: Any
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: library_views.read(c, caller, work_id, **where)
        )

    def library_quote_check(
        self, project_id: str, caller: reads.Caller, **quoted: Any
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: library_views.quote_check(c, caller, **quoted)
        )

    # ----- CP11 S11.4b: research folders (decision 0066) ------------------------------------------

    def project_roots(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        """The approved research folders (alias, path, whether it is there now)."""
        from prometheos.app import roots

        def read(c: sqlite3.Connection, d: Path) -> dict[str, Any]:
            record = reads.current_record(c, project_id)
            assert record is not None
            return roots.listing(record["body"], int(record["rev"]))

        return self._read_as(project_id, caller, read)

    def project_root_plan(self, project_id: str, alias: str, path: str) -> dict[str, Any]:
        """The exact addition the owner approves (checked against the location rules)."""
        from prometheos.app import roots

        store = self.open(project_id)

        def read(c: sqlite3.Connection, d: Path) -> dict[str, Any]:
            record = reads.current_record(c, project_id)
            assert record is not None
            return roots.plan_add(
                record["body"], int(record["rev"]), alias, path, home=self.home,
                store=store.directory, project_id=project_id,
            )  # fmt: skip

        return self._read(project_id, read)

    def project_root_request_plan(self, project_id: str, alias: str, path: str) -> dict[str, Any]:
        """An agent's request to add a folder: checked lexically only, one refusal for every
        location rule (nothing about this computer's folders is revealed)."""
        from prometheos.app import roots

        store = self.open(project_id)

        def read(c: sqlite3.Connection, d: Path) -> dict[str, Any]:
            record = reads.current_record(c, project_id)
            assert record is not None
            return roots.plan_request(
                record["body"], int(record["rev"]), alias, path, home=self.home,
                store=store.directory, project_id=project_id,
            )  # fmt: skip

        return self._read(project_id, read)

    def project_root_review(self, project_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """The owner's review of a requested folder (owner channels only: it names the rule a
        folder breaks, including whether it exists)."""
        from prometheos.app import roots

        store = self.open(project_id)

        def read(c: sqlite3.Connection, d: Path) -> dict[str, Any]:
            record = reads.current_record(c, project_id)
            assert record is not None
            return roots.review(
                record["body"], int(record["rev"]), plan, home=self.home, store=store.directory
            )

        return self._read(project_id, read)

    def project_root_add(
        self,
        project_id: str,
        alias: str,
        path: str,
        *,
        authority: object,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Add an approved research folder: an owner act (an agent requests it instead,
        ``approval_request`` kind ``project_root_add``). Audited as a ``project_update``."""
        from prometheos.app import roots

        act = require_owner(authority, "adding a research folder")
        plan = self.project_root_plan(project_id, alias, path)
        return roots.execute_add(
            self.open(project_id), plan, act.actor, home=self.home,
            request_id=request_id or new_request_id(),
        )  # fmt: skip

    # ----- CP11 S11.4: evidence matrices and grounding (decision 0064) ---------------------------

    def matrix_list(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        from prometheos.library import matrix as matrix_views

        return self._read_as(project_id, caller, lambda c, d: matrix_views.listing(c, caller))

    def matrix_show(self, project_id: str, caller: reads.Caller, matrix_id: str) -> dict[str, Any]:
        from prometheos.library import matrix as matrix_views

        return self._read_as(
            project_id, caller, lambda c, d: matrix_views.show(c, caller, matrix_id)
        )

    def _matrix_apply(
        self,
        project_id: str,
        caller: reads.Caller,
        build: Callable[[sqlite3.Connection, Path], tuple[str, list[dict[str, Any]]]],
        request_id: str | None,
        authority: object = None,
    ) -> dict[str, Any]:
        """Submit a matrix change through ``apply``: grounding checks it whoever asks. The
        owner's act (``authority``) grants the exact request it previews; anyone else submits it
        without a capability, so a protected effect (a review) becomes ``approval_required``."""
        from prometheos.library import matrix_ops

        if authority is not None:
            act = require_owner(authority, "reviewing matrix cells")
            caller = reads.Caller(actor=act.actor, scope="owner")

        def built(c: sqlite3.Connection, d: Path) -> tuple[str, bytes]:
            record_id, ops = build(c, d)
            seq = reads.project_seq(c)
            return record_id, matrix_ops.request(project_id, seq, ops, request_id)

        record_id, raw = self._read(project_id, built)
        if authority is None:
            commit = self.apply(project_id, raw, caller)
        else:
            preview = self.review_request(project_id, raw, caller)
            secret = None
            if preview["protected_effects"] and preview["would_apply"]:
                # (a request that does not apply is submitted as is: apply gives its refusal)
                grant = self.grant(
                    project_id, operation="exact_request", digest=preview["digest"],
                    ttl_seconds=60, max_uses=1, label="owner matrix review",
                )  # fmt: skip
                secret = grant.secret
            commit = self.apply(project_id, raw, caller, secret)
        return {"record_id": record_id, "commit": commit}

    def matrix_create(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        title: str,
        columns: list[dict[str, Any]],
        work_ids: list[str],
        question_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """A matrix over works (agent or owner); its rubric starts at version 1."""
        from prometheos.library import matrix_ops

        done = self._matrix_apply(
            project_id, caller,
            lambda c, d: matrix_ops.create_ops(
                c, caller, title=title, columns=columns, work_ids=work_ids,
                question_id=question_id,
            ),
            request_id,
        )  # fmt: skip
        return {"matrix_id": done["record_id"], "commit": done["commit"]}

    def matrix_propose(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        matrix_id: str,
        work_id: str,
        column_id: str,
        value_text: str,
        evidence: list[dict[str, Any]],
        quantities: list[dict[str, Any]] | None = None,
        basis: str | None = None,
        method: str = "manual",
        model: str | None = None,
        relies_on: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Propose one cell (state proposed) quoted from the cited span; grounding refuses an
        invented ID, an out-of-range locator, an altered quotation, a version of another work,
        a basis the work's access state cannot support and a number not in the quote."""
        from prometheos.library import matrix_ops

        done = self._matrix_apply(
            project_id, caller,
            lambda c, d: matrix_ops.propose_ops(
                c, d, caller, matrix_id=matrix_id, work_id=work_id, column_id=column_id,
                value_text=value_text, basis=basis, evidence=evidence,
                quantities_given=quantities or [], method=method, model=model,
                relies_on=relies_on or [],
            ),
            request_id,
        )  # fmt: skip
        return {"extraction_id": done["record_id"], "commit": done["commit"]}

    def matrix_review(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        matrix_id: str,
        work_id: str,
        column_ids: list[str] | None = None,
        authority: object = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """The owner reviews cells (re-reviewing a row clears its needs-review flags). Without
        the owner's act the request is stored for the owner: ``approval_required``."""
        from prometheos.library import matrix_ops

        done = self._matrix_apply(
            project_id, caller,
            lambda c, d: matrix_ops.review_ops(
                c, caller if authority is None else reads.Caller(actor="owner", scope="owner"),
                matrix_id=matrix_id, work_id=work_id, column_ids=column_ids,
            ),
            request_id, authority,
        )  # fmt: skip
        return {"extraction_id": done["record_id"], "commit": done["commit"]}

    def matrix_unsupported(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        matrix_id: str,
        work_id: str,
        column_id: str,
        reason: str,
        basis: str | None = None,
        authority: object = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark a cell unsupported (no value claim). Over a reviewed cell this is the owner's
        act (``approval_required`` otherwise)."""
        from prometheos.library import matrix_ops

        done = self._matrix_apply(
            project_id, caller,
            lambda c, d: matrix_ops.unsupported_ops(
                c, caller if authority is None else reads.Caller(actor="owner", scope="owner"),
                matrix_id=matrix_id, work_id=work_id, column_id=column_id, reason=reason,
                basis=basis,
            ),
            request_id, authority,
        )  # fmt: skip
        return {"extraction_id": done["record_id"], "commit": done["commit"]}

    def matrix_extract(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        matrix_id: str,
        rules: dict[str, Any],
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Run deterministic keyword and pattern rules over the matrix's works (an operation
        run; a transaction per row). Hits are proposed cells; no match is unsupported."""
        from prometheos.library import rules as rule_engine

        parsed = rule_engine.parse(rules)
        params = {"matrix_id": matrix_id, "rules": parsed.model_dump(mode="json")}
        return self._long(
            project_id, caller, "matrix_extract", params, mode=mode, wait_seconds=wait_seconds,
            request_id=request_id,
        )  # fmt: skip

    def matrix_export(
        self,
        project_id: str,
        caller: reads.Caller,
        matrix_id: str,
        fmt: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """CSV, JSON or Markdown. The Markdown export is the generated brief: it is registered
        as a generated version with ``used_in`` relations from the matrix and its rows."""
        from prometheos.library import matrix as matrix_views
        from prometheos.library import matrix_ops

        if fmt != "markdown":
            out = self._read_as(
                project_id, caller, lambda c, d: matrix_views.export(c, caller, matrix_id, fmt)
            )
            out.pop("view")
            return {
                **out, "brief": None, "content_trust": matrix_views.CONTENT_TRUST,
                "notice": matrix_views.NOTICE,
            }  # fmt: skip
        brief = matrix_ops.register_brief(
            self.open(project_id), caller, matrix_id, caller.actor,
            request_id or new_request_id(),
        )  # fmt: skip
        content = brief.pop("content")
        return {
            "project_seq": brief["commit"]["project_seq"],
            "matrix_id": matrix_id,
            "format": "markdown",
            "rows": brief["rows"],
            "content": content,
            "content_sha256": brief["sha256"],
            "brief": brief,
            "content_trust": matrix_views.CONTENT_TRUST,
            "notice": matrix_views.NOTICE,
        }

    def adoption_scan(
        self,
        project_id: str,
        caller: reads.Caller | None = None,
        *,
        root_alias: str,
        exclusions: list[str] | None = None,
        limits: dict[str, Any] | None = None,
        profile: str = "generic",
        rank: bool = True,
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Inventory an approved research root without writing into it (an operation run).
        Agent scope unless an owner caller is given: it only reads an approved root."""
        caller = caller or reads.Caller(actor=self.actor)
        try:
            config = ScanConfig(
                exclusions=list(exclusions or []),
                limits=ArchiveLimits.model_validate(limits or {}),
                profile=profile,
                rank=rank,
            )
        except ValidationError as exc:
            raise PrometheosError(
                "validation_error", "invalid scan configuration", {"errors": exc.error_count()}
            ) from exc
        params = {"root_alias": root_alias, "config": config.model_dump(mode="json")}
        return self._long(
            project_id, caller, "adoption_scan", params, mode=mode,
            wait_seconds=wait_seconds, request_id=request_id,
        )  # fmt: skip

    def adoption_scans(
        self, project_id: str, caller: reads.Caller, root_alias: str | None = None
    ) -> dict[str, Any]:
        return self._read_as(project_id, caller, lambda c, d: adoption_views.scans(c, root_alias))

    def adoption_files(
        self, project_id: str, caller: reads.Caller, scan_gen: int, **filters: Any
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: adoption_views.files(c, scan_gen, **filters)
        )

    def adoption_evidence(
        self, project_id: str, caller: reads.Caller, scan_gen: int, **filters: Any
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: adoption_views.evidence(c, scan_gen, **filters)
        )

    def adoption_families(
        self, project_id: str, caller: reads.Caller, scan_gen: int, **filters: Any
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: adoption_views.families(c, scan_gen, **filters)
        )

    def adoption_family(
        self, project_id: str, caller: reads.Caller, scan_gen: int, family_id: str
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: adoption_views.family(c, scan_gen, family_id)
        )

    def _adoption_root(self, project_id: str, scan_gen: int) -> Path:
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            scan = adoption_tables.require_scan(conn, scan_gen)
            settings, _ = sources._settings(conn, project_id)
        finally:
            conn.close()
        return sources.root_path(settings, scan.root_alias)

    def adoption_assist_tasks(
        self, project_id: str, caller: reads.Caller, scan_gen: int
    ) -> dict[str, Any]:
        return self._read_as(project_id, caller, lambda c, d: adoption_assist.tasks(c, scan_gen))

    def adoption_assist_prepare(
        self, project_id: str, caller: reads.Caller, scan_gen: int, task_kind: str, subject: str
    ) -> dict[str, Any]:
        """The input bundle, closed schema and instructions of one task (a read)."""
        root = self._adoption_root(project_id, scan_gen)
        return self._read_as(
            project_id,
            caller,
            lambda c, d: adoption_assist.prepare(c, root, scan_gen, task_kind, subject),
        )

    def adoption_assist_submit(
        self,
        project_id: str,
        caller: reads.Caller,
        scan_gen: int,
        task_kind: str,
        subject: str,
        sample: int,
        response: Any,
        model: str | None = None,
    ) -> dict[str, Any]:
        """One sample of a task's result: validated by digest and quote; accepted results
        (after agreement) become proposed evidence. Agent scope: it proposes, never decides."""
        root = self._adoption_root(project_id, scan_gen)
        return adoption_assist.submit(
            self.open(project_id), root, scan_gen, task_kind, subject, sample, response, model,
            caller.actor,
        )  # fmt: skip

    def adoption_plan_create(
        self, project_id: str, caller: reads.Caller, scan_gen: int
    ) -> dict[str, Any]:
        """A new consolidation plan version for a complete scan; each step becomes a proposed
        work item (decision 0043). Agent scope: it proposes, the owner adopts items or not."""
        return adoption_plan.create(self.open(project_id), scan_gen, caller.actor, caller.rank)

    def adoption_plan(self, project_id: str, caller: reads.Caller, scan_gen: int) -> dict[str, Any]:
        return self._read_as(project_id, caller, lambda c, d: adoption_plan.view(c, scan_gen))

    def adoption_step_report(
        self, project_id: str, caller: reads.Caller, scan_gen: int, step_id: str, result: Any
    ) -> dict[str, Any]:
        """An agent's result for one plan step, validated against its closed schema and the
        inventory (validated or rejected; the owner accepts results with a baseline)."""
        return adoption_plan.report(self.open(project_id), scan_gen, step_id, result)

    def adoption_decide(
        self,
        project_id: str,
        scan_gen: int,
        family_id: str,
        current: str,
        note: str | None = None,
        *,
        authority: object,
    ) -> dict[str, Any]:
        """The owner chooses a family's current member (T0). An agent requests it instead
        (``approval_request``, kind ``adoption_decide``)."""
        act = require_owner(authority, "choosing a family's current file")
        return adoption_plan.decide(
            self.open(project_id), scan_gen, family_id, current, note, act.actor
        )

    def adoption_classify(
        self,
        project_id: str,
        scan_gen: int,
        family_id: str,
        decision: str,
        note: str | None = None,
        *,
        authority: object,
    ) -> dict[str, Any]:
        """The owner says what a family is when it is not competition: variant, noise or split
        (A06.1, CP20.4). Owner only; it files no work item."""
        act = require_owner(authority, "deciding a family's class")
        return adoption_plan.decide_class(
            self.open(project_id), scan_gen, family_id, decision, note, act.actor
        )

    def adoption_outline(
        self, project_id: str, caller: reads.Caller, scan_gen: int
    ) -> dict[str, Any]:
        """The scan's proposed outline (tracks, deliverables, questions, results with their
        producers, stale candidates), each item citing its file and line (A06.1, CP20.4). A
        read; it records nothing."""
        root = self._adoption_root(project_id, scan_gen)
        return self._read_as(
            project_id, caller, lambda c, d: adoption_outline.propose(c, root, scan_gen)
        )

    def adoption_outline_preview(
        self,
        project_id: str,
        caller: reads.Caller,
        scan_gen: int,
        proposal_sha256: str,
        edits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """What confirming the outline with these edits would record (a read)."""
        root = self._adoption_root(project_id, scan_gen)
        parsed = adoption_outline.Edits.model_validate(edits or {})
        return self._read_as(
            project_id,
            caller,
            lambda c, d: adoption_outline.preview(c, root, scan_gen, proposal_sha256, parsed),
        )

    def adoption_outline_confirm(
        self,
        project_id: str,
        scan_gen: int,
        proposal_sha256: str,
        edits: dict[str, Any] | None = None,
        note: str | None = None,
        *,
        authority: object,
    ) -> dict[str, Any]:
        """The owner confirms the outline once, after correcting it: one transaction records
        the kept tracks, questions and deliverables and their links. An agent requests it
        instead (``approval_request``, kind ``adoption_outline_confirm``)."""
        act = require_owner(authority, "confirming an adoption outline")
        root = self._adoption_root(project_id, scan_gen)
        parsed = adoption_outline.Edits.model_validate(edits or {})
        return adoption_outline.confirm(
            self.open(project_id), root, scan_gen, proposal_sha256, parsed, note, act.actor
        )

    def adoption_cite(
        self,
        project_id: str,
        caller: reads.Caller,
        baseline_id: str,
        path: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """A baseline file as a reference-only version bound to the scan's digest (A06.1,
        CP20.4): nothing is copied or re-read, and it reads as unverified until a source check
        confirms its bytes. An agent may cite, as it may import."""
        return sources.cite_baseline_file(
            self.open(project_id), baseline_id, path, caller.actor, caller.rank, request_id
        )

    def adoption_baseline_accept(
        self, project_id: str, scan_gen: int, note: str | None = None, *, authority: object
    ) -> dict[str, Any]:
        """The owner accepts a generation as the folder's baseline. An agent requests it
        instead (``approval_request``, kind ``adoption_baseline_accept``)."""
        act = require_owner(authority, "accepting an adoption baseline")
        return adoption_plan.accept_baseline(self.open(project_id), scan_gen, note, act.actor, 0)

    def adoption_baseline(
        self, project_id: str, caller: reads.Caller, baseline_id: str
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: adoption_plan.baseline(c, baseline_id)
        )

    # ----- re-adoption, vault import, export (CP17.6; decision 0045) ----------------------

    def adoption_drift(
        self,
        project_id: str,
        caller: reads.Caller,
        scan_gen: int,
        against: str = "previous",
        change: str | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: adoption_drift.view(
                c, scan_gen, against=against, change=change, offset=offset, limit=limit
            ),
        )

    def adoption_vault_import(
        self,
        project_id: str,
        caller: reads.Caller,
        root_alias: str,
        offset: int = 0,
        retention: str | None = None,
        sensitivity: str = "normal",
    ) -> dict[str, Any]:
        """One batch (at most 100) of an approved notes vault's Markdown notes, planned as an
        ordinary source import (commit it with ``import_commit``). Reads only: nothing is
        written into the vault; placeholders and PrometheOS exports are left out."""
        from prometheos.adoption import scan as adoption_scan_mod
        from prometheos.adoption import vault

        root, _rev = adoption_scan_mod.root_of(self._run_env(project_id), root_alias)
        selection = vault.select(root, adoption_scan_mod.STAT_PROVIDER, offset=offset)
        planned = None
        if selection["paths"]:
            planned = self.import_plan(
                project_id, caller, root_alias=root_alias, paths=selection["paths"],
                retention=retention, sensitivity=sensitivity, mode="wait",
            )  # fmt: skip
        return {"root_alias": root_alias, **selection, "import": planned}

    def adoption_baselines(
        self, project_id: str, caller: reads.Caller, root_alias: str | None = None
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: adoption_plan.baselines(c, root_alias)
        )

    def adoption_baseline_export(
        self, project_id: str, caller: reads.Caller, baseline_id: str
    ) -> dict[str, Any]:
        """A baseline as a self-checking package (re-importable into another store)."""
        return self._read_as(
            project_id, caller, lambda c, d: adoption_transfer.export_package(c, baseline_id)
        )

    def adoption_baseline_import(
        self, project_id: str, package: Any, *, authority: object
    ) -> dict[str, Any]:
        """Recreate an exported baseline in this store (owner channel only: the package holds
        owner decisions, which no agent may supply)."""
        act = require_owner(authority, "importing an adoption baseline")
        return adoption_transfer.import_package(self.open(project_id), package, act.actor)

    def adoption_export_markdown_plan(
        self, project_id: str, caller: reads.Caller, baseline_id: str, root_alias: str,
        folder: str,
    ) -> dict[str, Any]:  # fmt: skip
        """What an export would write: the note's path (relative to the root), its new text's
        digest and whether it replaces a block, appends one or creates the note."""
        _target, name, previous, text = self._markdown_export(
            project_id, baseline_id, root_alias, folder
        )
        return {
            "baseline_id": baseline_id,
            "root_alias": root_alias,
            "note": f"{folder.strip('/')}/{name}".lstrip("/"),
            "action": "create"
            if previous is None
            else ("replace" if text != previous else "unchanged"),
            "text_sha256": canonical_sha256({"text": text}),
            "previous_sha256": None if previous is None else canonical_sha256({"text": previous}),
        }

    def adoption_export_markdown(
        self,
        project_id: str,
        baseline_id: str,
        root_alias: str,
        folder: str,
        *,
        authority: object,
        grant: Any = None,
        grant_digest: str | None = None,
    ) -> dict[str, Any]:
        """Write the baseline's fenced block into ``adoption-<root>.md`` in a folder of an
        approved root (an owner act; an agent requests it, kind ``adoption_export_markdown``).
        Text outside the block is kept; the export is one-way."""
        if grant is None:
            require_owner(authority, "writing an adoption note into a folder")
        target, name, previous, text = self._markdown_export(
            project_id, baseline_id, root_alias, folder
        )
        if grant is not None:
            store = self.open(project_id)
            conn = store.connect()
            try:
                with db.write_transaction(conn):
                    capabilities.consume(
                        conn, secret=grant, project_id=project_id, operation="exact_request",
                        digest=grant_digest,
                    )  # fmt: skip
            finally:
                conn.close()
        root = self._root_path(project_id, root_alias)
        digest = adoption_transfer.write_note(target, root, name, text, previous)
        note = f"{folder.strip('/')}/{name}".lstrip("/")
        return {
            "baseline_id": baseline_id,
            "root_alias": root_alias,
            "note": note,
            "text_sha256": digest,
        }

    def _root_path(self, project_id: str, root_alias: str) -> Path:
        from prometheos.adoption import scan as adoption_scan_mod

        root, _rev = adoption_scan_mod.root_of(self._run_env(project_id), root_alias)
        return root

    def _markdown_export(
        self, project_id: str, baseline_id: str, root_alias: str, folder: str
    ) -> tuple[Path, str, str | None, str]:
        from prometheos.adoption.contracts import check_relative

        if folder.strip("/"):
            try:
                check_relative(folder.strip("/"))
            except ValueError as exc:  # a domain error, never a raw exception (cp17-6-a F5)
                raise PrometheosError(
                    "validation_error", "the folder must be relative and stay inside", {}
                ) from exc
        root = self._root_path(project_id, root_alias)
        target = root / folder.strip("/") if folder.strip("/") else root
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            baseline = adoption_plan.baseline(conn, baseline_id)
            block = adoption_transfer.markdown_block(conn, baseline_id)
        finally:
            conn.close()
        adopted = baseline["payload"]["root_alias"]
        # Never into a folder PrometheOS adopts: the next scan would inventory the note and the
        # folder would no longer be the one adopted (review cp17-6-a F2).
        conn = store.connect(readonly=True)
        try:
            scanned = sorted({s.root_alias for s in adoption_tables.list_scans(conn)})
        finally:
            conn.close()
        for alias in scanned:
            try:
                other = self._root_path(project_id, alias).resolve()
            except PrometheosError:
                continue
            place = target.resolve()
            if place == other or place.is_relative_to(other) or other.is_relative_to(place):
                raise PrometheosError(
                    "policy_denied",
                    "the note would be written into a folder PrometheOS adopts; choose another "
                    "approved folder, such as your notes vault",
                    {"root_alias": alias},
                )
        name = adoption_transfer.note_name(adopted)
        previous = adoption_transfer.read_note(target, name)
        return target, name, previous, adoption_transfer.merge(previous, block, adopted)

    def adoption_analyzers(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        """The analyzer declarations (data; the same for every project)."""
        self.open(project_id)
        return adoption_views.analyzer_list()

    def inbox(
        self, project_id: str, caller: reads.Caller, include_dismissed: bool = False
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: jobs.inbox(c, caller, include_dismissed)
        )

    def inbox_dismiss(
        self, project_id: str, caller: reads.Caller, notification_id: int
    ) -> dict[str, Any]:
        return jobs.dismiss(self.open(project_id), caller, notification_id)

    def paper_overview(
        self, project_id: str, caller: reads.Caller, paper_id: str
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: papers.overview(c, d, caller, paper_id)
        )

    def version_audio_range(
        self,
        project_id: str,
        caller: reads.Caller,
        version_id: str,
        first: int | None,
        last: int | None,
    ) -> tuple[bytes, int, str, int, int]:
        return self._read(
            project_id,
            lambda c, d: reads.version_audio_range(c, d, caller, version_id, first, last),
        )

    def version_content(
        self, project_id: str, caller: reads.Caller, version_id: str
    ) -> tuple[bytes, str]:
        """Verified bytes for an inline preview (hub viewers); the caller's policy applies."""
        return self._read(project_id, lambda c, d: reads.version_content(c, d, caller, version_id))

    # ----- CP9: TeX engine, LaTeX workspace, Overleaf Git (A01; decision 0012) -----------------

    def tex_engine_status(self, redact: bool = False) -> dict[str, Any]:
        """``redact`` (hub and MCP): the engine folder as a home-relative label."""
        report = texengine.status(self.home)
        return texengine.redacted(self.home, report) if redact else report

    def tex_engine_plan(self, redact: bool = False) -> dict[str, Any]:
        plan = texengine.install_plan(self.home)
        return texengine.redacted(self.home, plan) if redact else plan

    def tex_engine_install(
        self,
        plan_digest: str,
        archive: Path | None = None,
        redact: bool = False,
        *,
        authority: object,
        project_id: str | None = None,
        caller: reads.Caller | None = None,
        mode: str = "wait",
        wait_seconds: float = 0,
        approval_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Download (or take ``archive``), verify against the pin and install. An owner
        decision (A02, CP10.2): ``authority`` is an :class:`OwnerAct` from the CLI
        confirmation, the hub owner session or a spent approval grant.

        With a project this is an operation run recorded in that project (decision 0017);
        the engine lives in the PrometheOS home, so without one it runs untracked."""
        require_owner(authority, "installing the TeX engine")
        if project_id is None:
            try:
                report = texengine.install(self.home, plan_digest, archive=archive)
            except PrometheosError as exc:
                # A stale digest answers with the current plan: shown with its label, as the
                # plan route shows it (integration review CP5 INT2-M2).
                plan = exc.details.get("plan")
                if redact and isinstance(plan, dict):
                    exc.details["plan"] = texengine.redacted(self.home, plan)
                raise
            return texengine.redacted(self.home, report) if redact else report
        current = texengine.install_plan(self.home)
        if plan_digest != current["plan_digest"]:
            raise PrometheosError(
                "revision_conflict", "the install plan changed; review the new plan",
                {"plan": texengine.redacted(self.home, current) if redact else current},
            )  # fmt: skip
        params = {"plan_digest": plan_digest, "archive": str(archive) if archive else None}
        result = self._long(
            project_id, caller or reads.Caller(actor=self.actor, scope="owner"),
            "tex_engine_install", params, mode=mode, wait_seconds=wait_seconds,
            approval_request_id=approval_request_id,
        )  # fmt: skip
        if mode == "wait" and not redact:
            return texengine.status(self.home) | {"run_id": result["run_id"]}
        return result

    @staticmethod
    def _rank(caller: reads.Caller) -> int:
        return caller.rank

    def workspace_bind(
        self, project_id: str, caller: reads.Caller, paper_id: str, root_alias: str, main: str
    ) -> dict[str, Any]:
        return workspace.bind(
            self.open(project_id),
            caller,
            paper_id,
            root_alias,
            main,
            self.actor,
            self._rank(caller),
        )

    def workspace_files(
        self, project_id: str, caller: reads.Caller, paper_id: str
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: workspace.list_files(c, project_id, caller, paper_id)
        )

    def workspace_read(
        self, project_id: str, caller: reads.Caller, paper_id: str, path: str
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: workspace.read_file(c, project_id, caller, paper_id, path),
        )

    def workspace_write(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        path: str,
        text: str,
        base_sha256: str | None,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: workspace.write_file(
                c, project_id, caller, paper_id, path, text, base_sha256
            ),
        )

    def workspace_snapshot(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        paths: list[str] | None = None,
    ) -> dict[str, Any]:
        return workspace.snapshot(self.open(project_id), caller, paper_id, self.actor, paths)

    def workspace_structure(
        self, project_id: str, caller: reads.Caller, paper_id: str
    ) -> dict[str, Any]:
        def query(conn: sqlite3.Connection, directory: Path) -> dict[str, Any]:
            result = workspace.structure_query(conn, project_id, caller, paper_id)
            result["anchors"] = workspace.anchors(conn, directory, caller, paper_id)
            return result

        return self._read_as(project_id, caller, query)

    def paper_compile(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        *,
        fetch: bool = False,
        request_id: str | None = None,
        limits: texengine.RunLimits | None = None,
        mode: str = "wait",
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        """An operation run (decision 0017): stages snapshot, stage, compile, register; the
        outputs commit under a key derived from the run (``request_id`` makes the start
        idempotent)."""
        texengine.resolve(self.home)  # refuse at once (not as a failed run) without an engine
        return self._long(
            project_id, caller, "paper_compile", {"paper_id": paper_id, "fetch": fetch},
            mode=mode, wait_seconds=wait_seconds, request_id=request_id,
            options={"limits": limits},
        )  # fmt: skip

    def paper_compile_status(
        self, project_id: str, caller: reads.Caller, paper_id: str
    ) -> dict[str, Any]:
        return self._read_as(
            project_id, caller, lambda c, d: texcompile.status(c, d, caller, paper_id)
        )

    def synctex_forward(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        file: str,
        line: int,
        pdf_version: str | None = None,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: texcompile.sync_forward(c, d, caller, paper_id, file, line, pdf_version),
        )

    def synctex_inverse(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        page: int,
        x: float,
        y: float,
        pdf_version: str | None = None,
    ) -> dict[str, Any]:
        return self._read_as(
            project_id,
            caller,
            lambda c, d: texcompile.sync_inverse(c, d, caller, paper_id, page, x, y, pdf_version),
        )

    def workspace_insert(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        kind: str,
        version_id: str,
        bib_file: str | None = None,
    ) -> dict[str, Any]:
        return workspace.insert_evidence(
            self.open(project_id),
            caller,
            paper_id,
            kind,
            version_id,
            self.actor,
            self._rank(caller),
            bib_file,
        )

    # Overleaf Git (CP9.4). Push needs the owner's exact-plan capability; everything else
    # is local or a read of the remote and follows the caller's ordinary scope.

    def overleaf_link(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        url: str,
        root_alias: str,
        folder: str,
        main: str = "main.tex",
    ) -> dict[str, Any]:
        return gitsync.link(
            self.open(project_id),
            self.home,
            caller,
            paper_id,
            url,
            root_alias,
            folder,
            main,
            self.actor,
            self._rank(caller),
        )

    def overleaf_status(
        self, project_id: str, caller: reads.Caller, paper_id: str
    ) -> dict[str, Any]:
        return gitsync.status(self.open(project_id), self.home, caller, paper_id)

    def overleaf_pull_preview(
        self, project_id: str, caller: reads.Caller, paper_id: str, fetch: bool = True
    ) -> dict[str, Any]:
        return gitsync.pull_preview(self.open(project_id), self.home, caller, paper_id, fetch)

    def overleaf_pull_apply(
        self, project_id: str, caller: reads.Caller, paper_id: str, digest: str
    ) -> dict[str, Any]:
        return gitsync.pull_apply(
            self.open(project_id), self.home, caller, paper_id, digest, self.actor
        )

    def overleaf_commit(
        self, project_id: str, caller: reads.Caller, paper_id: str, message: str
    ) -> dict[str, Any]:
        return gitsync.commit(self.open(project_id), self.home, caller, paper_id, message)

    def overleaf_push_preview(
        self, project_id: str, caller: reads.Caller, paper_id: str
    ) -> dict[str, Any]:
        return gitsync.push_preview(self.open(project_id), self.home, caller, paper_id)

    def overleaf_push_grant(self, project_id: str, digest: str, label: str) -> Any:
        """Owner channels only: the interactive CLI confirmation and the paired hub."""
        return gitsync.grant_push(self.open(project_id), digest, label)

    def overleaf_push(
        self,
        project_id: str,
        caller: reads.Caller,
        paper_id: str,
        digest: str,
        capability: CapabilitySecret,
    ) -> dict[str, Any]:
        return gitsync.push_apply(
            self.open(project_id), self.home, caller, paper_id, digest, capability, self.actor
        )

    # ----- CP6: audio, transcription and recording ----------------------------------------

    def _owner_label(self) -> str:
        return f"{self.actor}:{os.getpid()}"

    def audio_import(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        root_alias: str,
        path: str,
        sensitivity: str = importing.DEFAULT_AUDIO_SENSITIVITY,
        title: str | None = None,
        retention: str = "copy",
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not path.lower().endswith(importing.AUDIO_SUFFIXES):
            raise PrometheosError(
                "validation_error",
                "not a recognised audio file name",
                {"accepted": list(importing.AUDIO_SUFFIXES)},
            )
        params = {
            "root_alias": root_alias,
            "path": path,
            "sensitivity": sensitivity,
            "title": title,
            "retention": retention,
        }
        return self._long(
            project_id, caller, "audio_import", params, mode=mode, wait_seconds=wait_seconds,
            request_id=request_id,
        )  # fmt: skip

    def transcribe_start(
        self,
        project_id: str,
        caller: reads.Caller,
        version_id: str,
        config: transcribe.RunConfig,
        *,
        budget_seconds: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create (or resume the unfinished) transcription run; runs nothing."""
        return transcribe.start(
            self.open(project_id), self.home, caller, version_id, config,
            budget_seconds=budget_seconds, request_id=request_id,
        )  # fmt: skip

    def transcribe_background(self, project_id: str, run: dict[str, Any]) -> dict[str, Any]:
        """Execute a started (or re-queued) transcription in a thread of this long-lived
        process (hub, MCP server); its run row carries what happens."""
        if run["state"] == "queued":
            runs.execute_in_background(self._run_env(project_id), run["run_id"], actor=self.actor)
        return run

    def transcribe_run(
        self,
        project_id: str,
        caller: reads.Caller,
        run_id: str,
        *,
        progress: Callable[[int, int], None] | None = None,
        engine: Any = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Execute or resume a run in this process until it finishes, fails or is cancelled.
        A run over audio the caller may not see reads as absent, and the response is filtered
        to IDs the caller may see (a run's commit rank is fixed when it starts; see 0013)."""
        self.transcription_status(project_id, caller, run_id)
        result = transcribe.execute(
            self.open(project_id),
            self.home,
            run_id,
            owner=owner or runs.owner_label(self.actor),
            actor=self.actor,
            progress=progress,
            engine=engine,
            service=self,
        )
        return self._read_as(project_id, caller, lambda c, d: result)

    def transcribe_audio(
        self,
        project_id: str,
        caller: reads.Caller,
        version_id: str,
        config: transcribe.RunConfig,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        run = self.transcribe_start(project_id, caller, version_id, config)
        return self.transcribe_run(project_id, caller, run["run_id"], progress=progress)

    def transcription_status(
        self, project_id: str, caller: reads.Caller, run_id: str
    ) -> dict[str, Any]:
        return self._read(project_id, lambda c, d: transcribe.run_status(c, d, caller, run_id))

    def transcription_list(
        self, project_id: str, caller: reads.Caller, version_id: str | None = None
    ) -> dict[str, Any]:
        return self._read(project_id, lambda c, d: transcribe.list_runs(c, d, caller, version_id))

    def transcription_cancel(
        self, project_id: str, caller: reads.Caller, run_id: str
    ) -> dict[str, Any]:
        return transcribe.cancel(self.open(project_id), caller, run_id)

    def transcript_get(
        self, project_id: str, caller: reads.Caller, version_id: str
    ) -> dict[str, Any]:
        return self._read(
            project_id, lambda c, d: transcribe.transcript_view(c, d, caller, version_id)
        )

    def transcript_correct(
        self,
        project_id: str,
        caller: reads.Caller,
        version_id: str,
        edits: list[transcribe.Edit],
        request_id: str,
    ) -> dict[str, Any]:
        return transcribe.correct(
            self.open(project_id),
            caller,
            version_id,
            edits,
            actor=self.actor,
            request_id=request_id,
        )

    def audio_span(
        self,
        project_id: str,
        caller: reads.Caller,
        version_id: str,
        start_ms: int,
        end_ms: int,
        out: Path | None = None,
        audio_version_id: str | None = None,
    ) -> dict[str, Any]:
        """Transcript text for a locator, and optionally the matching audio clip (WAV),
        so a person can listen to the exact span (sample-span verification, E05)."""

        def read(conn: sqlite3.Connection, store_dir: Path) -> dict[str, Any]:
            report = transcribe.span_report(conn, store_dir, caller, version_id, start_ms, end_ms)
            if out is not None:
                view = transcribe.transcript_view(conn, store_dir, caller, version_id)
                source = audio_version_id or view["audio_version_id"]
                if source is None:
                    raise PrometheosError(
                        "source_unavailable", "the recording is not available to this caller", {}
                    )
                record = transcribe.audio_version(conn, store_dir, caller, source)
                # The read connection holds the project gate for the whole block: no purge can
                # commit while the clip is cut (decision 0031).
                with transcribe.materialized(conn, store_dir, record["body"]) as path:
                    if out.exists():
                        raise PrometheosError("validation_error", "the output file exists", {})
                    info = wav.extract_clip(path, start_ms, end_ms, out)
                report["clip"] = {
                    "file": str(out),
                    "duration_ms": info.duration_ms,
                    "audio_version_id": source,
                }
            return report

        return self._read(project_id, read)

    def audio_models_plan(
        self, key: str = models.DEFAULT_MODEL, redact_paths: bool = False
    ) -> dict[str, Any]:
        """``redact_paths``: for agent transports, show ``<home>/models/...`` instead of the
        absolute path (which carries the user's name)."""
        plan = models.plan(self.home, key)
        return models.redact_location(self.home, plan) if redact_paths else plan

    def audio_models_install(
        self,
        key: str,
        plan_sha256: str,
        progress: Callable[[str, int, int], None] | None = None,
        *,
        authority: object,
        project_id: str | None = None,
        caller: reads.Caller | None = None,
        mode: str = "wait",
        wait_seconds: float = 0,
        approval_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Owner decision (A02, CP10.2): ``authority`` is an :class:`OwnerAct`. With a project
        this is an operation run recorded there (decision 0017); without one, untracked."""
        require_owner(authority, "downloading a speech model")
        if project_id is None:
            return models.install(self.home, key, plan_sha256, progress=progress)
        current = models.plan(self.home, key)
        if plan_sha256 != current["plan_sha256"]:
            raise PrometheosError(
                "revision_conflict",
                "the plan changed or the digest is wrong; review the plan again",
                {"plan_sha256": current["plan_sha256"]},
            )
        result = self._long(
            project_id, caller or reads.Caller(actor=self.actor, scope="owner"),
            "audio_models_install", {"model": key, "plan_sha256": plan_sha256}, mode=mode,
            wait_seconds=wait_seconds, options={"progress": progress},
            approval_request_id=approval_request_id,
        )  # fmt: skip
        if mode == "wait" and "location" in result:
            # The run stores the redacted location (agents read runs); the owner's own caller
            # gets the real one, as before.
            target = models.target_dir(self.home, models.spec_for(key))
            return result | {"location": str(target)}
        return result

    def audio_models_status(
        self, verify: bool = False, redact_paths: bool = False
    ) -> dict[str, Any]:
        status = models.status(self.home, verify)
        if redact_paths:
            status = {"models": [models.redact_location(self.home, m) for m in status["models"]]}
        return status

    def audio_models_remove(self, key: str, *, authority: object) -> dict[str, Any]:
        """Owner decision (A02, CP10.2): ``authority`` is an :class:`OwnerAct`."""
        require_owner(authority, "deleting a speech model")
        return models.remove(self.home, key)

    def capture_devices(self, backend: capture.Backend | None = None) -> dict[str, Any]:
        chosen = backend or capture.get_backend()
        return {"backend": chosen.name, "devices": [d.as_json() for d in chosen.devices()]}

    def recording_start(
        self,
        project_id: str,
        caller: reads.Caller,
        request: recorder.SessionRequest,
        backend: capture.Backend | None = None,
        *,
        authority: object,
    ) -> dict[str, Any]:
        """Create a session (consent must be acknowledged); ``recording_run`` captures. The
        caller's rank binds the commit that registers the recording. Consent is an owner act
        at the computer (A02, CP10.2; decision 0015 R15): ``authority`` is an
        :class:`OwnerAct`; an agent requests a recording and the owner grants it."""
        require_owner(authority, "starting a recording")
        return recorder.create_session(
            self.open(project_id),
            backend or capture.get_backend(),
            request,
            self._owner_label(),
            caller,
        )

    def recording_run(
        self,
        project_id: str,
        session_id: str,
        backend: capture.Backend | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return recorder.run_session(
            self.open(project_id),
            backend or capture.get_backend(),
            session_id,
            owner=self._owner_label(),
            actor=self.actor,
            **kwargs,
        )

    def recording_status(
        self, project_id: str, caller: reads.Caller, session_id: str
    ) -> dict[str, Any]:
        return self._read(project_id, lambda c, d: recorder.status(c, session_id, caller))

    def recording_list(
        self, project_id: str, caller: reads.Caller, include_done: bool = False
    ) -> dict[str, Any]:
        return self._read(project_id, lambda c, d: recorder.list_sessions(c, include_done, caller))

    def recording_control(
        self, project_id: str, caller: reads.Caller, session_id: str, action: str
    ) -> dict[str, Any]:
        if action not in ("pause", "resume", "stop", "cancel"):
            raise PrometheosError("validation_error", "unknown recording control", {})
        return recorder.request_flag(self.open(project_id), session_id, action, caller)

    def recording_recover(
        self, project_id: str, caller: reads.Caller, session_id: str
    ) -> dict[str, Any]:
        return recorder.recover(self.open(project_id), session_id, actor=self.actor, caller=caller)

    def recording_discard(
        self, project_id: str, caller: reads.Caller, session_id: str, confirm: bool = False
    ) -> dict[str, Any]:
        return recorder.discard(self.open(project_id), session_id, caller, confirm=confirm)

    def capture_meeting_from_transcript(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        transcript_version_id: str,
        audio_version_id: str | None,
        title: str,
        held_at: str | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return meetings.capture_from_transcript(
            self.open(project_id),
            caller=caller,
            transcript_version_id=transcript_version_id,
            audio_version_id=audio_version_id,
            title=title,
            held_at=held_at,
            actor=self.actor,
            request_id=request_id,
        )

    # ----- CP6.3: providers (capability interface, routing policy, egress, budgets) ----------

    def _secrets(self) -> provider_secrets.SecretStore:
        if self.secret_store is not None:
            return self.secret_store
        from prometheos.intelligence import replay

        if replay.from_environment() is not None:
            # C22-WALK's fake key: the environment only, never the OS credential store (D2).
            return provider_secrets.EnvSecretStore()
        return provider_secrets.default_store()

    def _transport(self) -> provider_transport.Transport:
        if self.provider_transport is not None:
            return self.provider_transport
        from prometheos.intelligence import replay

        # C22-WALK only (decision 0098): recorded replies, and only with the explicit test switch.
        return replay.from_environment() or provider_transport.UrllibTransport()

    def capabilities(self, project_id: str | None) -> dict[str, Any]:
        """Pure capability discovery (A02, CP10.1): per feature, version, availability,
        required authority, local/remote, cost class and blocker. Agent view; no secrets,
        paths or restricted content. ``project_id`` may be ``None`` (CLI without a project):
        project-dependent features then report ``not_configured``."""
        from prometheos.app import discovery

        status: dict[str, Any] | PrometheosError | None = None
        if project_id is not None:
            self.open(project_id)  # an unknown project is still the caller's error
            try:
                status = self.provider_status(project_id, reads.Caller(actor=self.actor))
            except PrometheosError as exc:
                status = exc  # degrades the provider feature only
        return discovery.report(self.home, project_id, status)

    def provider_status(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        """Policy, route, capability table, credential reference and health, budget usage.
        Never a credential value; nothing is sent."""

        def read(conn: sqlite3.Connection, _dir: Path) -> dict[str, Any]:
            config, revision = provider_config.load(conn)
            return {
                "revision": revision,
                "config": config.model_dump(mode="json"),
                "policy": config.policy,
                "route": config.route,
                "endpoint_host": provider_config.host_of(config.endpoint or ""),
                "capabilities": provider_adapter.capability_table(config),
                "credential": provider_secrets.health(self._secrets(), config.credential_ref),
                "usage": provider_ledger.usage_summary(conn, config, caller.scope == "owner"),
                "digest": provider_config.digest(config),
                "silent_remote_fallback": False,
            }

        return self._read(project_id, read)

    def provider_configure_preview(
        self, project_id: str, document: Any, base_revision: int
    ) -> dict[str, Any]:
        """Pure: the validated configuration, what changes and the digest an approval binds."""
        proposed = provider_config.parse(document)

        def read(conn: sqlite3.Connection, _dir: Path) -> dict[str, Any]:
            current, revision = provider_config.load(conn)
            old, new = current.model_dump(mode="json"), proposed.model_dump(mode="json")
            changes = sorted(k for k in new if new[k] != old[k])
            return {
                "config": new,
                "route": proposed.route,
                "policy": proposed.policy,
                "changes": changes,
                "current_revision": revision,
                "stale": revision != base_revision,
                "digest": canonical_sha256(
                    {
                        "op": "provider_configure",
                        "revision": base_revision,
                        "config": provider_config.digest(proposed),
                    }
                ),
                "credential": provider_secrets.health(self._secrets(), proposed.credential_ref),
            }

        return self._read(project_id, read)

    def provider_configure(
        self,
        project_id: str,
        document: Any,
        base_revision: int,
        capability: CapabilitySecret,
    ) -> dict[str, Any]:
        """Owner decision: store the routing policy and settings (capability bound to the
        exact configuration and revision)."""
        shown = self.provider_configure_preview(project_id, document, base_revision)
        if shown["stale"]:
            raise PrometheosError(
                "revision_conflict",
                "the provider configuration changed; review it again",
                {"revision": shown["current_revision"]},
            )
        store = self.open(project_id)

        def authorize(conn: sqlite3.Connection) -> None:
            capabilities.consume(
                conn,
                secret=capability,
                project_id=project_id,
                operation="provider_configure",
                digest=shown["digest"],
            )

        revision = provider_config.save(
            store, provider_config.parse(document), base_revision, authorize
        )
        return {"revision": revision, "route": shown["route"], "policy": shown["policy"]}

    # ----- the intelligence layer (A07, CP22.4; decision 0094) ---------------------------------

    def ai_policy_show(self, project_id: str) -> dict[str, Any]:
        return app_intelligence.policy_show(self, project_id)

    def ai_policy_base(self, project_id: str) -> dict[str, Any]:
        return app_intelligence.policy_base(self, project_id)

    def ai_policy_preview(self, project_id: str, document: Any) -> dict[str, Any]:
        return app_intelligence.policy_preview(self, project_id, document)

    def ai_policy_grant(
        self, project_id: str, document: Any, binding_sha256: str, authority: object = None
    ) -> dict[str, Any]:
        """Owner decision (an owner act only); an agent's request is an approval request."""
        return app_intelligence.policy_grant(self, project_id, document, binding_sha256, authority)

    def ai_policy_revoke(self, project_id: str, authority: object = None) -> dict[str, Any]:
        """The owner's alone: without an owner act it answers ``policy_denied``."""
        return app_intelligence.policy_revoke(self, project_id, authority)

    def ai_root_sensitivity_show(self, project_id: str) -> dict[str, Any]:
        return app_intelligence.root_sensitivity_show(self, project_id)

    def ai_root_sensitivity_set(
        self, project_id: str, alias: str, sensitivity: str, authority: object = None
    ) -> dict[str, Any]:
        """Raising is open to any caller; lowering needs an owner act (A07.4)."""
        return app_intelligence.root_sensitivity_set(
            self, project_id, alias, sensitivity, authority
        )

    def ai_run(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        task_kind: str,
        scan_gen: int,
        paths: list[str],
        dry_run: bool = False,
        cassettes: Path | None = None,
        per_call: dict[str, Any] | None = None,
        authority: object = None,
    ) -> dict[str, Any]:
        """A task run over adoption files inside the standing policy (A07.4); outside it, an
        approval request with the per-call preview, and once granted, that one run."""
        return app_intelligence.run_task(
            self, project_id, task_kind=task_kind, scan_gen=scan_gen, paths=paths,
            agent=caller.scope != "owner", dry_run=dry_run, cassettes=cassettes,
            caller=caller, per_call=per_call, authority=authority,
        )  # fmt: skip

    def ai_map_suggest(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        task_kind: str = "map.link_claim_question",
        claim_ids: list[str] | None = None,
        dry_run: bool = False,
        cassettes: Path | None = None,
    ) -> dict[str, Any]:
        """A map task over the claims it applies to (decision 0109), inside the standing policy;
        its proposals show beside each claim and the owner decides each with one act."""
        return app_intelligence.run_map_task(
            self, project_id, task_kind=task_kind, claim_ids=claim_ids,
            agent=caller.scope != "owner", dry_run=dry_run, cassettes=cassettes, caller=caller,
        )  # fmt: skip

    def ai_run_per_call_preview(
        self, project_id: str, task_kind: str, scan_gen: int, paths: list[str]
    ) -> dict[str, Any]:
        return app_intelligence.per_call_preview(
            self, project_id, task_kind=task_kind, scan_gen=scan_gen, paths=paths
        )

    def ai_accept_preview(
        self, project_id: str, task_kind: str, scan_gen: int, subject: str
    ) -> dict[str, Any]:
        return app_intelligence.accept_preview(
            self, project_id, task_kind=task_kind, scan_gen=scan_gen, subject=subject
        )

    def ai_accept_keys(
        self, project_id: str, task_kind: str, scan_gen: int, subject: str,
        result_keys: list[str] | None = None,
    ) -> list[str]:  # fmt: skip
        return app_intelligence.accept_keys(
            self, project_id, task_kind=task_kind, scan_gen=scan_gen, subject=subject,
            result_keys=result_keys,
        )  # fmt: skip

    def ai_accept(
        self, project_id: str, task_kind: str, scan_gen: int, subject: str,
        authority: object = None, result_keys: list[str] | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Owner decision: a layer proposal becomes proposed evidence (one owner action)."""
        return app_intelligence.accept(
            self, project_id, task_kind=task_kind, scan_gen=scan_gen, subject=subject,
            authority=authority, result_keys=result_keys,
        )  # fmt: skip

    def ai_results(
        self, project_id: str, task_kind: str | None = None, limit: int = 200,
        scan_gen: int | None = None, current: bool = False,
    ) -> dict[str, Any]:  # fmt: skip
        return app_intelligence.results_list(self, project_id, task_kind, limit, scan_gen, current)

    def ai_evaluations(self, project_id: str) -> dict[str, Any]:
        return app_intelligence.evaluations(self, project_id)

    def provider_secret_set(
        self, project_id: str, reference: str, value: str, capability: str | None
    ) -> dict[str, Any]:
        """Owner decision: store a credential in the OS credential store. The capability binds
        the reference, never the value; the value is not returned or recorded anywhere."""
        provider_secrets.check_reference(reference)
        provider_secrets.check_value(value)
        self._spend_secret_capability(project_id, "set", reference, capability)
        self._secrets().set(reference, value)
        return {"credential": provider_secrets.health(self._secrets(), reference)}

    def provider_secret_delete(
        self, project_id: str, reference: str, capability: str | None
    ) -> dict[str, Any]:
        provider_secrets.check_reference(reference)
        self._spend_secret_capability(project_id, "delete", reference, capability)
        removed = self._secrets().delete(reference)
        return {
            "removed": removed,
            "credential": provider_secrets.health(self._secrets(), reference),
        }

    @staticmethod
    def provider_secret_digest(action: str, reference: str) -> str:
        return canonical_sha256({"op": "provider_secret", "action": action, "reference": reference})

    def _spend_secret_capability(
        self, project_id: str, action: str, reference: str, capability: str | None
    ) -> None:
        conn = self.open(project_id).connect()
        try:
            with db.write_transaction(conn):
                capabilities.consume(
                    conn,
                    secret=capability,
                    project_id=project_id,
                    operation="provider_configure",
                    digest=self.provider_secret_digest(action, reference),
                )
        finally:
            conn.close()

    def provider_preview(
        self,
        project_id: str,
        caller: reads.Caller,
        *,
        purpose: str,
        meeting_id: str | None = None,
        context_budget: int = 12000,
        include_restricted: bool = False,
    ) -> dict[str, Any]:
        """Pure egress preview: what would leave this computer, by which route, at what
        estimated cost, and why it cannot go ahead. Restricted inputs need owner scope."""
        if include_restricted and caller.scope != "owner":
            raise PrometheosError(
                "policy_denied", "previewing restricted inputs needs owner scope", {}
            )
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            config, _revision = provider_config.load(conn)
        finally:
            conn.close()
        return provider_generate.preview(
            store,
            config,
            self._secrets(),
            purpose=purpose,
            meeting_id=meeting_id,
            context_budget=context_budget,
            include_restricted=include_restricted,
            transport=self._transport(),
            reveal_hidden_count=caller.scope == "owner",
        )

    def provider_run(
        self,
        project_id: str,
        *,
        purpose: str,
        meeting_id: str | None,
        context_budget: int,
        include_restricted: bool,
        approved_digest: str,
        capability: CapabilitySecret,
        egress_capability: CapabilitySecret = None,
        acknowledge_unknown: str | None = None,
        caller: reads.Caller | None = None,
        mode: str = "wait",
        wait_seconds: float = 0,
        request_id: str | None = None,
        approval_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Owner decision: one bounded generation. Needs a capability bound to the reviewed
        digest (and a second one for restricted inputs), spent once when the operation run is
        created (decision 0017). Failure never blocks anything else."""
        store = self.open(project_id)
        conn = store.connect(readonly=True)
        try:
            config, _revision = provider_config.load(conn)
        finally:
            conn.close()
        checked = provider_generate.check_run(
            store,
            config,
            self._secrets(),
            self._transport(),
            purpose=purpose,
            meeting_id=meeting_id,
            context_budget=context_budget,
            include_restricted=include_restricted,
            approved_digest=approved_digest,
            capability_secret=capability,
            egress_secret=egress_capability,
            acknowledge_unknown=acknowledge_unknown,
        )
        params = {
            "purpose": purpose,
            "meeting_id": meeting_id,
            "context_budget": context_budget,
            "include_restricted": include_restricted,
            "approved_digest": approved_digest,
            "acknowledge_unknown": acknowledge_unknown,
        }
        fingerprint = runs.Fingerprint(
            inputs=(("payload", checked["shown"]["transmits"]["payload_sha256"]),),
            config=provider_generate.run_configuration(config),
            model=config.model,
        )
        return self._long(
            project_id,
            caller or reads.Caller(actor=self.actor, scope="owner"),
            "provider_run",
            params,
            mode=mode,
            wait_seconds=wait_seconds,
            request_id=request_id,
            fingerprint=fingerprint,
            authorize=lambda c: provider_generate.spend(c, store, checked),
            approval_request_id=approval_request_id,
        )

    def provider_calls(self, project_id: str, caller: reads.Caller) -> dict[str, Any]:
        """The call ledger: digests, routes, states and costs; never prompt or reply text."""

        def read(conn: sqlite3.Connection, _dir: Path) -> dict[str, Any]:
            config, _rev = provider_config.load(conn)
            return {
                "calls": provider_ledger.listing(conn, caller.rank),
                "usage": provider_ledger.usage_summary(conn, config, caller.scope == "owner"),
            }

        return self._read(project_id, read)
