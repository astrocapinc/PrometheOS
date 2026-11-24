"""Run handlers of the long operations (A02, CP10.3; decision 0017).

Each handler names its operation's stages and what resume compares. Research-visible results
commit through the transaction service under request keys derived from the run ID, so a
resumed or replayed stage never duplicates them:

| Operation | Stages (cancellation is checked between them) | Inputs compared at resume |
|---|---|---|
| source_import_plan | hash (throttled checkpoints), plan | project settings revision |
| source_import_commit | verify each file, commit | current bytes of every planned file |
| audio_import | plan, planned, verify, commit | project settings revision |
| paper_compile | snapshot, stage, compile, register | current compile input files |
| tex_engine_install | install (download, verify, place) | the pinned install plan |
| audio_models_install | install (per-file download checkpoints) | the reviewed model plan |
| audio_transcribe | decode, chunk:N..., merge | audio bytes, engine version |
| provider_run | task, call:N, submit:N (``providers.generate``) | exact request payload, config |
| library_import_plan | read (throttled checkpoints), plan | project settings revision |
| library_import_commit | file N of M (a commit per file), commit | current bytes of planned files |
| zotero_import_plan | read (throttled checkpoints), plan | the scope |
| zotero_import_commit | item N of M (a commit per item), commit | plan and item digests |
| matrix_extract | match, commit (a transaction per row) | the rule set, the matrix revision |
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prometheos.adoption import scan as _adoption_scan  # noqa: F401 - registers its handler
from prometheos.app import reads, runs, sources, texcompile, texengine, workspace
from prometheos.audio import importing
from prometheos.audio import transcribe as _transcribe  # noqa: F401 - registers its handler
from prometheos.domain.ids import derived_request_id
from prometheos.domain.models import PrometheosError
from prometheos.library import importer as _library  # noqa: F401 - registers its handlers
from prometheos.library import matrix_ops as _matrix  # noqa: F401 - registers its handler
from prometheos.library import zotero as _zotero  # noqa: F401 - registers its handlers
from prometheos.sources.paths import open_verified, relative_to_root, resolve_in_root

CHECKPOINT_SECONDS = 2.0


def _uuid(run_id: str) -> str:
    return run_id.removeprefix("oprun_")


def _request(ctx: runs.RunContext, label: str) -> str:
    return derived_request_id(label, ctx.store.project_id, ctx.run.id)


def throttled(
    ctx: runs.RunContext, label: str, seconds: float = CHECKPOINT_SECONDS
) -> Callable[[], None]:
    """A progress callback that checkpoints (cancellation, lease) at most every ``seconds``."""
    last = [time.monotonic()]

    def tick() -> None:
        now = time.monotonic()
        if now - last[0] >= seconds:
            last[0] = now
            ctx.checkpoint(label)

    return tick


def _commit_artifacts(ctx: runs.RunContext, commit: dict[str, Any], role: str) -> None:
    for item in commit.get("results", []):
        if item.get("id"):
            ctx.artifact(str(item["id"]), int(item["rev"]), role)
    if commit.get("project_seq") is not None:
        ctx.result_seq = int(commit["project_seq"])


# ----- sources -------------------------------------------------------------------------------


def _check_paths(env: runs.Env, alias: str, paths: list[str]) -> int:
    """Refuse, before any run exists, what the plan would refuse for its paths (outside the
    root, version-control metadata, short aliases): the same answer on every transport,
    at once. Returns the settings revision the run is bound to."""
    import os

    if not paths or len(paths) > sources.MAX_PLAN_FILES:
        raise PrometheosError(
            "budget_exceeded",
            f"select between 1 and {sources.MAX_PLAN_FILES} files",
            {"count": len(paths)},
        )
    conn = env.store.connect(readonly=True)
    try:
        settings, rev = sources._settings(conn, env.project_id)
    finally:
        conn.close()
    root = sources.root_path(settings, alias)
    for given in paths:
        rel = relative_to_root(root, Path(given)) if os.path.isabs(given) else given
        resolve_in_root(root, rel)
    return rev


class ImportPlanHandler(runs.Handler):
    operation = "source_import_plan"
    version = "import-plan-v1"

    def fingerprint(
        self,
        env: runs.Env,
        params: dict[str, Any],
        caller: reads.Caller,
        run: Any = None,
    ) -> runs.Fingerprint:
        rev = _check_paths(env, str(params["root_alias"]), list(params["paths"]))
        return runs.Fingerprint(inputs=(), config={"settings_rev": rev})

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        p = ctx.params
        ctx.checkpoint("hash")
        plan = sources.create_plan(
            ctx.store,
            root_alias=p["root_alias"],
            paths=list(p["paths"]),
            retention=p["retention"],
            sensitivity=p["sensitivity"],
            titles=p.get("titles"),
            description=p.get("description", ""),
            max_rank=ctx.run.caller_rank,
            plan_id=_uuid(ctx.run.id),
            request_id=_request(ctx, "import-plan-commit"),
            progress=throttled(ctx, "hash"),
        )
        ctx.complete("plan", {"plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"]})
        return plan


class ImportCommitHandler(runs.Handler):
    operation = "source_import_commit"
    version = "import-commit-v1"

    @staticmethod
    def planned(env: runs.Env, plan_id: str, rank: int) -> dict[str, Any]:
        plan = sources.stored_plan(env.store, plan_id, rank)
        if plan is None:
            raise PrometheosError("not_found", "import plan does not exist", {"plan_id": plan_id})
        return plan

    def from_plan(self, plan: dict[str, Any]) -> runs.Fingerprint:
        inputs = [("plan", str(plan["plan_sha256"]))]
        inputs += [
            (str(i["relative_path"])[:200], str(i["sha256"]))
            for i in plan["items"]
            if i["action"] != "unchanged"
        ]
        return runs.Fingerprint(inputs=tuple(inputs), config={"plan_sha256": plan["plan_sha256"]})

    def fingerprint(
        self,
        env: runs.Env,
        params: dict[str, Any],
        caller: reads.Caller,
        run: Any = None,
    ) -> runs.Fingerprint:
        """The bytes as they are *now*: a planned file that changed since the plan gives other
        digests, so a resume is refused and a new plan (and run) is needed."""
        plan = self.planned(env, str(params["plan_id"]), 2)
        conn = env.store.connect(readonly=True)
        try:
            settings, _ = sources._settings(conn, env.project_id)
        finally:
            conn.close()
        root = sources.root_path(settings, plan["root_alias"])
        inputs = [("plan", str(plan["plan_sha256"]))]
        for item in plan["items"]:
            if item["action"] == "unchanged":
                continue
            try:
                resolved = resolve_in_root(root, item["relative_path"])
                with open_verified(resolved) as handle:
                    sha, _size, _data = sources.hash_file(
                        handle, settings.retention.max_media_bytes
                    )
            except PrometheosError:
                sha = "0" * 64  # gone or unreadable: never equal to the planned bytes
            inputs.append((str(item["relative_path"])[:200], sha))
        return runs.Fingerprint(inputs=tuple(inputs), config={"plan_sha256": plan["plan_sha256"]})

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        commit = sources.commit_plan(
            ctx.store,
            str(ctx.params["plan_id"]),
            ctx.run.actor,
            max_rank=ctx.run.caller_rank,
            checkpoint=ctx.checkpoint,
        )
        _commit_artifacts(ctx, commit, "imported")
        ctx.complete("commit", {"project_seq": commit["project_seq"]})
        return commit


# ----- audio import ---------------------------------------------------------------------------


class AudioImportHandler(runs.Handler):
    operation = "audio_import"
    version = "audio-import-v1"

    def fingerprint(
        self,
        env: runs.Env,
        params: dict[str, Any],
        caller: reads.Caller,
        run: Any = None,
    ) -> runs.Fingerprint:
        rev = _check_paths(env, str(params["root_alias"]), [str(params["path"])])
        return runs.Fingerprint(inputs=(), config={"settings_rev": rev})

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        p = ctx.params
        ctx.checkpoint("plan")
        result = importing.import_file(
            ctx.store,
            caller=ctx.caller,
            root_alias=p["root_alias"],
            path=p["path"],
            sensitivity=p["sensitivity"],
            title=p.get("title"),
            retention=p["retention"],
            actor=ctx.run.actor,
            plan_id=_uuid(ctx.run.id),
            request_id=_request(ctx, "audio-import-commit"),
            checkpoint=ctx.checkpoint,
        )
        _commit_artifacts(ctx, result["commit"], "imported")
        ctx.complete("commit", {"version_id": result["version_id"]})
        return result


# ----- LaTeX compile and engine ---------------------------------------------------------------


class CompileHandler(runs.Handler):
    operation = "paper_compile"
    version = "compile-v1"

    def fingerprint(
        self,
        env: runs.Env,
        params: dict[str, Any],
        caller: reads.Caller,
        run: Any = None,
    ) -> runs.Fingerprint:
        # At the visibility of the rank that starts the run (round 2, F1): an agent's run never
        # names, hashes or counts a workspace file registered above its scope, exactly like
        # the agent's own listing (review cp9-a CP9-A-3). Such a compile is refused anyway.
        conn = env.store.connect(readonly=True)
        try:
            listing = workspace.list_files(conn, env.project_id, caller, str(params["paper_id"]))
        finally:
            conn.close()
        inputs = tuple(
            (str(f["path"])[:200], str(f["sha256"] or "0" * 64))
            for f in listing["files"]
            if f["compile_input"]
        )[:200]
        status = texengine.status(env.home)
        installed = status.get("installed") or {}
        engine = {
            "state": status.get("state"),
            "version": installed.get("version"),
            "exe_sha256": installed.get("exe_sha256"),
        }
        return runs.Fingerprint(
            inputs=inputs,
            config={"engine": engine, "fetch": bool(params["fetch"])},
            model=f"tectonic {engine['version']}" if engine["version"] else None,
        )

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        request_id = _request(ctx, "compile-register")
        prior = _committed(ctx, request_id)
        if prior is not None:
            # The outputs were registered before the worker stopped: report them, never compile
            # and register a second time.
            paper_id = str(ctx.params["paper_id"])
            created = {
                str(r["id"])
                for r in prior.get("results", [])
                if str(r["id"]).startswith("version_")
            }
            registered = texcompile._versions_of_commit(ctx.store, paper_id, created)
            _commit_artifacts(ctx, prior, "compile_output")
            ctx.limitations.append(
                "the outputs were registered before the worker stopped; this result reports "
                "them without compiling again (see the compile log for diagnostics)"
            )
            return {
                "status": "succeeded" if "pdf" in registered else "failed",
                "outputs": registered,
                "commit": prior | {"replayed": True},
                "resumed_after_registration": True,
            }
        result = texcompile.compile_paper(
            ctx.store,
            ctx.env.home,
            ctx.caller,
            str(ctx.params["paper_id"]),
            fetch=bool(ctx.params["fetch"]),
            request_id=request_id,
            actor=ctx.run.actor,
            limits=ctx.options.get("limits"),
            checkpoint=ctx.checkpoint,
        )
        if result.get("commit"):
            _commit_artifacts(ctx, result["commit"], "compile_output")
        return result


def _committed(ctx: runs.RunContext, request_id: str) -> dict[str, Any] | None:
    conn = ctx.store.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT response FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    from prometheos.domain.canonical import loads_internal

    return dict(loads_internal(row[0]))


class EngineInstallHandler(runs.Handler):
    """Created only by an owner decision (CLI confirmation, paired hub, spent approval grant):
    the run row is that decision's record, so executing it needs no second authority."""

    operation = "tex_engine_install"
    version = "tex-install-v1"
    protected = True  # one approved install per grant (round 2, F3)

    def fingerprint(
        self,
        env: runs.Env,
        params: dict[str, Any],
        caller: reads.Caller,
        run: Any = None,
    ) -> runs.Fingerprint:
        """The pinned release (not whether it is installed yet, which the install changes)."""
        plan = texengine.install_plan(env.home)
        pinned = {
            k: plan[k] for k in ("engine", "version", "release_tag", "sha256", "download_bytes")
        }
        return runs.Fingerprint(inputs=(), config=pinned, model=f"tectonic {plan['version']}")

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        ctx.checkpoint("install")
        if (
            ctx.output("install") is not None
            or texengine.status(ctx.env.home)["state"] == "installed"
        ):
            # Installed before the worker stopped (or by another run): nothing to repeat.
            return texengine.redacted(ctx.env.home, texengine.status(ctx.env.home))
        archive = ctx.params.get("archive")
        # The approved effect starts here: if this worker stops before it ends, the run is
        # unknown_external and never repeated under the same approval (round 2, F3).
        ctx.external_started("tectonic install", None, str(ctx.params["plan_digest"]))
        try:
            report = texengine.install(
                ctx.env.home,
                str(ctx.params["plan_digest"]),
                archive=Path(archive) if archive else None,
            )
        except PrometheosError:
            ctx.external_confirmed("install failed; nothing was installed (staging removed)")
            raise
        ctx.complete("install", {"installed": bool(report.get("installed"))})
        return texengine.redacted(ctx.env.home, report)

    def local_outcome(self, env: runs.Env, run: Any) -> dict[str, Any] | None:
        """The pinned engine is installed and verifies (archive digest of the pin, executable
        digest of the install manifest): the install completed before the worker stopped."""
        status = texengine.status(env.home)
        if status["state"] != "installed":
            return None
        return texengine.redacted(env.home, status)


