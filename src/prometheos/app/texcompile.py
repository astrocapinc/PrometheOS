"""Staged Tectonic compile of a paper workspace (A01, CP9.1; decision 0012).

1. Snapshot: every compile input becomes (or already is) an immutable source version.
2. Static refusal scan of the staged text (shell escape, literal paths outside the
   workspace): a refused source never reaches the engine.
3. Stage the exact version bytes into a fresh directory and run the pinned engine there
   with ``--untrusted``, ``--only-cached`` unless a fetch is requested, a clean
   environment, a timeout and output/memory limits.
4. Audit what the engine read (absolute-path warnings, Makefile dependency list, files
   opened in the TeX log). Anything outside the staging directory discards the outputs.
5. Register the compile log always, and the PDF and SyncTeX map only on success, as
   generated versions with ``conversion`` provenance naming the exact input versions.
   A failed compile therefore leaves the previous PDF as the latest PDF version, still
   labelled with its own inputs.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import Any

from prometheos.app import reads, sources, texengine, workspace
from prometheos.app.reads import Caller
from prometheos.domain.bodies import SourceBody, VersionBody
from prometheos.domain.canonical import canonical_sha256, loads_internal
from prometheos.domain.ids import new_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.latex import diagnostics as diag
from prometheos.latex import structure, synctex
from prometheos.store import derived
from prometheos.store.lock import maintenance_lock
from prometheos.store.project import ProjectStore, Txn

MAX_INPUTS = 200  # Transformation.input_version_refs holds at most 200 versions
MAX_STAGE_BYTES = 256 * 1024 * 1024
MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_SYNCTEX_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
OUTPUT_KINDS = {
    "pdf": {"mime": "application/pdf", "title": "Compiled PDF"},
    "synctex": {"mime": "application/x-synctex", "title": "SyncTeX map"},
    "log": {"mime": "text/plain", "title": "Compile log"},
}
SCAN_SUFFIXES = (".tex", ".sty", ".cls", ".def", ".cfg", ".clo", ".ltx")
_ABSOLUTE_WARNING = re.compile(r"accessing absolute path `([^`]+)`")


def _limits() -> texengine.RunLimits:
    return texengine.RunLimits()


def _bounded_log(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= MAX_LOG_BYTES:
        return text
    half = MAX_LOG_BYTES // 2
    head = data[:half].decode("utf-8", "ignore")
    tail = data[-half:].decode("utf-8", "ignore")
    return f"{head}\n[... {len(data) - 2 * half} bytes of log omitted ...]\n{tail}"


def _scrub(text: str, staging: Path) -> str:
    """Replace the staging directory with '.' so no local path enters the record."""
    for form in {str(staging), staging.as_posix(), str(staging).replace("\\", "\\\\")}:
        text = re.sub(re.escape(form), ".", text, flags=re.IGNORECASE)
    return text


def audit_reads(
    stderr: str, makefile: str | None, tex_log: str, staging: Path, out_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Files the engine read outside the staging directory (decision 0012 item 11).

    Tectonic writes Makefile dependencies as ``<outdir>/<name as requested>`` (an absolute
    request stays absolute), so the output-directory prefix is mapped back to the staging
    directory before resolving."""
    findings: list[dict[str, Any]] = []
    root = os.path.normcase(os.path.realpath(staging))
    out_prefix = os.path.normcase(str(out_dir)) if out_dir is not None else None

    def outside(name: str) -> bool:
        candidate = name.strip().strip('"')
        if not candidate:
            return False
        if out_prefix and os.path.normcase(candidate).startswith(out_prefix):
            candidate = candidate[len(out_prefix) :].lstrip("\\/") or "."
        full = candidate if os.path.isabs(candidate) else os.path.join(staging, candidate)
        resolved = os.path.normcase(os.path.realpath(full))
        return not (resolved == root or resolved.startswith(root + os.sep))

    for match in _ABSOLUTE_WARNING.finditer(stderr):
        if outside(match.group(1)):
            findings.append({"code": "read_outside", "name": os.path.basename(match.group(1))})
    if makefile:
        # "<targets> : <primary>" then one dependency per " \<newline>  " continuation;
        # paths are written raw (they may contain spaces), so split on the continuations.
        _, _, deps = makefile.partition(" : ")
        for dep in re.split(r" \\\r?\n  ", deps.strip()):
            if dep.strip() and outside(dep):
                findings.append({"code": "read_outside", "name": os.path.basename(dep)})
    for match in re.finditer(r"\(((?:[A-Za-z]:)?[\\/][^\s()\"]+|\.\.[\\/][^\s()\"]+)", tex_log):
        if outside(match.group(1)):
            findings.append({"code": "read_outside", "name": os.path.basename(match.group(1))})
    unique = {f["name"]: f for f in findings}
    return list(unique.values())


