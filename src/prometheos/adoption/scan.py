"""The adoption scan as a durable operation run (A05, CP17.1; decision 0039 section 4).

Stages: ``start`` (root digest, a new scan generation), ``inventory`` (walk, digests, archive
listings, stored in one transaction), ``finish`` (root digest again; a difference fails the
scan). A resumed run continues from its stored stages; a run that stops leaves its generation
``failed``, which later reads ignore.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from prometheos.adoption import analysis, drift, fusion, plan, profiles, tables
from prometheos.adoption.contracts import AdoptionScan, AnalysisSummary, ScanConfig
from prometheos.adoption.inventory import (
    Inventory,
    StatProvider,
    entry_stat,
    exclusion_prune,
    root_digest,
)
from prometheos.app import reads, runs, sources
from prometheos.domain.models import PrometheosError, utc_now

OPERATION = "adoption_scan"
CHECKPOINT_SECONDS = 2.0

# Tests replace this to simulate cloud placeholders (user-mode code cannot set those bits).
STAT_PROVIDER: StatProvider = entry_stat


def root_of(env: runs.Env, alias: str) -> tuple[Path, int]:
    conn = env.store.connect(readonly=True)
    try:
        settings, rev = sources._settings(conn, env.project_id)
    finally:
        conn.close()
    root = sources.root_path(settings, alias)
    if not root.is_dir():
        raise PrometheosError(
            "source_unavailable", "the adoption root is unavailable", {"alias": alias}
        )
    for place in (Path(env.service.home), Path(env.store.directory)):
        if _inside(place, root) or _inside(root, place):
            raise PrometheosError(
                "policy_denied",
                "the adoption root and the PrometheOS home or project store overlap; PrometheOS "
                "writes there, so the folder could never be proved unchanged",
                {"alias": alias},
            )
    return root, rev


def _inside(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.realpath(child)).relative_to(Path(os.path.realpath(parent)))
    except ValueError:
        return False
    return True


def _scan_of_run(conn: Any, run_id: str) -> AdoptionScan | None:
    row = conn.execute("SELECT * FROM adoption_scans WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else tables.from_row(AdoptionScan, row)


class AdoptionScanHandler(runs.Handler):
    operation = OPERATION
    version = "adoption-scan-v1"

    def fingerprint(
        self, env: runs.Env, params: dict[str, Any], caller: reads.Caller, run: Any = None
    ) -> runs.Fingerprint:
        profiles.get(ScanConfig.model_validate(params["config"]).profile)  # unknown: refused
        _root, rev = root_of(env, str(params["root_alias"]))
        return runs.Fingerprint(
            inputs=(), config={"settings_rev": rev, "root_alias": params["root_alias"]}
        )

    def execute(self, ctx: runs.RunContext) -> dict[str, Any]:
        alias = str(ctx.params["root_alias"])
        config = ScanConfig.model_validate(ctx.params["config"])
        root, _ = root_of(ctx.env, alias)
        last = [time.monotonic()]

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] >= CHECKPOINT_SECONDS:
                last[0] = now
                ctx.progress(done, total, "files")
                ctx.checkpoint("inventory")

        # start: one generation per run, found again on resume
        start = ctx.output("start")
        if start is None:
            before, _ = root_digest(root, STAT_PROVIDER, exclusion_prune(config))
            with ctx.txn() as conn:
                scan = _scan_of_run(conn, ctx.run.id)
                if scan is None:
                    previous = tables.latest_complete(conn, alias)
                    if previous is not None and config.previous_gen is None:
                        config = config.model_copy(update={"previous_gen": previous.scan_gen})
                    scan = AdoptionScan(
                        scan_gen=tables.next_scan_gen(conn),
                        root_alias=alias,
                        run_id=ctx.run.id,
                        state="running",
                        config=config,
                        started_at=utc_now(),
                        finished_at=None,
                        root_digest_before=before,
                        root_digest_after=None,
                        summary=None,
                    )
                    tables.insert_scan(conn, scan)
            ctx.complete(
                "start", {"scan_gen": scan.scan_gen, "root_digest_before": scan.root_digest_before}
            )
            start = ctx.output("start")
        assert start is not None
        gen = int(start["scan_gen"])

        if ctx.output("inventory") is None:
            ctx.checkpoint("inventory")
            result = Inventory(root, gen, config, stat_of=STAT_PROVIDER, progress=progress).run()
            summary = result.summary
            assert summary is not None

            def store_rows(conn: Any) -> None:
                tables.clear_files(conn, gen)
                tables.insert_files(conn, result.rows)
                scan = tables.require_scan(conn, gen)
                tables.replace_scan(conn, scan.model_copy(update={"summary": summary}))

            ctx.complete("inventory", {"entries": summary.entries}, within=store_rows)

        if ctx.output("analyze") is None:
            ctx.checkpoint("analyze")
            conn = ctx.store.connect(readonly=True)
            try:
                rows = tables.all_files(conn, gen)
                analysed = analysis.analyze(
                    conn, root, gen, rows, progress=progress, profile=profiles.get(config.profile)
                )
            finally:
                conn.close()
            stats = analysed.stats()

            def store_evidence(conn: Any) -> None:
                analysis.store(conn, gen, analysed)
                scan = tables.require_scan(conn, gen)
                tables.replace_scan(
                    conn,
                    scan.model_copy(update={"analysis": AnalysisSummary.model_validate(stats)}),
                )

            ctx.complete("analyze", stats, within=store_evidence)

        if config.rank and ctx.output("fuse") is None:
            ctx.checkpoint("fuse")
            conn = ctx.store.connect(readonly=True)
            try:
                rows = tables.all_files(conn, gen)
                decided = plan.decisions_for(conn, tables.require_scan(conn, gen).root_alias, rows)
                families = fusion.fuse(gen, rows, tables.all_evidence(conn, gen), decided)
            finally:
                conn.close()
            bands: dict[str, int] = {}
            for family in families:
                bands[family.band] = bands.get(family.band, 0) + 1
            conflicts = sum(len(f.conflicts) for f in families)

            def store_families(conn: Any) -> None:
                tables.replace_families(conn, gen, families)
                scan = tables.require_scan(conn, gen)
                if scan.analysis is not None:
                    summary = scan.analysis.model_copy(
                        update={
                            "families": len(families),
                            "by_band": dict(sorted(bands.items())),
                            "conflicts": conflicts,
                        }
                    )
                    tables.replace_scan(conn, scan.model_copy(update={"analysis": summary}))

            ctx.complete("fuse", {"families": len(families)}, within=store_families)

        ctx.checkpoint("finish")
        after, _ = root_digest(root, STAT_PROVIDER, exclusion_prune(config))
        conn = ctx.store.connect(readonly=True)
        try:
            scan = tables.require_scan(conn, gen)
        finally:
            conn.close()
        changed = after != scan.root_digest_before
        final = scan.model_copy(
            update={
                "root_digest_after": after,
                "finished_at": utc_now(),
                "state": "failed" if changed else "complete",
                "error": (
                    "the adopted folder changed while it was scanned (root digest before and "
                    "after differ); the inventory is kept but not used"
                    if changed
                    else None
                ),
            }
        )
        final = AdoptionScan.model_validate(final.model_dump())
        with ctx.txn() as conn:
            tables.replace_scan(conn, final)
            if final.state == "complete":
                drift.prune_generations(conn, final.root_alias)
        ctx.complete("finish", {"state": final.state})
        if changed:
            raise PrometheosError(
                "io_error",
                "the adopted folder changed while it was scanned; scan again when it is quiet",
                {"reason": "adoption_tree_changed", "scan_gen": gen},
            )
        return scan_view(final)

    def finish_txn(self, ctx: runs.RunContext, conn: Any, state: str) -> None:
        """A run that ends other than completed leaves its generation ``failed``."""
        if state == "completed":
            return
        scan = _scan_of_run(conn, ctx.run.id)
        if scan is not None and scan.state == "running":
            tables.replace_scan(
                conn,
                scan.model_copy(
                    update={"state": "failed", "finished_at": utc_now(), "error": f"run {state}"}
                ),
            )


def scan_view(scan: AdoptionScan) -> dict[str, Any]:
    """A scan as replies show it (the root by alias only)."""
    return scan.model_dump(mode="json")


runs.register(AdoptionScanHandler())