# ----- speech models -------------------------------------------------------------------------


class ModelInstallHandler(runs.Handler):
    """Created only by an owner decision, like the engine install."""

    operation = "audio_models_install"
    version = "model-install-v1"
    protected = True  # one approved download per grant (round 2, F3)

    def fingerprint(
        self,
        env: runs.Env,
        params: dict[str, Any],
        caller: reads.Caller,
        run: Any = None,
    ) -> runs.Fingerprint:
        from prometheos.audio import models

        plan = models.plan(env.home, str(params["model"]))
        return runs.Fingerprint(
            inputs=(), config={"plan_sha256": plan["plan_sha256"]}, model=str(params["model"])
        )

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        from prometheos.audio import models

        outer: Callable[[str, int, int], None] | None = ctx.options.get("progress")
        tick = throttled(ctx, "download")

        def progress(name: str, received: int, total: int) -> None:
            tick()  # cancellation between chunks of a download
            if outer is not None:
                outer(name, received, total)

        ctx.checkpoint("install")
        ctx.external_started("speech model download", None, str(ctx.params["plan_sha256"]))
        try:
            done = models.install(
                ctx.env.home, str(ctx.params["model"]), str(ctx.params["plan_sha256"]),
                progress=progress,
            )  # fmt: skip
        except PrometheosError:
            ctx.external_confirmed("download failed; nothing was installed (staging removed)")
            raise
        ctx.complete("install", {"status": str(done.get("status"))})
        return models.redact_location(ctx.env.home, done)

    def local_outcome(self, env: runs.Env, run: Any) -> dict[str, Any] | None:
        """The model is in place and every file matches its recorded digest."""
        from prometheos.audio import models

        spec = models.KNOWN.get(str(run.params.get("model")))
        if spec is None or not models.installed(env.home, spec):
            return None
        target = models.target_dir(env.home, spec)
        if not models._verify(target):
            return None
        done = {"model": spec.key, "status": "installed", "location": str(target)}
        return models.redact_location(env.home, done)