def _output_sources(txn: Txn, paper_id: str) -> dict[str, str]:
    """kind -> source id of this paper's existing compile output sources."""
    found: dict[str, str] = {}
    for row in txn.conn.execute(
        "SELECT source_id FROM relations WHERE target_id = ? AND relation = 'used_in' "
        "AND retired_seq IS NULL",
        (paper_id,),
    ):
        state = txn.get(row[0])
        if state is None or state.kind != "source" or state.lifecycle == "deleted":
            continue
        description = str(state.body.get("description", ""))
        for kind in OUTPUT_KINDS:
            if description.startswith(f"[tectonic-{kind}] "):
                found.setdefault(kind, state.id)
    return found


def _register_outputs(
    store: ProjectStore,
    *,
    paper_id: str,
    paper_title: str,
    outputs: dict[str, tuple[bytes, Path | None]],
    transformation: dict[str, Any],
    sensitivity: str,
    actor: str,
    request_id: str,
    max_rank: int,
) -> dict[str, Any]:
    prepared: dict[str, dict[str, Any]] = {}  # filled below, under the lock when it stages media

    def handler(txn: Txn) -> None:
        existing = _output_sources(txn, paper_id)
        paper = txn.get(paper_id)
        if paper is None:
            raise PrometheosError("not_found", "paper not found", {"id": paper_id})
        for index, (kind, item) in enumerate(prepared.items()):
            txn.op_index = index
            source_id = existing.get(kind)
            if source_id is None:
                source_id = new_id("source")
                body = SourceBody(
                    title=f"{OUTPUT_KINDS[kind]['title']}: {paper_title}"[:300],
                    origin="file",
                    origin_uri=None,
                    root_binding=None,
                    retention="copy",
                    description=f"[tectonic-{kind}] generated by compiling the LaTeX workspace "
                    f"of paper {paper_id}",
                ).model_dump(mode="json")
                txn.write_revision(
                    source_id,
                    "source",
                    1,
                    txn.insert_body(body),
                    _highest(sensitivity, paper.sensitivity),
                    "active",
                    None,
                    "tex_compile_output",
                    body=body,
                )
                txn.result(source_id, 1)
                txn.conn.execute(
                    "INSERT INTO relations (source_id, source_rev, target_id, target_rev, "
                    "relation, created_seq) VALUES (?,?,?,?,?,?)",
                    (source_id, 1, paper_id, paper.rev, "used_in", txn.seq),
                )
            if item["storage"] == "body":
                txn.conn.execute(
                    "INSERT OR IGNORE INTO payloads (sha256, data, byte_length, created_seq) "
                    "VALUES (?,?,?,?)",
                    (item["sha256"], item["data"], item["size"], txn.seq),
                )
            else:
                txn.conn.execute(
                    "INSERT OR IGNORE INTO media (sha256, byte_length, storage_key, created_seq) "
                    "VALUES (?,?,?,?)",
                    (item["sha256"], item["size"], item["key"], txn.seq),
                )
            version_id = new_id("version")
            mime = str(OUTPUT_KINDS[kind]["mime"])
            version = VersionBody(
                source_id=source_id,
                sha256=item["sha256"],
                byte_length=item["size"],
                mime=mime,
                captured_at=txn.recorded_at,
                storage=item["storage"],
                storage_key=item["key"],
                availability="retained",
                extraction="none",
                derived_from=None,
                payload_schema="text" if kind != "pdf" else None,
                transformation=transformation,  # type: ignore[arg-type]
            ).model_dump(mode="json")
            txn.write_revision(
                version_id,
                "version",
                1,
                txn.insert_body(version),
                _highest(sensitivity, paper.sensitivity),
                "active",
                None,
                "tex_compile_output",
                body=version,
            )
            if kind == "log" and item["data"] is not None:
                derived.index_text(
                    txn.conn, version_id, "version", "", bytes(item["data"]).decode("utf-8")
                )
            txn.result(version_id, 1)

    # Media is staged under the maintenance lock and the lock is held until the reference
    # commits, as for source imports: a content-addressed file this compile reuses (the same
    # bytes as a purged output whose file is still listed for deletion) can then never be swept
    # or collected in between (decision 0031).
    needs_lock = any(len(data) > sources.INLINE_LIMIT for data, _path in outputs.values())
    with maintenance_lock(store.directory) if needs_lock else nullcontext():
        for kind, (data, path) in outputs.items():
            sha = hashlib.sha256(data).hexdigest()
            entry: dict[str, Any] = {"sha256": sha, "size": len(data), "data": None, "key": None}
            if len(data) <= sources.INLINE_LIMIT:
                entry["storage"] = "body"
                entry["data"] = data
                entry["key"] = f"payload:{sha}"
            else:
                assert path is not None
                entry["storage"] = "media"
                entry["key"] = sources._stage_media(store, path, sha)
            prepared[kind] = entry
        return store.commit(
            request_id=request_id,
            request_sha256=canonical_sha256(
                {
                    "op": "tex_compile_outputs",
                    "paper_id": paper_id,
                    "outputs": {k: v["sha256"] for k, v in prepared.items()},
                    "transformation": transformation,
                }
            ),
            expected_seq=None,
            actor=actor,
            handler=handler,
            max_rank=max_rank,
        )


def _highest(*levels: str) -> str:
    return max(levels, key=lambda s: SENSITIVITY_ORDER[s])


def compile_paper(
    store: ProjectStore,
    home: Path,
    caller: Caller,
    paper_id: str,
    *,
    fetch: bool,
    request_id: str,
    actor: str,
    limits: texengine.RunLimits | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """``checkpoint(stage)`` is called between the snapshot, the staging, the engine run and
    the registration of outputs (an operation run's cancellation point, decision 0017); the
    outputs commit under ``request_id``, so a resumed compile replays instead of registering
    them twice."""
    limits = limits or _limits()
    engine = texengine.resolve(home)
    if checkpoint is not None:
        checkpoint("snapshot")
    snap = workspace.snapshot(store, caller, paper_id, actor, inputs_only=True)
    ws = snap["workspace"]
    versions: dict[str, dict[str, Any]] = snap["versions"]
    if len(versions) > MAX_INPUTS:
        raise PrometheosError(
            "budget_exceeded",
            f"a compile binds at most {MAX_INPUTS} input files",
            {"files": len(versions)},
        )
    if ws["main"] not in versions:
        raise PrometheosError("source_unavailable", "the main file is missing", {})
    input_ids = sorted(v["version_id"] for v in versions.values())
    sensitivity = _highest("normal", *(v["sensitivity"] for v in versions.values()))
    base = {
        "workspace": ws,
        "inputs": {p: v["version_id"] for p, v in sorted(versions.items())},
        "snapshot_commits": [c["project_seq"] for c in snap["commits"]],
        "engine": {"name": "tectonic", "version": engine.version, "exe_sha256": engine.exe_sha256},
        "fetch": fetch,
    }
    # Stage the exact retained bytes of every input version.
    work_root = store.directory / "staging" / f"tex-{uuid.uuid4().hex}"
    stage = work_root / "work"
    out_dir = work_root / "out"
    temp = work_root / "tmp"
    for folder in (stage, out_dir, temp):
        folder.mkdir(parents=True)
    try:
        if checkpoint is not None:
            checkpoint("stage")
        conn = store.connect(readonly=True)
        try:
            texts: dict[str, str] = {}
            sources_text: list[str] = []  # every UTF-8 input: what diagnostics may quote
            total = 0
            for rel, info in versions.items():
                record = reads.current_record(conn, info["version_id"])
                assert record is not None
                data = workspace.version_bytes(conn, store.directory, record["body"])
                if data is None:
                    raise PrometheosError(
                        "source_unavailable", "a compile input is not retained", {"path": rel}
                    )
                total += len(data)
                if total > MAX_STAGE_BYTES:
                    raise PrometheosError(
                        "budget_exceeded", "compile inputs exceed the staging limit", {}
                    )
                target = stage.joinpath(*PurePosixPath(rel).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "xb") as handle:
                    handle.write(data)
                if rel.lower().endswith(SCAN_SUFFIXES):
                    texts[rel] = data.decode("utf-8", "replace")
                try:
                    sources_text.append(data.decode("utf-8"))
                except UnicodeDecodeError:
                    pass
        finally:
            conn.close()
        refusals = structure.scan_hostile(texts)
        if refusals:
            return base | {
                "status": "refused",
                "diagnostics": [
                    {
                        "severity": "error",
                        "file": f["file"],
                        "line": f["line"],
                        "message": f["message"],
                        "origin": "prometheos",
                        "context": f["code"],
                    }
                    for f in refusals
                ],
                "outputs": {},
                "commit": None,
                "seconds": 0.0,
            }
        makefile = temp / "deps.mk"
        argv = [
            str(engine.executable),
            "--color",
            "never",
            "--untrusted",
            "--keep-logs",
            "--synctex",
            "--bundle",
            texengine.TECTONIC.bundle_url,
            "--outdir",
            str(out_dir),
            "--makefile-rules",
            str(makefile),
        ]
        if not fetch:
            argv.append("--only-cached")
            texengine.mark_bundle_checked(engine.cache)
        argv.append(ws["main"])
        env = texengine.clean_environment(engine.cache, temp, network=fetch)
        if checkpoint is not None:
            checkpoint("compile")
        run = texengine.run(argv, cwd=stage, env=env, limits=limits, watch=out_dir)
        stem = PurePosixPath(ws["main"]).stem
        stderr = run.stderr.decode("utf-8", "replace") + run.stdout.decode("utf-8", "replace")
        log_path = out_dir / f"{stem}.log"
        tex_log = ""
        if log_path.is_file() and log_path.stat().st_size <= 64 * 1024 * 1024:
            tex_log = log_path.read_bytes().decode("utf-8", "replace")
        mk = makefile.read_text("utf-8", "replace") if makefile.is_file() else None
        outside = audit_reads(stderr, mk, tex_log, stage, out_dir)
        status = "succeeded"
        if outside:
            status = "refused"
        elif run.timed_out:
            status = "timeout"
        elif run.output_exceeded:
            status = "output_limit"
        elif run.memory_exceeded:
            status = "memory_limit"
        elif run.returncode != 0 or not (out_dir / f"{stem}.pdf").is_file():
            status = "failed"
        # Reads are *proven* to have stayed inside the workspace (and the bundle) only when
        # the engine finished and wrote its dependency list, which names every file it
        # opened from disk. Tectonic writes that list on success only (decision 0012 item
        # 11a), so a failed, stopped or crashed run is unproven.
        proven = status == "succeeded" and mk is not None
        if status == "succeeded" and not proven:
            outside = [{"code": "reads_unverified", "name": "no dependency list"}]
            status = "refused"
        if status == "refused":
            # The engine read something outside the workspace: nothing it produced,
            # including the log, is kept or shown.
            return base | {
                "status": "refused",
                "diagnostics": [
                    {
                        "severity": "error",
                        "file": None,
                        "line": None,
                        "message": (
                            f"the document read a file outside the workspace ({f['name']}); "
                            "outputs were discarded"
                            if f["code"] == "read_outside"
                            else "the files this compile read could not be verified; "
                            "outputs were discarded"
                        ),
                        "origin": "prometheos",
                        "context": f["code"],
                    }
                    for f in outside
                ],
                "outputs": {},
                "commit": None,
                "seconds": run.seconds,
            }
        notes = {
            "timeout": f"compile stopped after {limits.timeout_seconds:g} s (time limit)",
            "output_limit": "compile stopped: output exceeded its size limit",
            "memory_limit": "compile stopped: the engine reached its memory limit",
        }
        header = [
            f"PrometheOS compile log: Tectonic {engine.version}, status {status}, "
            f"exit {run.returncode}, {run.seconds:g} s, fetch {'allowed' if fetch else 'off'}",
            diag.INPUTS_PREFIX + json.dumps(sorted(versions)),
        ]
        note = f"error: {notes[status]}\n" if status in notes else ""
        raw_log = _bounded_log(
            _scrub(
                _scrub(
                    "\n".join(header)
                    + f"\n{diag.ENGINE_HEADER}\n{note}{stderr}\n"
                    + f"{diag.LOG_HEADER} ({stem}.log)\n{tex_log}",
                    stage,
                ),
                work_root,
            )
        )
        if proven:
            log_text = raw_log
        else:
            # Unproven reads: the raw engine output and TeX log may carry text of a file
            # outside the workspace, so neither is stored nor returned. What remains is the
            # header and diagnostics re-derived from it, kept only in a form that quotes
            # fixed message templates and workspace source text (decision 0012 item 11a).
            sanitized = diag.sanitize(
                diag.diagnostics(raw_log), set(versions), "\n".join(sources_text)
            )
            log_text = diag.sanitized_log(header, sanitized)
        outputs: dict[str, tuple[bytes, Path | None]] = {"log": (log_text.encode("utf-8"), None)}
        if status == "succeeded":
            pdf = out_dir / f"{stem}.pdf"
            if pdf.stat().st_size > MAX_PDF_BYTES:
                status = "output_limit"
            else:
                outputs["pdf"] = (pdf.read_bytes(), pdf)
                sync = _read_synctex(out_dir, stem, stage)
                if sync is not None:
                    outputs["synctex"] = (sync.encode("utf-8"), None)
        transformation = {
            "type": "conversion",
            "engine": "tectonic",
            "engine_version": engine.version,
            "model": None,
            "language": None,
            "parameters_digest": canonical_sha256(
                {
                    "argv": [a for a in argv[1:] if a not in (str(out_dir), str(makefile))],
                    "main": ws["main"],
                    "fetch": fetch,
                    "exe_sha256": engine.exe_sha256,
                    "limits": {
                        "timeout_seconds": limits.timeout_seconds,
                        "max_output_dir_bytes": limits.max_output_dir_bytes,
                        "max_memory_bytes": limits.max_memory_bytes,
                    },
                }
            ),
            "input_version_refs": input_ids,
            "run_id": None,
        }
        if checkpoint is not None:
            checkpoint("register")
        commit = _register_outputs(
            store,
            paper_id=paper_id,
            paper_title=ws["title"],
            outputs=outputs,
            transformation=transformation,
            sensitivity=sensitivity,
            actor=actor,
            request_id=request_id,
            max_rank=caller.rank,
        )
        created = {
            r["id"]: r for r in commit.get("results", []) if str(r["id"]).startswith("version_")
        }
        registered = _versions_of_commit(store, paper_id, set(created))
        return base | {
            "status": status,
            "diagnostics": diag.diagnostics(log_text),
            "outputs": registered,
            "commit": commit,
            "seconds": run.seconds,
            "exit_code": run.returncode,
        }
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def _read_synctex(out_dir: Path, stem: str, stage: Path) -> str | None:
    packed = out_dir / f"{stem}.synctex.gz"
    plain = out_dir / f"{stem}.synctex"
    try:
        if packed.is_file():
            with gzip.open(packed, "rb") as handle:
                data = handle.read(MAX_SYNCTEX_BYTES + 1)
        elif plain.is_file():
            data = plain.read_bytes()[: MAX_SYNCTEX_BYTES + 1]
        else:
            return None
    except (OSError, EOFError):
        return None
    if len(data) > MAX_SYNCTEX_BYTES:
        return None
    return synctex.normalize_inputs(data.decode("utf-8", "replace"), str(stage))


def _versions_of_commit(store: ProjectStore, paper_id: str, ids: set[str]) -> dict[str, str]:
    conn = store.connect(readonly=True)
    try:
        out: dict[str, str] = {}
        for version_id in ids:
            record = reads.current_record(conn, version_id)
            if record is None:
                continue
            mime = record["body"]["mime"]
            for kind, meta in OUTPUT_KINDS.items():
                if meta["mime"] == mime:
                    out[kind] = version_id
        return out
    finally:
        conn.close()


# ----- compile state and SyncTeX (pure) --------------------------------------------------------


def _output_versions(
    conn: sqlite3.Connection, caller: Caller, paper_id: str
) -> dict[str, list[dict[str, Any]]]:
    """kind -> visible versions newest first, each with its commit sequence."""
    kinds: dict[str, str] = {}
    for row in conn.execute(
        "SELECT source_id FROM relations WHERE target_id = ? AND relation = 'used_in' "
        "AND retired_seq IS NULL",
        (paper_id,),
    ):
        source = reads.current_record(conn, row[0])
        if (
            source is None
            or source["kind"] != "source"
            or not caller.may_see(source["sensitivity"])
        ):
            continue
        description = str(source["body"].get("description", ""))
        for kind in OUTPUT_KINDS:
            if description.startswith(f"[tectonic-{kind}] "):
                kinds[source["id"]] = kind
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in OUTPUT_KINDS}
    if not kinds:
        return out
    for row in conn.execute(
        "SELECT r.id, r.sensitivity, r.created_seq, b.encoded FROM records r JOIN revisions v ON "
        "v.record_id = r.id AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = 'version' AND r.lifecycle = 'active' ORDER BY r.created_seq DESC"
    ):
        if row["encoded"] is None or not caller.may_see(row["sensitivity"]):
            continue
        body = loads_internal(row["encoded"])
        found = kinds.get(str(body.get("source_id")))
        if found is None or len(out[found]) >= 20:
            continue
        out[found].append(
            {
                "version_id": row["id"],
                "source_id": body["source_id"],
                "commit_seq": row["created_seq"],
                "captured_at": body["captured_at"],
                "sha256": body["sha256"],
                "byte_length": body["byte_length"],
                "transformation": body.get("transformation"),
                "body": body,
            }
        )
    return out