# ----- provider ------------------------------------------------------------------------------


class ProviderRunHandler(runs.Handler):
    """Created only after the owner's capability (and egress grant) was spent with the run."""

    operation = "provider_run"
    version = "provider-run-v1"
    protected = True  # a failed approved run is final (round 2, F3)

    @staticmethod
    def _config(env: runs.Env) -> Any:
        from prometheos.providers import config as provider_config

        conn = env.store.connect(readonly=True)
        try:
            config, _revision = provider_config.load(conn)
        finally:
            conn.close()
        return config

    def fingerprint(
        self,
        env: runs.Env,
        params: dict[str, Any],
        caller: reads.Caller,
        run: Any = None,
    ) -> runs.Fingerprint:
        from prometheos.providers import generate

        config = self._config(env)
        task = runs_task(env, run)
        if task is not None:
            # Once the task is prepared, the request is rebuilt from its stored context: the
            # task's own commit moved the project on, so a fresh render would never match
            # (round 2, F2). The task context is immutable.
            payload = generate.payload_digest_from_task(
                env.store, config, env.service._secrets(), env.service._transport(),
                task_id=str(task["task_id"]), purpose=str(params["purpose"]),
            )  # fmt: skip
        else:
            payload = generate.payload_digest(
                env.store,
                config,
                env.service._secrets(),
                env.service._transport(),
                purpose=str(params["purpose"]),
                meeting_id=params["meeting_id"],
                context_budget=int(params["context_budget"]),
                include_restricted=bool(params["include_restricted"]),
            )
        return runs.Fingerprint(
            inputs=(("payload", payload),),
            config=generate.run_configuration(config),
            model=config.model,
        )

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        from prometheos.providers import generate

        return generate.execute_run(
            ctx,
            ctx.store,
            self._config(ctx.env),
            ctx.env.service._secrets(),
            ctx.env.service._transport(),
            actor=ctx.run.actor,
        )

    def recover_uncertain(self, env: runs.Env, run: Any) -> None:
        """A worker that stopped during a call leaves its ledger row ``sent``: whether it was
        received or billed is unknown, so it reads as unknown there too."""
        from prometheos.providers import ledger

        run_key = derived_request_id("provider-run", env.store.project_id, run.id)
        conn = env.store.connect(readonly=True)
        try:
            sent = [
                str(r[0])
                for r in conn.execute(
                    "SELECT id FROM provider_calls WHERE run_key = ? AND state = 'sent'",
                    (run_key,),
                )
            ]
        finally:
            conn.close()
        for call_id in sent:
            ledger.mark(
                env.store, call_id, "unknown",
                error="the worker stopped while the call was in flight",
            )  # fmt: skip


def runs_task(env: runs.Env, run: Any) -> dict[str, Any] | None:
    """The prepared task of a provider run (its completed ``task`` stage), if any."""
    if run is None:
        return None
    conn = env.store.connect(readonly=True)
    try:
        return runs.stage_output(conn, run.id, "task")
    finally:
        conn.close()


for _handler in (
    ImportPlanHandler(),
    ImportCommitHandler(),
    AudioImportHandler(),
    CompileHandler(),
    EngineInstallHandler(),
    ModelInstallHandler(),
    ProviderRunHandler(),
):
    runs.register(_handler)