def status(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, paper_id: str
) -> dict[str, Any]:
    workspace.require_paper(conn, caller, paper_id)
    versions = _output_versions(conn, caller, paper_id)
    log = versions["log"][0] if versions["log"] else None
    pdf = versions["pdf"][0] if versions["pdf"] else None
    sync = next(
        (
            s
            for s in versions["synctex"]
            if pdf is not None and s["commit_seq"] == pdf["commit_seq"]
        ),
        None,
    )
    diagnostics: list[dict[str, Any]] = []
    log_status = None
    if log is not None:
        data = workspace.version_bytes(conn, store_dir, log["body"])
        text = data.decode("utf-8", "replace") if data is not None else ""
        diagnostics = diag.diagnostics(text)
        match = re.search(r"status (\w+)", text.split("\n", 1)[0])
        log_status = match.group(1) if match else None

    def public(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is None:
            return None
        return {k: v for k, v in item.items() if k != "body"}

    return {
        "paper_id": paper_id,
        "project_seq": reads.project_seq(conn),
        "last_compile": None
        if log is None
        else {
            "status": log_status,
            "log": public(log),
            "pdf_current": pdf is not None and pdf["commit_seq"] == log["commit_seq"],
            "diagnostics": diagnostics,
        },
        "pdf": public(pdf),
        "synctex": public(sync),
        "history": [public(v) for v in versions["log"][:10]],
    }


def _sync_map(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    paper_id: str,
    pdf_version: str | None,
) -> tuple[synctex.SyncMap, str]:
    versions = _output_versions(conn, caller, paper_id)
    pdfs = versions["pdf"]
    pdf = next((p for p in pdfs if p["version_id"] == pdf_version), None) if pdf_version else None
    if pdf_version and pdf is None:
        raise PrometheosError("not_found", "compiled PDF not found", {"id": pdf_version})
    pdf = pdf or (pdfs[0] if pdfs else None)
    if pdf is None:
        raise PrometheosError("not_found", "the paper has no compiled PDF yet", {})
    sync = next((s for s in versions["synctex"] if s["commit_seq"] == pdf["commit_seq"]), None)
    if sync is None:
        raise PrometheosError("not_found", "no SyncTeX map for this PDF", {})
    data = workspace.version_bytes(conn, store_dir, sync["body"])
    if data is None:
        raise PrometheosError("source_unavailable", "the SyncTeX map is not retained", {})
    return synctex.parse(data.decode("utf-8", "replace")), pdf["version_id"]


def sync_forward(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    paper_id: str,
    file: str,
    line: int,
    pdf_version: str | None = None,
) -> dict[str, Any]:
    workspace.require_paper(conn, caller, paper_id)
    sync, pdf_id = _sync_map(conn, store_dir, caller, paper_id, pdf_version)
    found = synctex.forward(sync, file, line)
    return {"pdf_version_id": pdf_id, "file": file, "line": line, "target": found}


def sync_inverse(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    paper_id: str,
    page: int,
    x: float,
    y: float,
    pdf_version: str | None = None,
) -> dict[str, Any]:
    workspace.require_paper(conn, caller, paper_id)
    sync, pdf_id = _sync_map(conn, store_dir, caller, paper_id, pdf_version)
    return {
        "pdf_version_id": pdf_id,
        "page": page,
        "x": x,
        "y": y,
        "source": synctex.inverse(sync, page, x, y),
    }
